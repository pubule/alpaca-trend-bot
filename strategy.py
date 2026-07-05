import math
from dataclasses import dataclass

import config as config_module


def load_rules(path: str = "rules.json") -> dict:
    return config_module.load_rules(path)


@dataclass
class EntrySignal:
    symbol: str
    entry_price: float
    stop_price: float
    risk_per_share: float


def check_entry_conditions(candidate: dict, rules: dict) -> EntrySignal | None:
    price = candidate["price"]
    prev_high = candidate["prev_high"]
    low_of_day = candidate["low_of_day"]

    if rules["filters"]["require_above_prev_day_high"] and price <= prev_high:
        return None

    limit_offset_pct = rules["entry"]["limit_offset_pct"]
    entry_price = price * (1 + limit_offset_pct / 100)

    stop_pct = rules["stop"]["initial_stop_pct_below_low_of_day"]
    stop_price = low_of_day * (1 - stop_pct / 100)

    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return None

    return EntrySignal(
        symbol=candidate["symbol"],
        entry_price=entry_price,
        stop_price=stop_price,
        risk_per_share=risk_per_share,
    )


def position_size(equity: float, buying_power: float, risk_per_share: float, entry_price: float, rules: dict) -> int:
    if risk_per_share <= 0 or entry_price <= 0:
        return 0

    risk_dollars = equity * rules["risk"]["risk_pct_per_trade"] / 100
    qty_by_risk = risk_dollars / risk_per_share

    max_position_dollars = equity * rules["risk"]["max_position_pct_of_account"] / 100
    qty_by_max_position = max_position_dollars / entry_price

    qty_by_buying_power = buying_power / entry_price

    qty = min(qty_by_risk, qty_by_max_position, qty_by_buying_power)
    return max(int(math.floor(qty)), 0)


def compute_stage(position: dict, current_price: float, recent_bars, rules: dict) -> tuple[str, float, bool]:
    entry_price = position["entry_price"]
    risk_per_share = position["risk_per_share"]
    stage = position["stage"]
    stop = position["current_stop"]
    should_fire_partial = False

    r_multiple = (current_price - entry_price) / risk_per_share

    partial_r = rules["targets"]["partial_r_multiple"]
    breakeven_r = rules["targets"]["breakeven_r_multiple"]
    trail_lookback = rules["targets"]["trail_lookback_bars"]

    if stage == "none" and r_multiple >= partial_r:
        stage = "partial_done"
        should_fire_partial = True

    if stage == "partial_done" and r_multiple >= breakeven_r:
        stage = "breakeven"
        stop = max(stop, entry_price)

    if stage in ("breakeven", "trailing") and recent_bars is not None and len(recent_bars["low"]) >= trail_lookback:
        swing_low = float(min(recent_bars["low"][-trail_lookback:]))
        stop = max(stop, swing_low)
        stage = "trailing"

    return stage, stop, should_fire_partial


if __name__ == "__main__":
    rules = {
        "targets": {
            "partial_r_multiple": 0.75,
            "breakeven_r_multiple": 1.0,
            "trail_lookback_bars": 3,
        }
    }

    position = {
        "entry_price": 100.0,
        "current_stop": 99.0,
        "risk_per_share": 1.0,
        "stage": "none",
    }

    stage, stop, fired = compute_stage(position, 100.75, None, rules)
    assert stage == "partial_done", stage
    assert fired is True
    assert stop == 99.0

    position["stage"] = stage
    position["current_stop"] = stop

    stage, stop, fired = compute_stage(position, 101.00, None, rules)
    assert stage == "breakeven", stage
    assert stop == 100.0, stop
    assert fired is False

    position["stage"] = stage
    position["current_stop"] = stop

    # Trailing: swing low (min of lookback window) above current stop ratchets it up.
    stage, stop, _ = compute_stage(position, 102.0, {"low": [100.2, 100.4, 100.6]}, rules)
    assert stage == "trailing", stage
    assert stop == 100.2, stop

    position["stage"] = stage
    position["current_stop"] = stop

    # A later, lower swing low must NOT pull the stop back down (ratchet only up).
    stage, stop, _ = compute_stage(position, 102.5, {"low": [99.0, 99.5, 100.0]}, rules)
    assert stage == "trailing", stage
    assert stop == 100.2, stop

    print("OK")
