import logging

import config as config_module
from broker import Broker
from universe import get_sp500_symbols

logger = logging.getLogger(__name__)


def spy_regime_ok(df, rules: dict) -> bool:
    """True if regime filter is off, SPY bars are unavailable (fail open,
    logged), or SPY's prior close is above its SMA. False blocks all entries."""
    if not rules["filters"].get("require_spy_above_sma200", False):
        return True
    try:
        spy_closes = df.loc["SPY"].sort_index()["close"]
    except KeyError:
        logger.warning("SPY bars missing; regime filter skipped")
        return True
    sma_period = rules["filters"]["sma_period_days"]
    if len(spy_closes) <= sma_period:
        return True
    spy_prev_close = float(spy_closes.iloc[-2])
    spy_sma = float(spy_closes.iloc[-(sma_period + 1):-1].mean())
    if spy_prev_close <= spy_sma:
        logger.info(
            "Regime filter: SPY prev close %.2f <= SMA%d %.2f — no long entries",
            spy_prev_close, sma_period, spy_sma,
        )
        return False
    return True


def filter_candidates(df, symbols: list[str], rules: dict) -> list[dict]:
    """Daily-bar gap/quality/regime filters shared by the live scanner and the
    backtester. `df` is a get_daily_bars()-shaped frame; the *last* row per
    symbol is treated as today's (still-forming) session bar and the row
    before it as the prior completed trading day. Does NOT apply the news
    catalyst filter (that needs a live/point-in-time news call) or top_n
    truncation — callers do that.
    """
    if not spy_regime_ok(df, rules):
        return []

    min_gap_pct = rules["filters"]["min_gap_pct"]
    sma_period = rules["filters"]["sma_period_days"]
    require_above_prev_high = rules["filters"]["require_above_prev_day_high"]
    require_prev_close_above_sma = rules["filters"]["require_prev_close_above_sma200"]
    min_price = rules["filters"].get("min_price", 0.0)
    min_avg_dollar_volume = rules["filters"].get("min_avg_dollar_volume", 0.0)

    candidates = []
    for symbol in symbols:
        try:
            sym_df = df.loc[symbol]
        except KeyError:
            continue
        if len(sym_df) < 2:
            continue

        sym_df = sym_df.sort_index()
        today_bar = sym_df.iloc[-1]
        prev_bar = sym_df.iloc[-2]

        prev_close = float(prev_bar["close"])
        prev_high = float(prev_bar["high"])
        price = float(today_bar["close"])
        today_open = float(today_bar["open"])
        low_of_day = float(today_bar["low"])

        if prev_close <= 0:
            continue
        gap_pct = (today_open - prev_close) / prev_close * 100

        closes = sym_df["close"]
        sma_200 = float(closes.iloc[-(sma_period + 1):-1].mean()) if len(closes) > sma_period else None

        if price < min_price:
            continue
        if min_avg_dollar_volume > 0:
            # 20-day avg dollar volume from the already-fetched daily bars
            recent = sym_df.iloc[-21:-1] if len(sym_df) > 21 else sym_df.iloc[:-1]
            avg_dollar_volume = float((recent["close"] * recent["volume"]).mean())
            if avg_dollar_volume < min_avg_dollar_volume:
                continue
        if gap_pct < min_gap_pct:
            continue
        # Optional upper bound: enables gap-DOWN strategies (e.g. max_gap_pct
        # -2.0 = must gap down at least 2%; pair with a negative min_gap_pct).
        max_gap_pct = rules["filters"].get("max_gap_pct")
        if max_gap_pct is not None and gap_pct > max_gap_pct:
            continue
        if require_above_prev_high and price <= prev_high:
            continue
        if require_prev_close_above_sma:
            if sma_200 is None or prev_close <= sma_200:
                continue

        candidates.append(
            {
                "symbol": symbol,
                "gap_pct": gap_pct,
                "prev_close": prev_close,
                "prev_high": prev_high,
                "sma_200": sma_200,
                "price": price,
                "low_of_day": low_of_day,
                "today_open": today_open,
            }
        )

    # Gap-up strategies want the biggest gap first; gap-down (max_gap_pct
    # negative) want the deepest dip first.
    gap_down_mode = (rules["filters"].get("max_gap_pct") or 0) < 0
    candidates.sort(key=lambda c: c["gap_pct"], reverse=not gap_down_mode)
    return candidates


def top_gappers(broker=None, symbols=None, rules=None, top_n=None) -> list[dict]:
    """Scan the universe for gapping candidates, apply the news catalyst
    filter (live news call), and return the top N by gap size."""
    cfg = config_module.load_config()
    if rules is None:
        rules = config_module.load_rules(cfg["paths"]["rules_path"])
    if top_n is None:
        top_n = cfg["scanner"]["top_n_gappers"]
    if broker is None:
        config_module.load_env()
        broker = Broker(
            config_module.get_required_env("ALPACA_API_KEY"),
            config_module.get_required_env("ALPACA_API_SECRET"),
            paper=cfg["alpaca"]["paper"],
            data_feed=cfg["alpaca"]["data_feed"],
        )
    if symbols is None:
        symbols = get_sp500_symbols(
            cache_path=cfg["scanner"]["universe_cache_path"],
            max_age_days=cfg["scanner"]["universe_cache_max_age_days"],
        )

    if not symbols:
        # Silent-empty-universe would look identical to "no gappers today" —
        # raise instead so bot.py's error path alerts via Telegram.
        raise RuntimeError(
            "Symbol universe is empty (SP500 fetch failed and no cache available); refusing to scan"
        )

    lookback_days = cfg["scanner"]["daily_bar_lookback_days"]
    require_spy_regime = rules["filters"].get("require_spy_above_sma200", False)
    fetch_symbols = list(symbols)
    if require_spy_regime and "SPY" not in fetch_symbols:
        fetch_symbols.append("SPY")
    df = broker.get_daily_bars(fetch_symbols, lookback_days)

    candidates = filter_candidates(df, symbols, rules)

    if rules["filters"].get("require_news_catalyst", False) and candidates:
        try:
            news_symbols = broker.get_news_symbols(
                [c["symbol"] for c in candidates],
                hours_back=rules["filters"].get("news_lookback_hours", 24),
            )
            candidates = [c for c in candidates if c["symbol"] in news_symbols]
        except Exception:
            logger.warning("News catalyst fetch failed; filter skipped this cycle", exc_info=True)

    return candidates[:top_n]
