import argparse
import hashlib
import logging
import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config as config_module
import scanner
import state
import strategy
from broker import Broker
from dashboard import generate_html
from universe import get_sp500_symbols

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
CACHE_DIR = "cache"

# ponytail: fills assume the stop/partial price is hit exactly (no slippage
# model beyond that). Paper trading is the real judge; see README.


def _cache_path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    return os.path.join(CACHE_DIR, f"{digest}.pkl")


def _cache_get(key: str):
    # pickle is safe here: cache/ only ever contains files this same process
    # wrote (Alpaca bars DataFrames), never data from an untrusted source.
    path = _cache_path(key)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    return None


def _cache_set(key: str, value) -> None:
    with open(_cache_path(key), "wb") as f:
        pickle.dump(value, f)


def _fetch_universe_daily_bars(broker, symbols, start, end):
    key = f"daily::{start.date()}::{end.date()}::{','.join(sorted(symbols))}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    df = broker.get_daily_bars(symbols, lookback_days=(end - start).days + 1, end=end)
    _cache_set(key, df)
    return df


def _fetch_intraday_bars(broker, symbol, day, day_open_utc, day_close_utc):
    key = f"intraday::{symbol}::{day.isoformat()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    bars = broker.get_intraday_bars(symbol, start=day_open_utc, end=day_close_utc)
    try:
        bars = bars.loc[symbol].sort_index()
    except (KeyError, TypeError):
        bars = bars.sort_index()
    _cache_set(key, bars)
    return bars


def _prepare_symbol_frames(full_df, symbols):
    """Per-symbol sorted daily bars + a parallel array of ET trading dates,
    precomputed once so each day's lookup is a fast searchsorted."""
    frames = {}
    for sym in symbols:
        try:
            sub = full_df.loc[sym].sort_index()
        except KeyError:
            continue
        if sub.empty:
            continue
        idx = sub.index
        dates = (idx.tz_convert("America/New_York") if idx.tz is not None else idx).date
        frames[sym] = (sub, np.array(dates))
    return frames


def _build_day_df(frames, day):
    """One synthetic 'today' row per symbol built ONLY from today's real open
    (known and safe at market open) plus all prior real daily bars (also
    safe). High/low/close of 'today' are placeholders (=open) — scanner.
    filter_candidates only reads them off the synthetic row for the parts
    that are legitimately point-in-time-safe (gap vs prev close, open vs
    prev high); the true intraday price/low_of_day are supplied later, bar
    by bar, during cycle simulation."""
    pieces = {}
    for sym, (sub, dates) in frames.items():
        idx = int(np.searchsorted(dates, day))
        if idx >= len(dates) or dates[idx] != day or idx == 0:
            continue
        hist = sub.iloc[:idx]
        today_ts = sub.index[idx]
        today_open = float(sub.iloc[idx]["open"])
        synthetic = pd.DataFrame(
            [{"open": today_open, "high": today_open, "low": today_open, "close": today_open, "volume": 0.0}],
            index=[today_ts],
        )
        pieces[sym] = pd.concat([hist, synthetic])
    if not pieces:
        return None
    return pd.concat(pieces, names=["symbol"])


def _vol_multiplier_for_day(frames, day, guard: dict) -> float:
    threshold = guard.get("high_vol_annualized_pct")
    if not threshold or "SPY" not in frames:
        return 1.0
    spy_sub, spy_dates = frames["SPY"]
    idx = int(np.searchsorted(spy_dates, day))
    closes = spy_sub.iloc[max(0, idx - 11):idx]["close"].tolist()
    return strategy.vol_risk_multiplier(closes, threshold)


