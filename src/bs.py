"""
bs.py — Black-Scholes Greeks and implied volatility.

Uses only the standard library (math.erf) — no scipy dependency.

Public API:
    greeks_from_mid(bid, ask, S, K, T, r, flag) -> (gamma, charm, vanna)
        Compute gamma, charm, and vanna from bid/ask mid in a single IV solve.
        flag: 'c' = call, 'p' = put
        Returns (0, 0, 0) if IV cannot be found.

    gamma_from_mid(bid, ask, S, K, T, r, flag) -> float
        Compute BS gamma from bid/ask mid via IV inversion.
        Returns 0.0 if IV cannot be found (deep ITM, zero price, etc.)

    iv(mkt_price, S, K, T, r, flag) -> float | None
        Implied volatility via bisection.

    greeks(S, K, T, r, v) -> (gamma, charm, vanna)
        Greeks for a known vol (same for calls and puts).

    t_to_close(expiry_str, now=None) -> float
        Years until 16:00 ET on the expiry date (decays intraday).

    dte_to_t(expiry_str, today_str=None) -> float
        Convert 'YYYY-MM-DD' expiry to T in years (whole days; legacy).

Greeks reference:
    gamma  = d²V/dS²            — curvature of option value w.r.t. spot
    charm  = dDelta/dτ          — delta decay rate per unit time remaining
             Grows as τ→0; naturally amplifies 0DTE afternoon pinning
    vanna  = dDelta/dSigma      — delta sensitivity to implied vol
             Largest for OTM options; drives pinning during IV compression/expansion
"""

import math

_SQRT2   = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT2PI


def _d1(S: float, K: float, T: float, r: float, v: float) -> float:
    return (math.log(S / K) + (r + 0.5 * v * v) * T) / (v * math.sqrt(T))


def _bs_price(S: float, K: float, T: float, r: float, v: float, flag: str) -> float:
    if T <= 0.0 or v <= 0.0:
        return max(0.0, (S - K) if flag == 'c' else (K - S))
    d1 = _d1(S, K, T, r, v)
    d2 = d1 - v * math.sqrt(T)
    if flag == 'c':
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def _bs_gamma(S: float, K: float, T: float, r: float, v: float) -> float:
    if T <= 0.0 or v <= 0.0:
        return 0.0
    d1 = _d1(S, K, T, r, v)
    return _npdf(d1) / (S * v * math.sqrt(T))


def _bs_charm(S: float, K: float, T: float, r: float, v: float) -> float:
    """dDelta/dτ — delta change per unit time-to-expiry remaining (per year).

    Same formula for calls and puts (q=0, European).
    Grows in magnitude as τ→0 because of the 1/T factor, which naturally
    amplifies 0DTE pinning in the afternoon without manual time-weighting.
    """
    if T <= 0.0 or v <= 0.0:
        return 0.0
    sqrtT = math.sqrt(T)
    d1    = _d1(S, K, T, r, v)
    d2    = d1 - v * sqrtT
    return -_npdf(d1) * (2.0 * r * T - d2 * v * sqrtT) / (2.0 * T * v * sqrtT)


def _bs_vanna(S: float, K: float, T: float, r: float, v: float) -> float:
    """dDelta/dSigma — delta change per unit of implied vol.

    Same formula for calls and puts.  Zero at ATM (d2≈0); largest for OTM
    options.  Drives pinning when IV compresses/expands intraday.
    """
    if T <= 0.0 or v <= 0.0:
        return 0.0
    d1 = _d1(S, K, T, r, v)
    d2 = d1 - v * math.sqrt(T)
    return -_npdf(d1) * d2 / v


IV_MIN, IV_MAX = 0.005, 5.0     # search range for implied vol (0.5% – 500%)


