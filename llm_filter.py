"""LLM catalyst classification for gap-down candidates.

A gap-down with a *transitory* cause (analyst downgrade, sector sympathy,
market-wide selloff) tends to mean-revert — our trade. A *structural* cause
(guidance cut, fraud, litigation) tends to keep falling — our stop. Claude
classifies the headlines; structural candidates are dropped.

Fully optional: if ANTHROPIC_API_KEY is not set, `CatalystClassifier.from_env()`
returns None and the strategy works as-is.
"""

import hashlib
import json
import logging
import os

import anthropic

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"  # cheapest tier; binary headline classification
CACHE_PATH = "llm_cache.json"

_PROMPT = """A stock in a long-term uptrend gapped down at least 2% at today's open.
Below are recent news headlines for {symbol}.

Classify the most likely cause of the gap down:
- "transitory": analyst downgrade or price-target cut, sector or sympathy selloff,
  market-wide move, technical/flow-driven selling, or no clearly negative
  company-specific news.
- "structural": guidance cut, earnings miss, fraud or accounting issues,
  litigation or regulatory action, key executive departure, product
  failure/recall, dividend cut, or other lasting damage to the business.

Headlines:
{headlines}

Reply with exactly one word: transitory or structural."""


class CatalystClassifier:
    def __init__(self, api_key: str, model: str | None = None, cache_path: str = CACHE_PATH):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        self.cache_path = cache_path
        self._cache = self._load_cache()

    @classmethod
    def from_env(cls) -> "CatalystClassifier | None":
        """None when ANTHROPIC_API_KEY is absent — caller skips the filter."""
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        return cls(api_key)

    def _load_cache(self) -> dict:
        try:
            with open(self.cache_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        with open(self.cache_path, "w") as f:
            json.dump(self._cache, f)

    def classify(self, symbol: str, headlines: list[str]) -> str:
        """Returns 'transitory', 'structural', or 'unknown' (API failure /
        unparseable reply — fail open, callers keep the candidate)."""
        if not headlines:
            return "unknown"
        digest = hashlib.sha256("\n".join(sorted(headlines)).encode()).hexdigest()[:16]
        key = f"{symbol}|{digest}"
        if key in self._cache:
            return self._cache[key]

        prompt = _PROMPT.format(
            symbol=symbol,
            headlines="\n".join(f"- {h}" for h in headlines[:10]),
        )
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=10,
                messages=[{"role": "user", "content": prompt}],
            )
            text = next(
                (b.text for b in response.content if b.type == "text"), ""
            ).strip().lower()
            label = text if text in ("transitory", "structural") else "unknown"
        except anthropic.APIError:
            logger.warning("Claude classification failed for %s; keeping candidate", symbol, exc_info=True)
            return "unknown"

        self._cache[key] = label
        self._save_cache()
        return label

    def drop_structural(self, candidates: list[dict], articles_by_symbol: dict[str, list[str]]) -> list[dict]:
        """Keep transitory/unknown candidates, drop structural ones."""
        kept = []
        for c in candidates:
            label = self.classify(c["symbol"], articles_by_symbol.get(c["symbol"], []))
            if label == "structural":
                logger.info("LLM filter: dropping %s (structural catalyst)", c["symbol"])
                continue
            kept.append(c)
        return kept


if __name__ == "__main__":
    # Self-check: cache round-trip + drop logic with a stubbed classifier.
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "llm_cache_test.json")
    if os.path.exists(path):
        os.remove(path)

    clf = CatalystClassifier.__new__(CatalystClassifier)
    clf.cache_path = path
    clf._cache = {}
    clf.model = DEFAULT_MODEL

    # Pre-seed the cache as if the API had answered; classify must not call out.
    import hashlib as _h
    d1 = _h.sha256("\n".join(sorted(["ACME cut guidance for FY26"])).encode()).hexdigest()[:16]
    d2 = _h.sha256("\n".join(sorted(["Sector selloff drags peers lower"])).encode()).hexdigest()[:16]
    clf._cache = {f"ACME|{d1}": "structural", f"BETA|{d2}": "transitory"}

    assert clf.classify("ACME", ["ACME cut guidance for FY26"]) == "structural"
    assert clf.classify("BETA", ["Sector selloff drags peers lower"]) == "transitory"
    assert clf.classify("GAMMA", []) == "unknown"  # no headlines -> unknown, no API call

    candidates = [{"symbol": "ACME"}, {"symbol": "BETA"}, {"symbol": "GAMMA"}]
    articles = {"ACME": ["ACME cut guidance for FY26"], "BETA": ["Sector selloff drags peers lower"]}
    kept = clf.drop_structural(candidates, articles)
    assert [c["symbol"] for c in kept] == ["BETA", "GAMMA"], kept  # structural dropped, unknown kept

    # Cache persisted and reloaded
    clf._save_cache()
    clf2 = CatalystClassifier.__new__(CatalystClassifier)
    clf2.cache_path = path
    clf2._cache = clf2._load_cache()
    assert clf2._cache[f"ACME|{d1}"] == "structural"

    os.remove(path)
    print("OK")
