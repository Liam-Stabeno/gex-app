import os
import json
import time
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ['SCHWAB_CLIENT_ID']
CLIENT_SECRET = os.environ['SCHWAB_CLIENT_SECRET']

TOKENS_FILE = 'data/tokens.json'

# ── Token cache — shared across all threads ────────────────────────────────────
# Schwab access tokens last 30 min; refresh proactively at 25 min so a fresh
# token is always ready when the streamer or GEX loop asks for one.
_token_lock   = threading.Lock()
_cached_token = {'access_token': None, 'expires_at': 0.0}
TOKEN_TTL     = 25 * 60   # seconds before we refresh (25 of 30 min)


def load_tokens() -> dict:
    with open(TOKENS_FILE, 'r') as f:
        return json.load(f)


def refresh_access_token(refresh_token: str) -> dict:
    import base64
    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()

    response = requests.post(
        "https://api.schwabapi.com/v1/oauth/token",
        headers={
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }
    )

    if not response.ok:
        print(f"Token refresh failed: {response.text}")
        response.raise_for_status()

    tokens = response.json()
    with open(TOKENS_FILE, 'w') as f:
        json.dump(tokens, f, indent=2)
    print("Access token refreshed.")
    return tokens


def get_access_token() -> str:
    """
    Return a valid Schwab access token, refreshing at most once per TOKEN_TTL seconds.
    Thread-safe — multiple threads share a single cached token.
    """
    with _token_lock:
        now = time.time()
        if _cached_token['access_token'] and now < _cached_token['expires_at']:
            return _cached_token['access_token']
        # Token missing or expired — refresh once
        tokens = load_tokens()
        tokens = refresh_access_token(tokens['refresh_token'])
        _cached_token['access_token'] = tokens['access_token']
        _cached_token['expires_at']   = now + TOKEN_TTL
        return tokens['access_token']


def fetch_option_chain(symbol: str, access_token: str) -> dict:
    """Fetch option chain for a symbol, limited to 60 DTE and 100 strikes ATM."""
    from datetime import timedelta
    today    = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    to_date  = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")

    params = {
        "symbol": symbol,
        "contractType": "ALL",
        "includeUnderlyingQuote": "true",
        "strikeCount": 100,
        "fromDate": today,
        "toDate": to_date,
    }

    response = requests.get(
        "https://api.schwabapi.com/marketdata/v1/chains",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params
    )

    # After market close Schwab rejects today as fromDate — retry with tomorrow
    if response.status_code == 400:
        params["fromDate"] = tomorrow
        response = requests.get(
            "https://api.schwabapi.com/marketdata/v1/chains",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params
        )

    if not response.ok:
        print(f"Option chain fetch failed: {response.text}")
        response.raise_for_status()

    data = response.json()

    # Debug: sample a non-0DTE option near ATM to verify gamma values
    spot = data.get('underlyingPrice', 0)
    print(f"\nSpot: {spot}, isDelayed: {data.get('isDelayed')}")
    for exp_date, strikes in data.get('callExpDateMap', {}).items():
        if ':0' in exp_date:
            continue  # skip 0DTE
        # Find strike closest to spot
        closest = min(strikes.keys(), key=lambda s: abs(float(s) - spot))
        opt = strikes[closest][0]
        print(f"\nSample option ({exp_date} {closest} call):")
        print(f"  gamma={opt.get('gamma')}  openInterest={opt.get('openInterest')}  delta={opt.get('delta')}")
        break

    return data


