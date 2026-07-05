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
    notify_eod_summary,
    notify_error,
    notify_exit,
    notify_partial,
    notify_risk_halt,
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

        stop_order_id_now = position["current_stop_order_id"]

        if should_fire_partial:
            partial_qty = max(
                int(position["initial_qty"] * rules["targets"]["partial_exit_pct_of_position"] / 100),
                1,
            )
            partial_qty = min(partial_qty, position["qty"])
            remaining_qty = position["qty"] - partial_qty

            # Cancel resting stop first: it reserves the full qty and would
            # otherwise reject the partial sell with "insufficient qty".
            if stop_order_id_now:
                try:
                    broker.cancel_order(stop_order_id_now)
                    state.remove_pending_order(conn, stop_order_id_now)
                except Exception:
                    logger.exception("Failed to cancel stop order before partial for %s", symbol)
                stop_order_id_now = None

            order = broker.submit_market_order(symbol, partial_qty, side="sell")
            state.upsert_pending_order(
                conn,
                {"order_id": order.id, "symbol": symbol, "purpose": "partial_exit", "qty": partial_qty},
            )

            # Re-establish a stop for the remaining qty at the (possibly
            # updated) stop price so the position is never left unprotected.
            if remaining_qty > 0:
                try:
                    new_stop_order = broker.submit_stop_order(symbol, remaining_qty, new_stop, side="sell")
                    stop_order_id_now = new_stop_order.id
                    state.upsert_pending_order(
                        conn,
                        {"order_id": new_stop_order.id, "symbol": symbol, "purpose": "stop", "qty": remaining_qty},
                    )
                except Exception:
                    logger.exception("Failed to re-submit stop after partial for %s", symbol)

            updated = dict(position)
            updated["current_stop_order_id"] = stop_order_id_now
            updated["current_stop"] = new_stop
            updated["stage"] = new_stage
            state.upsert_position(conn, updated)

        elif new_stop != position["current_stop"]:
            try:
                updated = dict(position)
                if stop_order_id_now:
                    replaced_order = broker.replace_stop_order(stop_order_id_now, new_stop)
                    state.remove_pending_order(conn, stop_order_id_now)
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
                else:
                    # Stop order id missing (e.g. consumed by a partial this
                    # cycle) — submit a fresh stop instead of replacing.
                    new_stop_order = broker.submit_stop_order(symbol, position["qty"], new_stop, side="sell")
                    updated["current_stop_order_id"] = new_stop_order.id
                    state.upsert_pending_order(
                        conn,
                        {"order_id": new_stop_order.id, "symbol": symbol, "purpose": "stop", "qty": position["qty"]},
                    )
                updated["current_stop"] = new_stop
                updated["stage"] = new_stage
                state.upsert_position(conn, updated)
            except Exception:
                logger.exception("Failed to replace stop order for %s", symbol)
        elif new_stage != position["stage"]:
            updated = dict(position)
            updated["stage"] = new_stage
            state.upsert_position(conn, updated)


# ponytail: module-level halt-notify dedup; restart re-notifies once, harmless
_halt_notified = {}


def _risk_guard_check(conn, cfg, equity: float, now_et: datetime) -> tuple[bool, str, str]:
    """Returns (entries_allowed, kind, detail). Open positions are ALWAYS managed;
    this only gates new entries."""
    guard = cfg.get("risk_guard", {})
    if not guard:
        return True, "", ""

    today_iso = now_et.strftime("%Y-%m-%d")
    month_iso = now_et.strftime("%Y-%m")

    cutoff = guard.get("no_new_entries_after_et")
    if cutoff and now_et.time() >= datetime.strptime(cutoff, "%H:%M").time():
        return False, "late_entry", f"past {cutoff} ET"

    max_daily = guard.get("max_daily_loss_pct")
    if max_daily:
        daily_pnl = state.realized_pnl_since(conn, today_iso)
        if daily_pnl <= -(max_daily / 100) * equity:
            return False, "daily", f"daily realized PnL {daily_pnl:.2f} <= -{max_daily}% of equity"

    max_monthly = guard.get("max_monthly_loss_pct")
    if max_monthly:
        monthly_pnl = state.realized_pnl_since(conn, month_iso + "-01")
        if monthly_pnl <= -(max_monthly / 100) * equity:
            return False, "monthly", f"monthly realized PnL {monthly_pnl:.2f} <= -{max_monthly}% of equity"

    max_trades = guard.get("max_trades_per_day")
    if max_trades and state.entries_today(conn, today_iso) >= max_trades:
        return False, "max_trades", f"{max_trades} trades reached today"

    return True, "", ""


def _scan_and_enter(broker: Broker, conn, cfg, rules, telegram_token, telegram_chat_id,
                    equity: float, buying_power: float):
    candidates = top_gappers(broker=broker, rules=rules, top_n=cfg["scanner"]["top_n_gappers"])

    base_risk_pct = rules["risk"]["risk_pct_per_trade"]
    risk_pct = strategy.effective_risk_pct(
        base_risk_pct, state.consecutive_losses(conn), cfg.get("risk_guard", {})
    )
    if risk_pct != base_risk_pct:
        logger.info("Risk halved to %.2f%% after consecutive losses", risk_pct)

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

        qty = strategy.position_size(
            equity, buying_power, signal.risk_per_share, signal.entry_price, rules, risk_pct=risk_pct
        )
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