def _simulate_day(broker, conn, cfg, rules, day, day_open_utc, day_close_utc,
                   candidates, equity, vol_multiplier, use_news) -> float:
    today_iso = day.isoformat()
    month_iso = day.strftime("%Y-%m")
    cycle_minutes = cfg["schedule"]["cycle_minutes"]
    force_close_t = datetime.strptime(cfg["schedule"]["force_close_time_et"], "%H:%M").time()
    guard = cfg.get("risk_guard", {})
    base_risk_pct = rules["risk"]["risk_pct_per_trade"]

    if use_news and rules["filters"].get("require_news_catalyst", False) and candidates:
        try:
            articles = broker.get_news_articles(
                [c["symbol"] for c in candidates],
                start=day_open_utc - timedelta(hours=rules["filters"].get("news_lookback_hours", 24)),
                end=day_open_utc,
            )
            candidates = [c for c in candidates if c["symbol"] in articles]
            candidates = scanner.apply_llm_filter(candidates, articles, rules)
        except Exception:
            logger.warning("Backtest news fetch failed for %s; filter skipped", day, exc_info=True)

    if not candidates:
        return equity

    intraday = {}
    for c in candidates:
        try:
            intraday[c["symbol"]] = _fetch_intraday_bars(broker, c["symbol"], day, day_open_utc, day_close_utc)
        except Exception:
            logger.warning("Failed to fetch intraday bars for %s on %s", c["symbol"], day, exc_info=True)

    positions: dict[str, dict] = {}
    cursor = day_open_utc

    def _close(symbol, pos, exit_price, exit_reason, when):
        nonlocal equity
        r_multiple = (exit_price - pos["entry_price"]) / pos["risk_per_share"]
        state.record_closed_trade(conn, {
            "symbol": symbol, "entry_price": pos["entry_price"], "exit_price": exit_price,
            "qty": pos["qty"], "entry_time": pos["entry_time"], "exit_time": when.isoformat(),
            "r_multiple": r_multiple, "exit_reason": exit_reason,
            "realized_pnl": (exit_price - pos["entry_price"]) * pos["qty"],
        })
        equity += (exit_price - pos["entry_price"]) * pos["qty"]

    while cursor <= day_close_utc:
        cursor_et = cursor.astimezone(ET)

        for symbol, pos in list(positions.items()):
            bars = intraday.get(symbol)
            if bars is None:
                continue
            so_far = bars[bars.index <= cursor]
            if so_far.empty:
                continue
            bar_low = float(so_far["low"].iloc[-1])
            if bar_low <= pos["current_stop"]:
                _close(symbol, pos, pos["current_stop"], "stop", cursor)
                del positions[symbol]
                continue

            current_price = float(so_far["close"].iloc[-1])
            recent = so_far.tail(rules["targets"]["trail_lookback_bars"])
            new_stage, new_stop, should_fire_partial = strategy.compute_stage(
                pos, current_price, {"low": recent["low"].tolist()}, rules
            )
            if should_fire_partial:
                partial_qty = min(
                    max(int(pos["initial_qty"] * rules["targets"]["partial_exit_pct_of_position"] / 100), 1),
                    pos["qty"],
                )
                r_multiple = (current_price - pos["entry_price"]) / pos["risk_per_share"]
                state.record_closed_trade(conn, {
                    "symbol": symbol, "entry_price": pos["entry_price"], "exit_price": current_price,
                    "qty": partial_qty, "entry_time": pos["entry_time"], "exit_time": cursor.isoformat(),
                    "r_multiple": r_multiple, "exit_reason": "partial",
                    "realized_pnl": (current_price - pos["entry_price"]) * partial_qty,
                })
                equity += (current_price - pos["entry_price"]) * partial_qty
                pos["qty"] -= partial_qty
                if pos["qty"] <= 0:
                    del positions[symbol]
                    continue
            pos["current_stop"] = new_stop
            pos["stage"] = new_stage

        if cursor_et.time() >= force_close_t:
            for symbol, pos in list(positions.items()):
                bars = intraday.get(symbol)
                so_far = bars[bars.index <= cursor] if bars is not None else None
                exit_price = float(so_far["close"].iloc[-1]) if so_far is not None and not so_far.empty else pos["entry_price"]
                _close(symbol, pos, exit_price, "force_close", cursor)
            break

        entries_allowed = True
        earliest = guard.get("no_entries_before_et")
        if earliest and cursor_et.time() < datetime.strptime(earliest, "%H:%M").time():
            entries_allowed = False
        latest = guard.get("no_new_entries_after_et")
        if entries_allowed and latest and cursor_et.time() >= datetime.strptime(latest, "%H:%M").time():
            entries_allowed = False
        if entries_allowed:
            max_daily = guard.get("max_daily_loss_pct")
            if max_daily and state.realized_pnl_since(conn, today_iso) <= -(max_daily / 100) * equity:
                entries_allowed = False
        if entries_allowed:
            max_monthly = guard.get("max_monthly_loss_pct")
            if max_monthly and state.realized_pnl_since(conn, month_iso + "-01") <= -(max_monthly / 100) * equity:
                entries_allowed = False
        if entries_allowed:
            max_trades = guard.get("max_trades_per_day")
            if max_trades and state.entries_today(conn, today_iso) >= max_trades:
                entries_allowed = False

        if entries_allowed:
            risk_pct = strategy.effective_risk_pct(
                base_risk_pct, state.consecutive_losses(conn), guard
            ) * vol_multiplier
            for c in candidates:
                symbol = c["symbol"]
                if symbol in positions or state.symbol_traded_today(conn, symbol, today_iso):
                    continue
                bars = intraday.get(symbol)
                if bars is None:
                    continue
                so_far = bars[bars.index <= cursor]
                if so_far.empty:
                    continue
                candidate_now = dict(c)
                candidate_now["price"] = float(so_far["close"].iloc[-1])
                candidate_now["low_of_day"] = float(so_far["low"].min())
                if rules["entry"].get("trigger") == "orb_30min":
                    orb_end = day_open_utc + timedelta(minutes=30)
                    orb_bars = bars[bars.index < orb_end]
                    if cursor < orb_end or orb_bars.empty:
                        continue  # opening range not complete yet
                    candidate_now["orb_high"] = float(orb_bars["high"].max())
                signal = strategy.check_entry_conditions(candidate_now, rules)
                if signal is None:
                    continue
                qty = strategy.position_size(
                    equity, equity, signal.risk_per_share, signal.entry_price, rules, risk_pct=risk_pct
                )
                if qty <= 0:
                    continue
                bar_high = float(so_far["high"].iloc[-1])
                if bar_high < signal.entry_price:
                    continue  # limit not reached this cycle; retry next cycle
                positions[symbol] = {
                    "entry_price": signal.entry_price, "qty": qty, "initial_qty": qty,
                    "entry_time": cursor.isoformat(), "current_stop": signal.stop_price,
                    "risk_per_share": signal.risk_per_share, "stage": "none",
                }

        cursor += timedelta(minutes=cycle_minutes)

    return equity


