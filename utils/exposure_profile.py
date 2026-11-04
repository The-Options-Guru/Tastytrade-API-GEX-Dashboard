"""Snapshot exposure estimates; no trade-direction or dealer-position inference."""
import math

import pandas as pd

from utils.gex_calculator import parse_option_symbol


PROFILE_COLUMNS = [
    "strike", "call_oi_gex", "put_oi_gex", "net_oi_gex",
    "call_volume_gex", "put_volume_gex", "net_volume_gex",
    "call_volume", "put_volume",
]


def _nonnegative_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def build_exposure_profile(option_data, spot_price):
    """Dollar gamma notional per 1% spot move, using a call-positive/put-negative convention."""
    spot = _nonnegative_number(spot_price)
    if not spot:
        raise ValueError("Exposure profile requires a finite, positive underlying price.")

    rows = []
    for symbol, data in option_data.items():
        parsed = parse_option_symbol(symbol)
        if not parsed:
            continue
        side = "call" if parsed["type"] == "C" else "put"
        gamma = _nonnegative_number(data.get("gamma"))
        oi = _nonnegative_number(data.get("oi"))
        volume = _nonnegative_number(data.get("volume"))
        # Delta-notional change for a 1% move: gamma * contracts * 100 * spot^2 * 0.01.
        factor = gamma * 100 * spot * spot * 0.01 if gamma is not None else None
        rows.append({
            "strike": parsed["strike"],
            "side": side,
            "oi_gex": factor * oi if factor is not None and oi is not None else None,
            "volume_gex": (
                factor * volume if factor is not None and volume is not None else None
            ),
            "volume": volume,
        })
    if not rows:
        return pd.DataFrame(columns=PROFILE_COLUMNS)

    # A missing observation must not become a measured zero.
    frame = pd.DataFrame(rows).groupby(["strike", "side"])[["oi_gex", "volume_gex", "volume"]].agg(
        lambda values: values.sum(min_count=len(values))
    ).unstack("side")
    frame.columns = [f"{side}_{measure}" for measure, side in frame.columns]
    frame = frame.reset_index().reindex(columns=PROFILE_COLUMNS)
    frame["net_oi_gex"] = frame["call_oi_gex"] - frame["put_oi_gex"]
    frame["net_volume_gex"] = frame["call_volume_gex"] - frame["put_volume_gex"]
    return frame[PROFILE_COLUMNS].sort_values("strike", ascending=False).reset_index(drop=True)


def get_wall_levels(profile, spot_price):
    """Peak side-specific OI gamma in the selected range; ties favor nearest spot, then lower strike."""
    spot = _nonnegative_number(spot_price)
    if not spot:
        raise ValueError("Wall levels require a finite, positive underlying price.")
    levels = {"call_wall": None, "put_wall": None, "atm_strike": None}
    if profile.empty:
        return levels
    levels["atm_strike"] = min(profile["strike"], key=lambda strike: (abs(strike - spot), strike))
    for side in ("call", "put"):
        column = f"{side}_oi_gex"
        candidates = profile.loc[profile[column].notna() & (profile[column] > 0)]
        if not candidates.empty:
            peak = candidates[column].max()
            strike = min(
                candidates.loc[candidates[column] == peak, "strike"],
                key=lambda value: (abs(value - spot), value),
            )
            levels[f"{side}_wall"] = {"strike": strike, "gex": peak}
    return levels


def get_open_to_wall_context(open_price, last_price, levels, comparable=True):
    """Endpoint distances only: no inference about the intervening path or next move."""
    opening, latest = _nonnegative_number(open_price), _nonnegative_number(last_price)
    result = {"direction": "Unavailable", "change": None, "pct": None, "nearest": "Unavailable", "approach": {}}
    if not comparable or not opening or not latest:
        return result
    change = latest - opening
    result.update(
        direction="Up" if change > 0 else "Down" if change < 0 else "Flat",
        change=change, pct=change / opening * 100,
    )
    distances = {}
    for side in ("call", "put"):
        wall = levels.get(f"{side}_wall")
        strike = _nonnegative_number(wall.get("strike")) if wall else None
        if strike:
            distance = abs(latest - strike)
            result["approach"][side] = distance - abs(opening - strike)
            distances[side] = distance
    if len(distances) == 2:
        if math.isclose(distances["call"], distances["put"], abs_tol=1e-9):
            result["nearest"] = "Equidistant"
        else:
            result["nearest"] = "Call wall" if distances["call"] < distances["put"] else "Put wall"
    return result


def calculate_max_pain(contracts, option_data):
    """Minimize intrinsic payout at listed strikes, only with complete listed-contract OI."""
    valid = {
        symbol: _nonnegative_number(option_data.get(symbol, {}).get("oi"))
        for symbol in contracts
    }
    observed = sum(oi is not None for oi in valid.values())
    result = {"strikes": [], "payout": None, "observed": observed, "expected": len(contracts)}
    if not contracts or observed != len(contracts):
        result["reason"] = "Full-chain open interest is incomplete."
        return result
    rows = {}
    for symbol, contract in contracts.items():
        strike = contract["strike"]
        rows.setdefault(strike, {"call": 0.0, "put": 0.0})
        rows[strike][contract["side"]] += valid[symbol] * contract["multiplier"]
    if not any(row["call"] or row["put"] for row in rows.values()):
        result["reason"] = "All listed open interest is zero; no informative max-pain level."
        return result

    remaining_puts = sum(row["put"] for row in rows.values())
    remaining_put_strikes = sum(strike * row["put"] for strike, row in rows.items())
    calls = call_strikes = 0.0
    payouts = {}
    for strike, row in sorted(rows.items()):
        calls += row["call"]
        call_strikes += strike * row["call"]
        remaining_puts -= row["put"]
        remaining_put_strikes -= strike * row["put"]
        payouts[strike] = (
            strike * calls - call_strikes + remaining_put_strikes - strike * remaining_puts
        )
    minimum = min(payouts.values())
    result["strikes"] = [
        strike for strike, payout in payouts.items()
        if math.isclose(payout, minimum, rel_tol=1e-12, abs_tol=1e-6)
    ]
    result["payout"] = minimum
    return result


def get_wall_bias(profile, spot_price, price_is_fallback=False):
    """An unvalidated snapshot breakout rule, not a closing-price forecast."""
    levels = get_wall_levels(profile, spot_price)
    if (
        price_is_fallback or profile.empty
        or profile[["call_oi_gex", "put_oi_gex"]].isna().any().any()
        or not levels["call_wall"] or not levels["put_wall"]
    ):
        return {"bias": "Unavailable", "lower": None, "upper": None}
    lower, upper = sorted(levels[f"{side}_wall"]["strike"] for side in ("call", "put"))
    bias = "Bullish" if spot_price > upper else "Bearish" if spot_price < lower else "Neutral"
    return {"bias": bias, "lower": lower, "upper": upper}