def parse_gex(chain: dict) -> pd.DataFrame:
    """
    Parse option chain JSON into GEX dataframes split by 0DTE vs multi-expiry.
    GEX = Gamma * Open Interest * Contract Multiplier * Spot Price
    Calls add positive GEX, puts add negative GEX.
    Returns: (gex_all, gex_0dte, gex_multi, spot, raw_df)
    """
    spot = chain['underlyingPrice']
    multiplier = 100

    rows = []

    for exp_date, strikes in chain.get('callExpDateMap', {}).items():
        is_0dte = exp_date.endswith(':0')
        for strike_str, options in strikes.items():
            strike = float(strike_str)
            for opt in options:
                gamma = opt.get('gamma', 0) or 0
                oi = opt.get('openInterest', 0) or 0
                gex = gamma * oi * multiplier * spot
                rows.append({
                    'expiration': exp_date.split(':')[0],
                    'is_0dte': is_0dte,
                    'strike': strike,
                    'type': 'call',
                    'gamma': gamma,
                    'oi': oi,
                    'gex': gex
                })

    for exp_date, strikes in chain.get('putExpDateMap', {}).items():
        is_0dte = exp_date.endswith(':0')
        for strike_str, options in strikes.items():
            strike = float(strike_str)
            for opt in options:
                gamma = opt.get('gamma', 0) or 0
                oi = opt.get('openInterest', 0) or 0
                gex = gamma * oi * multiplier * spot * -1
                rows.append({
                    'expiration': exp_date.split(':')[0],
                    'is_0dte': is_0dte,
                    'strike': strike,
                    'type': 'put',
                    'gamma': gamma,
                    'oi': oi,
                    'gex': gex
                })

    df = pd.DataFrame(rows)

    def aggregate(subset):
        if subset.empty:
            return pd.DataFrame(columns=['strike', 'net_gex'])
        return (
            subset.groupby('strike')['gex']
            .sum()
            .reset_index()
            .rename(columns={'gex': 'net_gex'})
            .sort_values('strike')
        )

    gex_all   = aggregate(df)
    gex_0dte  = aggregate(df[df['is_0dte']])
    gex_multi = aggregate(df[~df['is_0dte']])

    return gex_all, gex_0dte, gex_multi, spot, df


def find_key_levels(gex_by_strike: pd.DataFrame, spot: float) -> dict:
    # If total GEX is effectively zero, no meaningful levels exist
    if gex_by_strike['net_gex'].abs().sum() < 1000:
        return {'spot': spot, 'flip_level': None, 'put_wall': None, 'call_wall': None, 'pin': None}
    """Identify GEX flip point, put wall, and largest gamma strike."""

    # GEX flip: where cumulative GEX crosses zero near spot
    near_spot = gex_by_strike.copy()
    near_spot['cumulative'] = near_spot['net_gex'].cumsum()

    # Zero cross — where net GEX flips from negative to positive
    flip_candidates = near_spot[
        (near_spot['cumulative'].shift(1, fill_value=0) < 0) &
        (near_spot['cumulative'] >= 0)
    ]
    flip_level = flip_candidates['strike'].iloc[0] if not flip_candidates.empty else None

    # Put wall: most negative GEX strike below spot
    below_spot = gex_by_strike[gex_by_strike['strike'] < spot]
    put_wall = below_spot.loc[below_spot['net_gex'].idxmin(), 'strike'] if not below_spot.empty else None

    # Call wall: most positive GEX strike above spot
    above_spot = gex_by_strike[gex_by_strike['strike'] > spot]
    call_wall = above_spot.loc[above_spot['net_gex'].idxmax(), 'strike'] if not above_spot.empty else None

    # Largest absolute gamma strike (pin magnet)
    pin = gex_by_strike.loc[gex_by_strike['net_gex'].abs().idxmax(), 'strike']

    return {
        'spot': spot,
        'flip_level': flip_level,
        'put_wall': put_wall,
        'call_wall': call_wall,
        'pin': pin
    }


