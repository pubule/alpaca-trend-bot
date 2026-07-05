import argparse
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config as config_module
import state
import strategy
from broker import Broker
from notifier import (
    notify_cycle_summary,
    notify_entry,
    notify_error,
    notify_exit,
    notify_partial,
)
from scanner import top_gappers

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def next_boundary(now_et: datetime, cycle_minutes: int) -> datetime:
    base = now_et.replace(second=0, microsecond=0)
    minutes_since_midnight = base.hour * 60 + base.minute
    next_slot = ((minutes_since_midnight // cycle_minutes) + 1) * cycle_minutes
    boundary = base.replace(hour=0, minute=0) + timedelta(minutes=next_slot)
    return boundary


def current_boundary(now_et: datetime, cycle_minutes: int) -> datetime:
    base = now_et.replace(second=0, microsecond=0)
    minutes_since_midnight = base.hour * 60 + base.minute
    slot = (minutes_since_midnight // cycle_minutes) * cycle_minutes
    return base.replace(hour=0, minute=0) + timedelta(minutes=slot)


def _order_filled_qty(order) -> float:
    return float(order.filled_qty or 0)


def _reconcile_pending_orders(broker: Broker, conn, cfg, rules, telegram_token, telegram_chat_id):
    for pending in state.get_pending_orders(conn):
        try:
            order = broker.get_order(pending["order_id"])
        except Exception:
            logger.exception("Failed to fetch order %s", pending["order_id"])
            continue

        status = order.status.value if hasattr(order.status, "value") else str(order.status)
        filled_qty = _order_filled_qty(order)
        filled_avg_price = float(order.filled_avg_price) if order.filled_avg_price else None

        state.upsert_pending_order(
            conn,
            {
                "order_id": pending["order_id"],
                "symbol": pending["symbol"],
                "purpose": pending["purpose"],
                "submitted_at": pending["submitted_at"],
                "qty": pending["qty"],
                "filled_qty": filled_qty,
                "filled_avg_price": filled_avg_price,
                "status": status,
                "meta_stop_price": pending["meta_stop_price"],
                "meta_risk_per_share": pending["meta_risk_per_share"],
            },
        )

        if status not in ("filled", "partially_filled") or filled_qty <= 0:
            continue

        symbol = pending["symbol"]
        purpose = pending["purpose"]

        if purpose == "entry":
            stop_price = pending["meta_stop_price"]
            risk_per_share = pending["meta_risk_per_share"]
            entry_price = filled_avg_price or (stop_price + risk_per_share)
            position = {
                "symbol": symbol,
                "entry_order_id": pending["order_id"],
                "qty": int(filled_qty),
                "initial_qty": int(filled_qty),
                "entry_price": entry_price,
                "entry_time": datetime.utcnow().isoformat(),
                "initial_stop": stop_price,
                "current_stop": stop_price,
                "current_stop_order_id": None,
                "risk_per_share": risk_per_share,
                "partial_target_price": entry_price
                + rules["targets"]["partial_r_multiple"] * risk_per_share,
                "breakeven_price": entry_price,
                "stage": "none",
            }
            state.upsert_position(conn, position)
            stop_order = broker.submit_stop_order(symbol, int(filled_qty), stop_price, side="sell")
            position["current_stop_order_id"] = stop_order.id
            state.upsert_position(conn, position)
            state.upsert_pending_order(
                conn,
                {"order_id": stop_order.id, "symbol": symbol, "purpose": "stop", "qty": int(filled_qty)},
            )
            state.remove_pending_order(conn, pending["order_id"])
            notify_entry(telegram_token, telegram_chat_id, symbol, int(filled_qty), position["entry_price"])

        elif purpose == "stop":
            position = state.get_position(conn, symbol)
            if position is None:
                state.remove_pending_order(conn, pending["order_id"])
                continue
            exit_price = filled_avg_price or position["current_stop"]
            r_multiple = (exit_price - position["entry_price"]) / position["risk_per_share"]
            state.record_closed_trade(
                conn,
                {
                    "symbol": symbol,
                    "entry_price": position["entry_price"],
                    "exit_price": exit_price,
                    "qty": position["qty"],
                    "entry_time": position["entry_time"],
                    "exit_time": datetime.utcnow().isoformat(),
                    "r_multiple": r_multiple,
                    "exit_reason": "stop",
                    "realized_pnl": (exit_price - position["entry_price"]) * position["qty"],
                },
            )
            state.delete_position(conn, symbol)
            state.remove_pending_order(conn, pending["order_id"])
            notify_exit(telegram_token, telegram_chat_id, symbol, exit_price, r_multiple, "stop")

        elif purpose == "partial_exit":
            position = state.get_position(conn, symbol)
            if position is None:
                state.remove_pending_order(conn, pending["order_id"])
                continue
            remaining_qty = position["qty"] - int(filled_qty)
            r_multiple = (
                (filled_avg_price or position["partial_target_price"]) - position["entry_price"]
            ) / position["risk_per_share"]
            if remaining_qty <= 0:
                state.record_closed_trade(
                    conn,
                    {
                        "symbol": symbol,
                        "entry_price": position["entry_price"],
                        "exit_price": filled_avg_price or position["partial_target_price"],
                        "qty": int(filled_qty),
                        "entry_time": position["entry_time"],
                        "exit_time": datetime.utcnow().isoformat(),
                        "r_multiple": r_multiple,
                        "exit_reason": "partial",
                        "realized_pnl": ((filled_avg_price or position["partial_target_price"]) - position["entry_price"]) * int(filled_qty),
                    },
                )
                state.delete_position(conn, symbol)
            else:
                updated = dict(position)
                updated["qty"] = remaining_qty
                state.upsert_position(conn, updated)
            state.remove_pending_order(conn, pending["order_id"])
            notify_partial(telegram_token, telegram_chat_id, symbol, filled_avg_price or 0.0, r_multiple)

        elif purpose == "force_close":
            position = state.get_position(conn, symbol)
            if position is None:
                state.remove_pending_order(conn, pending["order_id"])
                continue
            exit_price = filled_avg_price or position["current_stop"]
            r_multiple = (exit_price - position["entry_price"]) / position["risk_per_share"]
            state.record_closed_trade(
                conn,
                {
                    "symbol": symbol,
                    "entry_price": position["entry_price"],
                    "exit_price": exit_price,
                    "qty": position["qty"],
                    "entry_time": position["entry_time"],
                    "exit_time": datetime.utcnow().isoformat(),
                    "r_multiple": r_multiple,
                    "exit_reason": "force_close",
                    "realized_pnl": (exit_price - position["entry_price"]) * position["qty"],
                },
            )
            state.delete_position(conn, symbol)
            state.remove_pending_order(conn, pending["order_id"])
            notify_exit(telegram_token, telegram_chat_id, symbol, exit_price, r_multiple, "force_close")


def _manage_open_positions(broker: Broker, conn, cfg, rules, telegram_token, telegram_chat_id):
    for position in state.get_open_positions(conn):
        symbol = position["symbol"]
        try:
            bars = broker.get_intraday_bars(symbol, minutes_back=120)
            current_price = float(bars["close"].iloc[-1])
            recent_bars = bars.tail(rules["targets"]["trail_lookback_bars"])
        except Exception:
            logger.exception("Failed to fetch intraday bars for %s", symbol)
            continue

        new_stage, new_stop, should_fire_partial = strategy.compute_stage(
            dict(position), current_price, recent_bars, rules
        )

        if should_fire_partial:
            partial_qty = max(
                int(position["initial_qty"] * rules["targets"]["partial_exit_pct_of_position"] / 100),
                1,
            )
            partial_qty = min(partial_qty, position["qty"])
            order = broker.submit_market_order(symbol, partial_qty, side="sell")
            state.upsert_pending_order(
                conn,
                {"order_id": order.id, "symbol": symbol, "purpose": "partial_exit", "qty": partial_qty},
            )

        if new_stop != position["current_stop"]:
            try:
                updated = dict(position)
                if position["current_stop_order_id"]:
                    replaced_order = broker.replace_stop_order(position["current_stop_order_id"], new_stop)
                    state.remove_pending_order(conn, position["current_stop_order_id"])
                    state.upsert_pending_order(
                        conn,
                        {
                            "order_id": replaced_order.id,
                            "symbol": symbol,
                            "purpose": "stop",
                            "qty": position["qty"],
                        },
                    )
                    updated["current_stop_order_id"] = replaced_order.id
                updated["current_stop"] = new_stop
                updated["stage"] = new_stage
                state.upsert_position(conn, updated)
            except Exception:
                logger.exception("Failed to replace stop order for %s", symbol)
        elif new_stage != position["stage"]:
            updated = dict(position)
            updated["stage"] = new_stage
            state.upsert_position(conn, updated)


def _scan_and_enter(broker: Broker, conn, cfg, rules, telegram_token, telegram_chat_id):
    candidates = top_gappers(broker=broker, rules=rules, top_n=cfg["scanner"]["top_n_gappers"])
    account = broker.get_account()
    equity = float(account.equity)
    buying_power = float(account.buying_power)

    entries = []
    for candidate in candidates:
        symbol = candidate["symbol"]
        if state.get_position(conn, symbol) is not None:
            continue
        if state.get_pending_orders_for_symbol(conn, symbol, purpose="entry"):
            continue

        signal = strategy.check_entry_conditions(candidate, rules)
        if signal is None:
            continue

        qty = strategy.position_size(equity, buying_power, signal.risk_per_share, signal.entry_price, rules)
        if qty <= 0:
            continue

        order = broker.submit_limit_order(symbol, qty, side="buy", limit_price=signal.entry_price)
        state.upsert_pending_order(
            conn,
            {
                "order_id": order.id,
                "symbol": symbol,
                "purpose": "entry",
                "qty": qty,
                "meta_stop_price": signal.stop_price,
                "meta_risk_per_share": signal.risk_per_share,
            },
        )
        entries.append(symbol)

    notify_cycle_summary(telegram_token, telegram_chat_id, candidates, entries, [], [])


def _force_close_all(broker: Broker, conn, telegram_token, telegram_chat_id):
    for position in state.get_open_positions(conn):
        symbol = position["symbol"]
        if state.get_pending_orders_for_symbol(conn, symbol, purpose="force_close"):
            continue
        order = broker.close_position(symbol)
        state.upsert_pending_order(
            conn,
            {"order_id": order.id, "symbol": symbol, "purpose": "force_close", "qty": position["qty"]},
        )


def run_cycle(broker: Broker, conn, rules: dict, cfg: dict) -> None:
    now_et = datetime.now(ET)
    cycle_time = current_boundary(now_et, cfg["schedule"]["cycle_minutes"]).isoformat()

    existing = state.get_cycle(conn, cycle_time)
    if existing is not None and existing["status"] == "ok":
        logger.info("Cycle %s already completed; skipping", cycle_time)
        return

    cycle_id = state.start_cycle(conn, cycle_time)

    config_module.load_env()
    telegram_token = config_module.get_required_env("TELEGRAM_BOT_TOKEN") if cfg["telegram"]["enabled"] else None
    telegram_chat_id = config_module.get_required_env("TELEGRAM_CHAT_ID") if cfg["telegram"]["enabled"] else None

    try:
        clock = broker.get_clock()
        if not clock.is_open:
            state.complete_cycle(conn, cycle_id, "market_closed")
            return

        _reconcile_pending_orders(broker, conn, cfg, rules, telegram_token, telegram_chat_id)
        _manage_open_positions(broker, conn, cfg, rules, telegram_token, telegram_chat_id)

        force_close_time = datetime.strptime(cfg["schedule"]["force_close_time_et"], "%H:%M").time()
        if now_et.time() >= force_close_time:
            _force_close_all(broker, conn, telegram_token, telegram_chat_id)
        else:
            _scan_and_enter(broker, conn, cfg, rules, telegram_token, telegram_chat_id)

        state.complete_cycle(conn, cycle_id, "ok")
    except Exception as e:
        logger.exception("Cycle failed")
        state.complete_cycle(conn, cycle_id, "error", notes=str(e))
        if telegram_token:
            notify_error(telegram_token, telegram_chat_id, str(e))


def main(once: bool = False) -> None:
    logging.basicConfig(level=logging.INFO)
    config_module.load_env()
    cfg = config_module.load_config()
    rules = config_module.load_rules(cfg["paths"]["rules_path"])
    conn = state.init_db(cfg["paths"]["db_path"])

    broker = Broker(
        config_module.get_required_env("ALPACA_API_KEY"),
        config_module.get_required_env("ALPACA_API_SECRET"),
        paper=cfg["alpaca"]["paper"],
        data_feed=cfg["alpaca"]["data_feed"],
    )

    if once:
        run_cycle(broker, conn, rules, cfg)
        return

    cycle_minutes = cfg["schedule"]["cycle_minutes"]
    while True:
        clock = broker.get_clock()
        if not clock.is_open:
            sleep_seconds = (clock.next_open - datetime.now(clock.next_open.tzinfo)).total_seconds()
            time.sleep(max(sleep_seconds, 1))
            continue

        run_cycle(broker, conn, rules, cfg)

        now_et = datetime.now(ET)
        boundary = next_boundary(now_et, cycle_minutes)
        sleep_seconds = (boundary - now_et).total_seconds()
        time.sleep(max(sleep_seconds, 1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    main(once=args.once)
