import time
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import (
    ClosePositionRequest,
    GetCalendarRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopOrderRequest,
)
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5


def _with_retry(fn, *args, **kwargs):
    delay = 1.0
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # alpaca-py raises APIError subclasses for HTTP errors
            status = getattr(exc, "status_code", None)
            if status is not None and status not in _RETRYABLE_STATUS:
                raise
            last_exc = exc
            if attempt == _MAX_RETRIES - 1:
                break
            time.sleep(delay)
            delay *= 2
    raise last_exc


class Broker:
    def __init__(self, api_key: str, api_secret: str, paper: bool = True, data_feed: str = "iex"):
        self.trading_client = TradingClient(api_key, api_secret, paper=paper)
        self.data_client = StockHistoricalDataClient(api_key, api_secret)
        self.news_client = NewsClient(api_key, api_secret)
        self.data_feed = DataFeed.SIP if data_feed == "sip" else DataFeed.IEX

    def get_account(self):
        return _with_retry(self.trading_client.get_account)

    def get_clock(self):
        return _with_retry(self.trading_client.get_clock)

    def get_open_positions(self):
        return _with_retry(self.trading_client.get_all_positions)

    def get_daily_bars(self, symbols: list[str], lookback_days: int, end: datetime | None = None):
        end = end or datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)
        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=start,
            end=end,
            feed=self.data_feed,
        )
        bar_set = _with_retry(self.data_client.get_stock_bars, request)
        return bar_set.df

    def get_intraday_bars(
        self,
        symbol,
        minutes_back: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ):
        """Live callers pass minutes_back (window ending now). Backtest passes
        an explicit start/end (a trading session) and may pass a symbol list."""
        if start is None:
            end = end or datetime.now(timezone.utc)
            start = end - timedelta(minutes=minutes_back)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed=self.data_feed,
        )
        bar_set = _with_retry(self.data_client.get_stock_bars, request)
        return bar_set.df

    def get_calendar(self, start_date, end_date):
        request = GetCalendarRequest(start=start_date, end=end_date)
        return _with_retry(self.trading_client.get_calendar, request)

    def get_news_articles(
        self,
        symbols: list[str],
        hours_back: float | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, list[str]]:
        """Headlines per requested symbol in the window. Live use passes
        hours_back; backtest passes an explicit start/end so historical news
        windows are reproducible."""
        if not symbols:
            return {}
        if start is None:
            end = end or datetime.now(timezone.utc)
            start = end - timedelta(hours=hours_back or 24)
        request = NewsRequest(
            symbols=",".join(symbols), start=start, end=end, limit=50, exclude_contentless=True
        )
        news_set = _with_retry(self.news_client.get_news, request)
        articles = news_set.data.get("news", []) if hasattr(news_set, "data") else []
        requested = set(symbols)
        by_symbol: dict[str, list[str]] = {}
        for article in articles:
            for sym in article.symbols or []:
                if sym in requested and article.headline:
                    by_symbol.setdefault(sym, []).append(article.headline)
        return by_symbol

    def get_news_symbols(self, symbols: list[str], **kwargs) -> set[str]:
        """Symbols with at least one news article in the window."""
        return set(self.get_news_articles(symbols, **kwargs))

    def submit_limit_order(self, symbol: str, qty: float, side: str, limit_price: float, tif: str = "day"):
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY if tif == "day" else TimeInForce.GTC,
            limit_price=limit_price,
        )
        return _with_retry(self.trading_client.submit_order, order_data)

    def submit_market_order(self, symbol: str, qty: float, side: str, tif: str = "day"):
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY if tif == "day" else TimeInForce.GTC,
        )
        return _with_retry(self.trading_client.submit_order, order_data)

    def submit_stop_order(self, symbol: str, qty: float, stop_price: float, side: str = "sell"):
        order_data = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            stop_price=stop_price,
        )
        return _with_retry(self.trading_client.submit_order, order_data)

    def replace_stop_order(self, order_id: str, new_stop_price: float):
        order_data = ReplaceOrderRequest(stop_price=new_stop_price)
        return _with_retry(self.trading_client.replace_order_by_id, order_id, order_data)

    def cancel_order(self, order_id: str):
        return _with_retry(self.trading_client.cancel_order_by_id, order_id)

    def get_order(self, order_id: str):
        return _with_retry(self.trading_client.get_order_by_id, order_id)

    def close_position(self, symbol: str):
        return _with_retry(self.trading_client.close_position, symbol, ClosePositionRequest())