def _force_close_and_eod(broker: Broker, conn, cfg, now_et, telegram_token, telegram_chat_id):
    _force_close_all(broker, conn, telegram_token, telegram_chat_id)
    if not telegram_token:
        return
    today_iso = now_et.strftime("%Y-%m-%d")
    month_iso = now_et.strftime("%Y-%m")
    closed_today = [t for t in state.get_closed_trades(conn) if t["exit_time"] >= today_iso]
    mtd_pnl = state.realized_pnl_since(conn, month_iso + "-01")
    start_eq = state.month_start_equity(conn, month_iso)
    target = cfg.get("risk_guard", {}).get("monthly_target_pct", 2.0)
    mtd_line = (
        f"MTD: {mtd_pnl / start_eq * 100:+.2f}% vs target {target}%"
        if start_eq
        else f"MTD: {mtd_pnl:+.2f} USD (no equity log yet)"
    )
    notify_eod_summary(telegram_token, telegram_chat_id, closed_today, mtd_line)


def run_force_close_pass(broker: Broker, conn, cfg: dict) -> None:
    """Dedicated 15:55 wake: cycle boundaries (:00/:30) never land on the
    force-close time, so without this pass positions would ride into the close."""
    now_et = datetime.now(ET)
    key = now_et.strftime("%Y-%m-%d") + "-force-close"
    existing = state.get_cycle(conn, key)
    if existing is not None and existing["status"] == "ok":
        return

    cycle_id = state.start_cycle(conn, key)
    config_module.load_env()
    telegram_token = config_module.get_required_env("TELEGRAM_BOT_TOKEN") if cfg["telegram"]["enabled"] else None
    telegram_chat_id = config_module.get_required_env("TELEGRAM_CHAT_ID") if cfg["telegram"]["enabled"] else None

    try:
        clock = broker.get_clock()
        if not clock.is_open:
            state.complete_cycle(conn, cycle_id, "market_closed")
            return
        rules = config_module.load_rules(cfg["paths"]["rules_path"])
        _reconcile_pending_orders(broker, conn, cfg, rules, telegram_token, telegram_chat_id)
        _force_close_and_eod(broker, conn, cfg, now_et, telegram_token, telegram_chat_id)
        state.complete_cycle(conn, cycle_id, "ok")
    except Exception as e:
        logger.exception("Force-close pass failed")
        state.complete_cycle(conn, cycle_id, "error", notes=str(e))
        if telegram_token:
            notify_error(telegram_token, telegram_chat_id, str(e))


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

        account = broker.get_account()
        equity = float(account.equity)
        buying_power = float(account.buying_power)
        state.log_equity(conn, now_et.strftime("%Y-%m-%d"), equity)

        _reconcile_pending_orders(broker, conn, cfg, rules, telegram_token, telegram_chat_id)
        _manage_open_positions(broker, conn, cfg, rules, telegram_token, telegram_chat_id)

        force_close_time = datetime.strptime(cfg["schedule"]["force_close_time_et"], "%H:%M").time()
        if now_et.time() >= force_close_time:
            _force_close_and_eod(broker, conn, cfg, now_et, telegram_token, telegram_chat_id)
        else:
            allowed, halt_kind, halt_detail = _risk_guard_check(conn, cfg, equity, now_et)
            if allowed:
                _scan_and_enter(
                    broker, conn, cfg, rules, telegram_token, telegram_chat_id, equity, buying_power
                )
            else:
                logger.info("Risk guard halt (%s): %s", halt_kind, halt_detail)
                halt_key = (now_et.strftime("%Y-%m-%d"), halt_kind)
                if telegram_token and halt_kind in ("daily", "monthly") and _halt_notified.get(halt_key) is None:
                    _halt_notified[halt_key] = True
                    notify_risk_halt(telegram_token, telegram_chat_id, halt_kind, halt_detail)

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
    force_close_t = datetime.strptime(cfg["schedule"]["force_close_time_et"], "%H:%M").time()
    while True:
        clock = broker.get_clock()
        if not clock.is_open:
            sleep_seconds = (clock.next_open - datetime.now(clock.next_open.tzinfo)).total_seconds()
            time.sleep(max(sleep_seconds, 1))
            continue

        now_et = datetime.now(ET)
        if now_et.time() >= force_close_t:
            run_force_close_pass(broker, conn, cfg)
        else:
            run_cycle(broker, conn, rules, cfg)

        # Wake at the next cycle boundary, or earlier at force-close time —
        # boundaries (:00/:30) never land on 15:55, so it needs its own wake.
        now_et = datetime.now(ET)
        boundary = next_boundary(now_et, cycle_minutes)
        force_close_dt = now_et.replace(
            hour=force_close_t.hour, minute=force_close_t.minute, second=0, microsecond=0
        )
        wake = min(boundary, force_close_dt) if force_close_dt > now_et else boundary
        sleep_seconds = (wake - now_et).total_seconds()
        time.sleep(max(sleep_seconds, 1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    main(once=args.once)
