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
        Implied volatility via Newton-Raphson.

    dte_to_t(expiry_str, today_str=None) -> float
        Convert 'YYYY-MM-DD' expiry to T in years.

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


def iv(mkt_price: float, S: float, K: float, T: float, r: float, flag: str,
       max_iter: int = 150, tol: float = 1e-7) -> float | None:
    """
    Implied volatility via Newton-Raphson.
    Returns sigma (annualised) or None if no convergent solution.
    """
    if T <= 0.0 or mkt_price <= 0.0:
        return None

    # Enforce no-arb floor so we never feed a negative time-value to the solver
    intrinsic = max(0.0, (S - K) if flag == 'c' else (K - S))
    effective = max(mkt_price, intrinsic + 1e-6)

    # Brenner-Subrahmanyam initial guess
    v = math.sqrt(2.0 * math.pi / T) * effective / S
    v = max(0.005, min(v, 8.0))

    for _ in range(max_iter):
        p    = _bs_price(S, K, T, r, v, flag)
        vega = S * _npdf(_d1(S, K, T, r, v)) * math.sqrt(T)
        if vega < 1e-12:
            break
        step = (p - effective) / vega
        v   -= step
        if v < 1e-6:
            v = 1e-6
        if abs(step) < tol:
            break

    return v if 1e-4 <= v <= 10.0 else None


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
