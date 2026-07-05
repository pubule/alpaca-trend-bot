# Alpaca Trend Join Long Bot

Autonomous, paper-first day-trading bot on Alpaca. Long-only "Trend Join Long"
gapper strategy on SP500, 5-min chart, 30-min cycle. See `/PLAN.md` (repo
root) for the full design rationale.

**This is paper-trading software. Never point it at a live account without
weeks of clean paper results reviewed manually.**

## Setup

1. Create a free Alpaca account at https://alpaca.markets and generate
   **paper trading** API keys from the dashboard (Paper Trading section, not
   Live).
2. Create a Telegram bot via [@BotFather](https://t.me/BotFather) to get a
   bot token, and get your numeric chat id (message the bot once, then hit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to read your chat id back).
3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in your real values:
   ```
   cp .env.example .env
   ```
   ```
   ALPACA_API_KEY=...
   ALPACA_API_SECRET=...
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```
   `.env` is gitignored — never commit real keys.
5. Review `config.yaml` (risk limits, schedule, feed) and `rules.json`
   (strategy parameters) and adjust if needed. `alpaca.paper` must stay
   `true` for all of the steps below.

## Verify the plumbing

Run these from inside `alpaca-trend-bot/`:

```
python test_connection.py
```
Confirms: paper account connects, prints equity/buying power, submits and
cancels a test limit order, sends a Telegram test message. Must print `PASS`.

```
python -c "import scanner; print(scanner.top_gappers())"
```
Prints up to 20 candidate gapper dicts (first run fetches and caches the
SP500 list from Wikipedia into `sp500.json`).

```
python strategy.py
```
Runs the partial/breakeven/trailing-stop self-check; prints `OK`.

```
python bot.py --once
```
Runs a single cycle: scans, sizes and submits an entry if a candidate
qualifies, persists state to `bot.db`. Inspect with:
```
sqlite3 bot.db "select * from positions;"
sqlite3 bot.db "select * from pending_orders;"
```

```
python dashboard.py
```
Writes `dashboard.html` with R-multiples for closed trades (table is empty
until trades have closed).

## Running continuously

```
python bot.py
```
Runs forever: sleeps until market open, then wakes every 30 minutes during
market hours (via Alpaca's `get_clock()`, so holidays/half-days are handled
automatically), force-closes all positions at `force_close_time_et` in
`config.yaml`, and keeps going. Safe to restart mid-day — open positions and
in-flight orders are read back from `bot.db` rather than re-submitted.

## Going live

Only after weeks of clean, reviewed paper trading: manually edit
`config.yaml` and set `alpaca.paper: false`, then point `.env` at your live
API keys. There is no automatic promotion path — this is a deliberate,
human-gated step.

## Known limitations

- Free Alpaca data is IEX, not the full SIP consolidated feed — gap
  detection is slightly less precise on thin/illiquid names.
- A handful of `alpaca-py` call shapes (stop replacement, position closing)
  were verified against the installed SDK version at build time; re-check
  against the SDK docs if you upgrade `alpaca-py`.
- Backtested performance does not guarantee live results — monitor slippage
  and fills closely once real money is involved.