def run_backtest(start_date_str: str, end_date_str: str, use_news: bool,
                  starting_equity: float, db_path: str, universe: list[str] | None = None,
                  rules_path: str | None = None, config_path: str | None = None) -> None:
    cfg = config_module.load_config(config_path or "config.yaml")
    rules = config_module.load_rules(rules_path or cfg["paths"]["rules_path"])

    for ext in ("", "-wal", "-shm"):
        p = db_path + ext
        if os.path.exists(p):
            os.remove(p)
    conn = state.init_db(db_path)

    config_module.load_env()
    broker = Broker(
        config_module.get_required_env("ALPACA_API_KEY"),
        config_module.get_required_env("ALPACA_API_SECRET"),
        paper=cfg["alpaca"]["paper"],
        data_feed=cfg["alpaca"]["data_feed"],
    )

    symbols = universe or get_sp500_symbols(
        cache_path=cfg["scanner"]["universe_cache_path"],
        max_age_days=cfg["scanner"]["universe_cache_max_age_days"],
    )
    if not symbols:
        raise RuntimeError("Symbol universe is empty (SP500 fetch failed and no cache available); aborting backtest")
    fetch_symbols = list(symbols)
    if "SPY" not in fetch_symbols:
        fetch_symbols.append("SPY")

    start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

    calendar = broker.get_calendar(start_date, end_date)
    if not calendar:
        print("No trading days in range.")
        return

    lookback_days = cfg["scanner"]["daily_bar_lookback_days"]
    fetch_start = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=lookback_days + 5)
    fetch_end = datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
    full_daily_df = _fetch_universe_daily_bars(broker, fetch_symbols, fetch_start, fetch_end)
    frames = _prepare_symbol_frames(full_daily_df, fetch_symbols)

    equity = starting_equity
    guard = cfg.get("risk_guard", {})

    for i, day_info in enumerate(calendar):
        day = day_info.date
        day_open_utc = day_info.open.replace(tzinfo=ET).astimezone(timezone.utc)
        day_close_utc = day_info.close.replace(tzinfo=ET).astimezone(timezone.utc)

        state.log_equity(conn, day.isoformat(), equity)

        day_df = _build_day_df(frames, day)
        candidates = scanner.filter_candidates(day_df, symbols, rules) if day_df is not None else []

        if candidates:
            vol_multiplier = _vol_multiplier_for_day(frames, day, guard)
            equity = _simulate_day(
                broker, conn, cfg, rules, day, day_open_utc, day_close_utc,
                candidates, equity, vol_multiplier, use_news,
            )

        if (i + 1) % 20 == 0 or i == len(calendar) - 1:
            print(f"[{day}] {i + 1}/{len(calendar)} days — equity {equity:,.2f}")

    print(f"\nDone. Final equity: {equity:,.2f} (started {starting_equity:,.2f})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Backtest the Trend Join Long strategy on historical Alpaca data.")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--no-news", action="store_true", help="Disable the news catalyst filter for this run")
    parser.add_argument("--starting-equity", type=float, default=100000.0)
    parser.add_argument("--db", default="backtest.db")
    parser.add_argument("--universe", help="Comma-separated symbol override, e.g. AAPL,NVDA,MSFT (for quick tests)")
    parser.add_argument("--rules", help="Alternate rules.json path (for strategy experiments)")
    parser.add_argument("--config", help="Alternate config.yaml path (for risk-guard experiments)")
    args = parser.parse_args()

    universe = args.universe.split(",") if args.universe else None
    run_backtest(args.start, args.end, use_news=not args.no_news,
                 starting_equity=args.starting_equity, db_path=args.db, universe=universe,
                 rules_path=args.rules, config_path=args.config)

    cfg = config_module.load_config()
    generate_html(db_path=args.db, output_path="backtest.html")
    print("Report written to backtest.html")
    sys.exit(0)
