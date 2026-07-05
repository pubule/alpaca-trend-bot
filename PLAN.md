# Trading Bot — Alpaca + Claude Code

## Context

Build autonomous day-trading bot from the video's blueprint, but on **Alpaca**
instead of IBKR. Runs a **Trend Join Long** momentum strategy hands-off — scans
SP500 gappers, places entries/exits, manages stops, sends Telegram alerts, logs
R-multiples to a dashboard. Cycle repeats every 30 min through the trading day.

**Why Alpaca over IBKR:** strategy is equities-only, so IBKR's multi-asset
weight and outdated/complex API buy nothing here. Alpaca gives modern REST +
WebSocket, free native paper trading, clean `alpaca-py` SDK, and **doubles as
the market-data source** — so no separate `yfinance` and no TWS/Gateway process
to keep alive. Simpler = less code, less debug with Claude.

**Reality check (from the video author himself):** the backtested edge does NOT
translate cleanly to live execution — his own manual trading beats the bot. So
this is *paper-first*. `paper: true` hits `paper-api.alpaca.markets`; flip to
live only after weeks running clean. Bot is a tool; edge lives in the strategy.

Greenfield: no existing trading code in the working dir (only unrelated
`software-estimation-*` projects). New self-contained project folder.

## Decisions

- Broker: **Alpaca**, `alpaca-py` SDK.
- Account: **paper now, live later** via `paper: true` flag in config.
- Strategy: video's **Trend Join Long** (long-only, SP500, 5-min chart).
- Scope: **full pipeline** — core loop + Telegram + R-multiple dashboard.

## Architecture

New folder `alpaca-trend-bot/`. Single long-running Python process (`bot.py`)
that wakes on 30-min boundaries during ET market hours. No TWS to babysit. Alpaca
serves both trading and market data. Per-position state persisted so exit
management (partial / breakeven / trail) survives across cycles.

### Files

| File | Purpose |
|------|---------|
| `config.yaml` | Alpaca key/secret, `paper: true`, risk limits, Telegram token/chat_id, scan params, force-close time |
| `.env` / secrets | API keys out of git (`.gitignore`) |
| `rules.json` | Trend Join Long params (filters, stops, targets, risk) |
| `requirements.txt` | `alpaca-py`, `requests`, `pyyaml`, `pandas`, `lxml` |
| `broker.py` | Alpaca `TradingClient` + `StockHistoricalDataClient`: account, buying power, positions, submit/cancel/replace orders, stop helpers |
| `universe.py` | SP500 tickers (pandas.read_html Wikipedia → cache to `sp500.json`) |
| `scanner.py` | Alpaca daily bars → top-20 gappers >3%, above prev high + prev close > 200-day MA |
| `strategy.py` | apply `rules.json`: entry check, position sizing, exit-stage logic |
| `state.py` | SQLite (`bot.db`): open positions + exit stage + closed trades w/ R |
| `notifier.py` | Telegram send via raw `requests` to Bot API (no extra lib) |
| `bot.py` | brain loop: market-hours gate, 30-min cycle, scan→decide→execute→manage, force-close before close |
| `dashboard.py` | read closed trades from `bot.db` → static HTML report of R-multiples per trade |
| `test_connection.py` | prove plumbing: connect, read account/buying power, submit+cancel 1 paper limit order, send Telegram test |
| `README.md` | Alpaca setup: create paper account, get API key/secret, put in config |

### Strategy rules (rules.json)

- Long only, universe = SP500, timeframe 5-min.
- Daily filters: price above previous day's high; previous close above 200-day MA; gapped up >3%.
- Initial stop: 1% below low of day.
- Partial take-profit at 0.75R; move stop to breakeven at 1R; then trail under 5-min swing lows.
- Risk: 1% account per trade; max 10% account position size. Size = risk_$ / stop_distance, capped at 10%.

### Exit management (per cycle, bot-managed)

Alpaca supports bracket + native trailing-stop orders, but partial+breakeven+trail
combined is dynamic — cleaner bot-managed. Entry with attached initial stop; each
30-min cycle the bot reads open positions from `state.py`, recomputes stage, and
replaces the stop / fires the partial as thresholds hit. Force-close everything at
`force_close_time` (e.g. 15:55 ET) before market close.

### Cycle & scheduling

`bot.py`: single loop. Compute next 30-min boundary in ET (`zoneinfo`), gate on
market hours 09:30–16:00 ET (or Alpaca `get_clock()`), run cycle, `sleep` to next
boundary. No cron/daemon dependency. Telegram alert each cycle (pre-filter list,
entries, exits, partials, EOD summary).

## Ponytail notes (deliberate laziness)

- Data + trading: **one** vendor (Alpaca) — no separate `yfinance`, no TWS.
- Telegram: raw `requests` POST to Bot API — no `python-telegram-bot` dep.
- Scheduler: sleep-to-boundary loop — no APScheduler/cron daemon.
- Dashboard: static HTML from SQLite — no Streamlit/Flask server.
- SP500 list: `pandas.read_html` Wikipedia, cached to file.

## Verification (end-to-end, paper)

1. Alpaca paper account created, keys in config. Run `python test_connection.py`:
   connects (paper endpoint), prints account + buying power, submits a low limit
   order (won't fill), cancels it, sends a Telegram test message. All must succeed.
2. `python -c "import scanner; print(scanner.top_gappers())"` → prints ≤20 tickers.
3. Dry-run one cycle (`--once`) on paper: scan → sized entry submitted → position
   in `bot.db` → Telegram entry alert fires.
4. `strategy.py __main__` runs one `assert`-based self-check on partial/breakeven/
   trail stage math.
5. `python dashboard.py` → HTML showing R-multiple per closed trade.
6. Only after weeks clean on paper: set `paper: false`. Manual, gated.

## Warnings

- Never flip `paper: false` without weeks of clean paper results.
- Alpaca free data = **IEX**, not full SIP consolidated → gap scan less precise on
  thin names; paid data plan unlocks full feed. Fine for build/paper.
- Live account (Italy): verify onboarding eligibility before real money.
- Backtest performance ≠ live; monitor slippage and fills.
