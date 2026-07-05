import logging

import config as config_module
from broker import Broker
from universe import get_sp500_symbols

logger = logging.getLogger(__name__)


def top_gappers(broker=None, symbols=None, rules=None, top_n=None) -> list[dict]:
    """Scan the universe for gapping candidates.

    Gap/price context comes from Alpaca daily bars: the *last* bar row per
    symbol is treated as today's (still-forming) session bar and the row
    before it as the prior completed trading day. `gap_pct` is computed from
    today's open vs. the prior day's close; `price` used for the
    above-prev-high filter is today's most recent close-so-far.
    """
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
        logger.warning("Empty symbol universe; skipping scan")
        return []

    lookback_days = cfg["scanner"]["daily_bar_lookback_days"]
    df = broker.get_daily_bars(symbols, lookback_days)

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
            }
        )

    candidates.sort(key=lambda c: c["gap_pct"], reverse=True)
    return candidates[:top_n]
