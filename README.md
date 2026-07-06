# Alpaca Gap-Down Reclaim Bot

Autonomous, paper-first day-trading bot on Alpaca. Long-only **mean reversion**
on SP500: buy stocks in an uptrend (above their 200-day SMA, SPY also above
its own) that gap DOWN ≥2% with a news catalyst and then reclaim their opening
price intraday. 5-min chart, 15-min cycle, everything closed by 15:55 ET.

This strategy replaced the original "Trend Join Long" gap-up momentum approach
after systematic backtesting (see `EXPERIMENTS.md`): every momentum variant
lost money on 2023-2025 SP500 data, while gap-down reclaim tested at **+12.8%
over 3 years, 58.6% win rate, all years positive, max drawdown 6.7%** (E16
config, now the committed `rules.json`). Backtests are optimistic (IEX data,
simplified fills, no slippage) — paper trading is still the gate before any
real money.

**This is paper-trading software. Never point it at a live account without
weeks of clean paper results reviewed manually.**

## Setup

1. Create a free Alpaca account at https://alpaca.markets and generate
   **paper trading** API keys from the dashboard (Paper Trading section, not
   Live).
2. Create a Telegram bot via [@BotFather](https://t.me/BotFather) to get a
   bot token, and get your numeric chat id (message the bot once, then hit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to read your chat id back).
3. Set up the environment and install dependencies:
   ```
   python -m venv .venv
   ./.venv/Scripts/python.exe -m pip install -r requirements.txt
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

All commands below assume you're inside `alpaca-trend-bot/` and using the
venv Python: `./.venv/Scripts/python.exe` (Windows) — substitute your shell's
equivalent (`.venv/bin/python` on Linux/Mac).

## Quick start (recommended order)

```
python test_connection.py
```
Confirms: paper account connects, prints equity/buying power, submits and
cancels a test limit order, sends a Telegram test message. Must print `PASS`.
Do this first — everything else assumes the plumbing works.

```
python strategy.py
```
Runs the pure-logic self-checks (entry sizing, stop floor, partial/breakeven/
trailing stages, vol multiplier, risk halving). Prints `OK`.

```
python bot.py --once
```
Runs a single trading cycle and exits: scans, sizes and submits an entry if a
candidate qualifies, persists state to `bot.db`. Use this to sanity-check
behavior without leaving the bot running. Inspect the result with:
```
sqlite3 bot.db "select * from positions;"
sqlite3 bot.db "select * from pending_orders;"
```

```
python bot.py
```
Runs forever — this is how you actually trade (on paper). What it does:
- Sleeps until the market opens (via Alpaca's `get_clock()`, so holidays and
  half-days are handled automatically).
- Wakes every `cycle_minutes` (15 by default, see `config.yaml`) during market
  hours. Each cycle: manages any open positions first (stops, partials,
  trailing — this always runs), then looks for new entries if the risk guard
  allows it.
- **Transactions only happen during this loop, only during market hours.**
  There is no separate scheduler — if the process isn't running, nothing
  trades.
- **Market hours (US Eastern) converted to Rome time:**

  | Period | Open | Force-close | Close |
  |---|---|---|---|
  | Most of the year (ET is 6h behind Rome) | 15:30 | 21:55 | 22:00 |
  | ~2-3 weeks in Mar/Oct (US already shifted DST, EU hasn't yet — 5h gap) | 14:30 | 20:55 | 21:00 |

  The code itself is DST-safe (uses `zoneinfo`/Alpaca's `get_clock()`, all ET-
  native) — this table is only for a human in Rome checking a wall clock.
- Force-closes every open position at `force_close_time_et` (15:55 ET) via a
  dedicated wake, since 15-minute cycle boundaries don't land exactly on it.
- Safe to restart mid-day: open positions and in-flight orders are read back
  from `bot.db` rather than re-submitted.
- Sends Telegram alerts on each cycle (candidates scanned, entries), on
  partial/exit/stop fills, on risk-guard halts, and an end-of-day summary
  with month-to-date performance vs the target.

Stop it with Ctrl+C. It's a plain foreground process — run it in a terminal
you can leave open, or under whatever process supervisor you already use.

## Dashboard

```
python dashboard.py
```
Writes `dashboard.html` from `bot.db`: overall win rate / total R / avg R,
a monthly table (net PnL, % of month-start equity vs `monthly_target_pct`),
and every closed trade with its R-multiple. Re-run any time — it's a static
snapshot, not a live server; refresh the browser tab after re-running to see
new trades.

## Backtester

Before trusting any parameter (gap %, stop distance, partial R, filters),
measure it against history instead of guessing:

```
python backtest.py --start 2024-01-01 --end 2025-12-31
```
Replays the exact same logic the live bot uses (`strategy.check_entry_conditions`,
`position_size`, `compute_stage`, `scanner.filter_candidates`) over historical
Alpaca daily + 5-minute bars, day by day, applying the same risk guard,
volatility filter, and news catalyst filter. Writes results to `backtest.db`
and a report to `backtest.html` (reuses `dashboard.py`'s report — same format
as the live dashboard).

Flags:
- `--no-news` — disable the news catalyst filter for this run, useful for
  comparing performance with/without it.
- `--starting-equity 50000` — simulate a different account size (default
  100000).
- `--universe AAPL,NVDA,MSFT` — restrict to a handful of symbols instead of
  the full SP500, for a fast sanity-check run before committing to a full
  multi-year backtest.
- `--db my_run.db` — write to a different SQLite file (each run starts fresh;
  the target db is wiped at the start of the run).

Historical bars are cached on disk under `cache/` — re-running with different
parameters over the same date range doesn't re-fetch years of data. Delete
`cache/` if you suspect stale data.

**Known limitations** (read before trusting the numbers):
- Fills are simplified: entries fill if the bar's high reaches the limit
  price; stops and partials fill at the exact stop/target price. No slippage
  model beyond that.
- No margin/buying-power modeling beyond using simulated equity as buying
  power.
- Runs on whatever data feed `config.yaml` specifies (IEX by default) — see
  "Known limitations" below on data quality.
- A backtest that looks good is a green light to paper trade longer, not a
  green light to go live.

## Risk guard — what each control does (and doesn't promise)

All of this lives in `config.yaml` under `risk_guard` and in `rules.json`.
None of it manufactures profit — it exists to cut losses faster and reduce
noise trades. The strategy's edge (or lack of one) is still the strategy's.

| Control | What it does |
|---|---|
| `max_daily_loss_pct` / `max_monthly_loss_pct` | Realized loss beyond this halts **new entries** for the rest of the day/month. Open positions are still managed (stops/exits keep working). |
| `consecutive_losses_to_halve` | After N losing trades in a row, position size is halved until a winner resets the streak. |
| `max_trades_per_day` | Hard cap on new entries per day. |
| `no_entries_before_et` / `no_new_entries_after_et` | Skips the opening-chop and late-day windows for new entries. |
| `high_vol_annualized_pct` | SPY's realized volatility (10-day, annualized) above this halves risk — no VIX subscription needed. |
| `require_spy_above_sma200` (rules.json) | No long entries at all while SPY is below its own 200-day average (bear-regime filter). |
| `require_news_catalyst` (rules.json) | Only trade gappers with a recent news headline (Alpaca News API) — a gap with a stated reason tends to hold up better than one without. |
| `monthly_target_pct` | **Reporting only.** Shows up in the dashboard and Telegram EOD summary as a yardstick. It is not, and cannot be, a guarantee. |

## Going live

Only after weeks of clean, reviewed paper trading: manually edit
`config.yaml` and set `alpaca.paper: false`, then point `.env` at your live
API keys. There is no automatic promotion path — this is a deliberate,
human-gated step.

## Known limitations

- Free Alpaca data is IEX, not the full SIP consolidated feed — gap
  detection is slightly less precise on thin/illiquid names.
- A handful of `alpaca-py` call shapes (stop replacement, position closing,
  news requests) were verified against the installed SDK version at build
  time; re-check against the SDK docs if you upgrade `alpaca-py`.
- Backtested performance does not guarantee live results — monitor slippage
  and fills closely once real money is involved.