def get_watch_contracts(chain: dict, call_wall: float, n_strikes: int = 3,
                        underlying: str = '') -> list:
    """
    Extract option contract symbols to watch for flow monitoring.

    Selects calls AND puts at the n_strikes nearest strikes at/below call_wall,
    for both the 0DTE expiry and the nearest multi-expiry.
    Symbols are taken directly from the chain JSON so no format construction needed.

    Returns list of dicts:
        { symbol, strike, side ('call'|'put'), expiry_label ('0DTE'|'MULTI'),
          weight (1.0 for 0DTE, 0.15 for multi), is_0dte (bool) }
    """
    if call_wall is None:
        return []

    call_map = chain.get('callExpDateMap', {})
    put_map  = chain.get('putExpDateMap',  {})

    # Separate 0DTE and multi-expiry keys; pick nearest multi
    dte0_key  = None
    multi_key = None
    for exp_key in sorted(call_map.keys()):
        if exp_key.endswith(':0'):
            if dte0_key is None:
                dte0_key = exp_key
        else:
            if multi_key is None:
                multi_key = exp_key

    def contracts_for_expiry(exp_key: str, is_0dte: bool) -> list:
        weight = 1.0 if is_0dte else 0.15
        label  = '0DTE' if is_0dte else 'MULTI'
        result = []

        all_strikes = sorted(float(s) for s in call_map.get(exp_key, {}).keys())
        # n_strikes nearest strikes at or below the call wall
        below   = [s for s in all_strikes if s <= call_wall]
        targets = below[-n_strikes:] if below else []

        for strike in targets:
            strike_str = f"{strike:.1f}"

            calls = call_map.get(exp_key, {}).get(strike_str, [])
            if calls and calls[0].get('symbol'):
                result.append({
                    'symbol':       calls[0]['symbol'],
                    'strike':       strike,
                    'side':         'call',
                    'expiry_label': label,
                    'weight':       weight,
                    'is_0dte':      is_0dte,
                    'underlying':   underlying,
                })

            puts = put_map.get(exp_key, {}).get(strike_str, [])
            if puts and puts[0].get('symbol'):
                result.append({
                    'symbol':       puts[0]['symbol'],
                    'strike':       strike,
                    'side':         'put',
                    'expiry_label': label,
                    'weight':       weight,
                    'is_0dte':      is_0dte,
                    'underlying':   underlying,
                })

        return result

    contracts = []
    if dte0_key:
        contracts.extend(contracts_for_expiry(dte0_key,  is_0dte=True))
    if multi_key:
        contracts.extend(contracts_for_expiry(multi_key, is_0dte=False))

    return contracts


def print_summary(levels: dict, gex_by_strike: pd.DataFrame, symbol: str = "SPX"):
    spot = levels['spot']
    total_gex = gex_by_strike['net_gex'].sum()
    regime = "POSITIVE (stable)" if total_gex > 0 else "NEGATIVE (volatile)"

    print("\n" + "="*50)
    print(f"  GEX SUMMARY — {symbol} @ {spot:,.2f}")
    print("="*50)
    print(f"  Total GEX:    ${total_gex/1e9:.2f}B  [{regime}]")
    print(f"  GEX Flip:     {levels['flip_level']}")
    print(f"  Put Wall:     {levels['put_wall']}")
    print(f"  Call Wall:    {levels['call_wall']}")
    print(f"  Pin / Magnet: {levels['pin']}")
    print("="*50)

    print("\nTop 10 strikes by absolute GEX:")
    top = gex_by_strike.reindex(
        gex_by_strike['net_gex'].abs().nlargest(10).index
    ).sort_values('strike')
    max_gex = gex_by_strike['net_gex'].abs().max()
    if max_gex == 0 or np.isnan(max_gex):
        print("  No gamma data available — market may be closed or greeks not returned.")
    else:
        for _, row in top.iterrows():
            bar = "▓" * int(abs(row['net_gex']) / max_gex * 20)
            sign = "+" if row['net_gex'] > 0 else "-"
            print(f"  {row['strike']:>7.0f}  {sign}  {bar}")


if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "$SPX"

    print(f"Fetching {symbol} options chain...")
    token = get_access_token()
    chain = fetch_option_chain(symbol, token)
    gex_all, gex_0dte, gex_multi, spot, raw_df = parse_gex(chain)
    levels = find_key_levels(gex_all, spot)
    print_summary(levels, gex_all, symbol)
    print(f"\n0DTE strikes: {len(gex_0dte)}  |  Multi-expiry strikes: {len(gex_multi)}")

    # Save data
    safe_symbol = symbol.replace("$", "")
    gex_all.to_csv(f'data/gex_by_strike_{safe_symbol}.csv', index=False)
    raw_df.to_csv(f'data/gex_raw_{safe_symbol}.csv', index=False)
    print(f"\nData saved to data/gex_by_strike_{safe_symbol}.csv")
