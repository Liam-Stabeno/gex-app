import os
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ['SCHWAB_CLIENT_ID']
CLIENT_SECRET = os.environ['SCHWAB_CLIENT_SECRET']

TOKENS_FILE = 'data/tokens.json'


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
    tokens = load_tokens()
    # Always try to refresh — simplest approach for now
    tokens = refresh_access_token(tokens['refresh_token'])
    return tokens['access_token']


def fetch_option_chain(symbol: str, access_token: str) -> dict:
    """Fetch option chain for a symbol, limited to 60 DTE and 100 strikes ATM."""
    from datetime import timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    to_date = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")

    response = requests.get(
        "https://api.schwabapi.com/marketdata/v1/chains",
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "symbol": symbol,
            "contractType": "ALL",
            "includeUnderlyingQuote": True,
            "optionType": "S",       # Standard options only
            "strikeCount": 100,      # 100 strikes above and below ATM
            "fromDate": today,
            "toDate": to_date,
        }
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
    Parse option chain JSON into a GEX dataframe by strike.
    GEX = Gamma * Open Interest * Contract Multiplier * Spot Price
    Calls add positive GEX, puts add negative GEX.
    """
    spot = chain['underlyingPrice']
    multiplier = 100  # SPX options

    rows = []

    for exp_date, strikes in chain.get('callExpDateMap', {}).items():
        for strike_str, options in strikes.items():
            strike = float(strike_str)
            for opt in options:
                gamma = opt.get('gamma', 0) or 0
                oi = opt.get('openInterest', 0) or 0
                gex = gamma * oi * multiplier * spot
                rows.append({
                    'expiration': exp_date.split(':')[0],
                    'strike': strike,
                    'type': 'call',
                    'gamma': gamma,
                    'oi': oi,
                    'gex': gex
                })

    for exp_date, strikes in chain.get('putExpDateMap', {}).items():
        for strike_str, options in strikes.items():
            strike = float(strike_str)
            for opt in options:
                gamma = opt.get('gamma', 0) or 0
                oi = opt.get('openInterest', 0) or 0
                gex = gamma * oi * multiplier * spot * -1  # puts flip sign
                rows.append({
                    'expiration': exp_date.split(':')[0],
                    'strike': strike,
                    'type': 'put',
                    'gamma': gamma,
                    'oi': oi,
                    'gex': gex
                })

    df = pd.DataFrame(rows)

    # Aggregate GEX by strike across all expirations
    gex_by_strike = (
        df.groupby('strike')['gex']
        .sum()
        .reset_index()
        .rename(columns={'gex': 'net_gex'})
        .sort_values('strike')
    )

    return gex_by_strike, spot, df


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
    gex_by_strike, spot, raw_df = parse_gex(chain)
    levels = find_key_levels(gex_by_strike, spot)
    print_summary(levels, gex_by_strike, symbol)

    # Save data
    safe_symbol = symbol.replace("$", "")
    gex_by_strike.to_csv(f'data/gex_by_strike_{safe_symbol}.csv', index=False)
    raw_df.to_csv(f'data/gex_raw_{safe_symbol}.csv', index=False)
    print(f"\nData saved to data/gex_by_strike_{safe_symbol}.csv")
