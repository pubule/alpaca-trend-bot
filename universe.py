import io
import json
import logging
from datetime import datetime, timezone

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def fetch_sp500_symbols() -> list[str]:
    # Wikipedia 403s the default urllib/pandas User-Agent; a browser-like one works.
    response = requests.get(_WIKIPEDIA_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    response.raise_for_status()
    tables = pd.read_html(io.StringIO(response.text))
    symbols_col = tables[0]["Symbol"]
    # Alpaca uses '-' for share classes (e.g. BRK-B); Wikipedia uses '.' (BRK.B).
    return [s.replace(".", "-") for s in symbols_col.tolist()]


def load_cached_symbols(cache_path: str, max_age_days: int) -> list[str] | None:
    try:
        with open(cache_path, "r") as f:
            cache = json.load(f)
        fetched_at = datetime.fromisoformat(cache["fetched_at"])
        age_days = (datetime.now(timezone.utc) - fetched_at).days
        if age_days > max_age_days:
            return None
        return cache["symbols"]
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _write_cache(cache_path: str, symbols: list[str]) -> None:
    with open(cache_path, "w") as f:
        json.dump(
            {"fetched_at": datetime.now(timezone.utc).isoformat(), "symbols": symbols},
            f,
        )


def get_sp500_symbols(
    cache_path: str = "sp500.json",
    max_age_days: int = 7,
    force_refresh: bool = False,
) -> list[str]:
    if not force_refresh:
        cached = load_cached_symbols(cache_path, max_age_days)
        if cached is not None:
            return cached

    try:
        symbols = fetch_sp500_symbols()
        _write_cache(cache_path, symbols)
        return symbols
    except Exception:
        logger.exception("Failed to fetch SP500 symbols from Wikipedia")
        try:
            with open(cache_path, "r") as f:
                stale_cache = json.load(f)
            logger.warning("Falling back to stale SP500 cache")
            return stale_cache["symbols"]
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            logger.error("No SP500 cache available and fetch failed; returning empty universe")
            return []