def iv(mkt_price: float, S: float, K: float, T: float, r: float, flag: str,
       max_iter: int = 100, tol: float = 1e-6) -> float | None:
    """
    Implied volatility by bisection on log-vol.
    Returns sigma (annualised) or None when the price is outside what any vol in
    [IV_MIN, IV_MAX] can produce (below intrinsic, or no time value left).

    Bisection, not Newton: for cheap OTM / deep ITM 0DTE options vega is close to
    zero at any reasonable starting guess, and Newton stalls on the guess itself.
    """
    if T <= 0.0 or mkt_price <= 0.0:
        return None
    lo, hi = IV_MIN, IV_MAX
    p_lo = _bs_price(S, K, T, r, lo, flag)
    p_hi = _bs_price(S, K, T, r, hi, flag)
    if not (p_lo <= mkt_price <= p_hi):
        return None
    for _ in range(max_iter):
        mid = math.sqrt(lo * hi)
        p = _bs_price(S, K, T, r, mid, flag)
        if abs(p - mkt_price) < tol:
            return mid
        if p < mkt_price:
            lo = mid
        else:
            hi = mid
        if hi / lo < 1 + 1e-9:
            break
    return math.sqrt(lo * hi)


def greeks(S: float, K: float, T: float, r: float, v: float) -> tuple[float, float, float]:
    """(gamma, charm, vanna) for a given vol — identical for calls and puts."""
    return _bs_gamma(S, K, T, r, v), _bs_charm(S, K, T, r, v), _bs_vanna(S, K, T, r, v)


def t_to_close(expiry_str: str, now=None) -> float:
    """Years (calendar, /365) from now until 16:00 ET on the expiry date.

    Floors at 5 minutes so gamma stays finite into the close. Unlike dte_to_t,
    0DTE time decays through the session instead of being a fixed constant.
    """
    from datetime import datetime, time as dtime, date
    from zoneinfo import ZoneInfo
    et = ZoneInfo('America/New_York')
    now = now or datetime.now(et)
    close = datetime.combine(date.fromisoformat(expiry_str), dtime(16, 0), et)
    secs = (close - now).total_seconds()
    if secs < 0 and close.date() < now.date():
        return 0.0
    return max(secs, 300.0) / (365.0 * 24 * 3600)


def greeks_from_mid(bid: float, ask: float,
                    S: float, K: float, T: float, r: float,
                    flag: str) -> tuple[float, float, float]:
    """
    Derive (gamma, charm, vanna) from bid/ask mid with a single IV solve.
    Returns (0, 0, 0) when IV cannot be found (expired, zero quotes, etc.)

    Use this in preference to calling gamma_from_mid / charm_from_mid /
    vanna_from_mid separately — it's 3× faster.
    """
    if ask <= 0.0:
        return 0.0, 0.0, 0.0
    mid = (bid + ask) * 0.5 if bid > 0.0 else ask * 0.5
    if mid <= 0.0:
        return 0.0, 0.0, 0.0
    sigma = iv(mid, S, K, T, r, flag)
    if sigma is None:
        return 0.0, 0.0, 0.0
    return (
        _bs_gamma(S, K, T, r, sigma),
        _bs_charm(S, K, T, r, sigma),
        _bs_vanna(S, K, T, r, sigma),
    )


def gamma_from_mid(bid: float, ask: float,
                   S: float, K: float, T: float, r: float,
                   flag: str) -> float:
    """
    Derive BS gamma from the bid/ask mid via IV inversion.
    Returns 0.0 when IV cannot be found (expired, zero quotes, etc.)
    """
    g, _, _ = greeks_from_mid(bid, ask, S, K, T, r, flag)
    return g


def dte_to_t(expiry_str: str, today_str: str | None = None) -> float:
    """
    Convert an ISO date string ('YYYY-MM-DD') to T in years.
    Uses calendar days / 365; minimum is 1 trading hour to avoid T=0 at expiry day.
    """
    from datetime import date
    expiry = date.fromisoformat(expiry_str)
    today  = date.fromisoformat(today_str) if today_str else date.today()
    days   = (expiry - today).days
    if days < 0:
        return 0.0
    # Floor at ~1 hour of trading time so 0DTE gamma stays finite until close
    return max(days / 365.0, 1.0 / (365.0 * 7.0))
