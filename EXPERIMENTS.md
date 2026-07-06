# Strategy experiments log

Goal: find the most profitable strategy configuration via backtest iteration.
Metric: final equity + avg R + win rate on the SAME data window. Paper trading
is still the final judge — backtest wins are hypotheses, not guarantees.

## Baseline results (2024-01-01 → 2025-12-31, 502 days)

| Run | Config | Final equity | Trades | Win% | Avg R | Verdict |
|---|---|---|---|---|---|---|
| B0 | committed rules (stop floor 1.5%, max pos 10%) | 85,385 (-14.6%) | 552 | 50.5% | +0.028 | R/$ incoherent (sizing imbalance) |
| B1 | max pos 25% | 64,687 (-35.3%) | 542 | 50.2% | +0.025 | amplifies imbalance, reverted |
| B2 | stop floor 3.0% | 80,583 (-19.4%) | 512 | 47.1% | -0.038 | R/$ coherent, edge NEGATIVE, reverted |

Diagnosis: Trend Join Long with current filters has no measurable edge
2024-2025. Sizing gives biggest positions to the most fragile trades
(tight stop floor -> small risk/share -> max qty), but fixing sizing (B2)
just exposes the negative edge underneath.

## Experiment queue

- [ ] E1: extend window to 2023-2025 (3y), baseline rules — more data, includes 2023 recovery
- [ ] E2: no partial exit — let winners run on trail only (partial at 0.75R caps the right tail; 42% of trades die at force_close ~0R)
- [ ] E3: wider trail (trail_lookback_bars 3 → 6) — less premature trailing stop-outs
- [ ] E4: entry = break of first-30-min high (ORB) instead of prev-day-high + 0.1% offset
- [ ] E5: risk-parity sizing — cap position notional by stop distance so tight-stop trades don't get max size
- [ ] E6: no stop floor at all + qty cap — test if the 1.5% floor is the whipsaw source

## Results

| Exp | Window | Config | Final equity | Trades | Win% | Avg R | Notes |
|---|---|---|---|---|---|---|---|
| E1 | 2023-2025 | baseline rules | 82,278 (-17.7%) | 669 | 50.8% | +0.031 | all 3 years negative $ (2023 -3.7k, 2024 -3.4k, 2025 -10.6k). Baseline loses everywhere. |
| E2 | 2023-2025 | no partial (trail-only winners) | 81,536 (-18.5%) | 669 | 50.8% | +0.031 | worse than E1: banking 50% at 0.75R beats trailing everything. Partial helps, keep it. |
| E3 | 2023-2025 | trail_lookback 6 bars | 82,218 (-17.8%) | 669 | 50.7% | +0.029 | identical to E1. Trail width irrelevant. Exit tweaks don't move the needle — entry is the problem. |
| E5 | 2023-2025 | skip_if_notional_capped | 100,128 (+0.1%) | 18 | 50.0% | +0.122 | first non-losing run. Filtered 97% of trades; the ~650 clipped (stop <10% of entry) trades collectively lose -18k. ALL the bleed comes from tight-stop trades. 18 trades = no strategy, but a key diagnostic. |
| E9 | 2023-2025 | min_gap 5% | 92,536 (-7.5%) | 294 | 49.7% | +0.003 | halves trades and bleed vs baseline, edge still negative. Stronger momentum filter helps but doesn't flip the sign. |
| E4 | 2023-2025 | ORB 30-min entry | 95,454 (-4.5%) | 212 | 46.7% | -0.035 | best full strategy so far; 2024 slightly positive (+1k). Still negative overall. |
| E12 | 2023-2025 | combo ORB+gap5%+skip-clipped | 99,947 (-0.1%) | 13 | 46.2% | +0.108 | flat, 13 trades = no strategy. Momentum family exhausted: every tradeable config loses, every flat config barely trades. |
| E13 | 2023-2025 | MEAN REVERSION: gap down ≥2% + reclaim open, uptrend only | 96,508 (-3.5%) | 875 | 57.1% | **+0.146** | FIRST real positive R edge (+128R total). $ negative purely from sizing distortion (stop floor -> clipped size -> non-uniform risk). Fixable. |
| **E14** | 2023-2025 | **E13 + uniform 0.15% risk** | **104,173 (+4.2%)** | 939 | **58.6%** | **+0.168** | **FIRST PROFITABLE CONFIG. All 3 years positive** (23: +851, 24: +1285, 25: +2037). Edge small (~1.4%/yr) but consistent. Now scaling. |
| E15 | 2023-2025 | E14 + 15 trades/day | 104,299 (+4.3%) | 942 | 58.5% | +0.167 | ≈E14. Trade cap was never binding — signal scarcity (few gap-downs-in-uptrend per day) limits count, not the cap. Scaling lever = per-trade risk. |
| E16 | 2023-2025 | E14 + risk 0.45% / notional 30% | **112,790 (+12.8%)** | 941 | 58.6% | +0.168 | Edge scales linearly (3x risk = 3x return). All years positive (23: +2.6k, 24: +4.0k, 25: +6.2k). **Max DD 6.7%.** ~4.1%/yr. |
| E17 | 2023-2025 | E14 + risk 0.9% / notional 60% | 112,717 (+12.7%) | 837 | 57.1% | +0.150 | Scaling breaks at 6x: DD doubles to 13.1%, 2025 negative, buying power binds (fewer trades), avgR degrades. E16 dominates. |

## WINNER: E16 — promoted to rules.json

**Gap-Down Reclaim** (mean reversion, long-only): SP500 stock above its
SMA200, SPY above its SMA200, gap down ≥2% at the open, recent news catalyst,
entry when price reclaims the day's open. Exits: stop 1% below LoD (1.5%
floor), 50% partial at 0.75R, breakeven at 1R, 3-bar swing-low trail,
force-close 15:55 ET. Risk 0.45%/trade, max 30% notional.

Backtest 2023-2025: +12.8%, 941 trades, 58.6% win, avg +0.168R, max DD 6.7%,
all three years positive.

Honest caveats: IEX data, simplified fills, no slippage model, and 2%/month
(~27%/yr) remains FAR above what this edge measured. Scaling risk further
degrades the profile (E17). Paper trade this config for weeks before any live
consideration.

Infra: added `--rules` flag to backtest.py so experiments use variant files
(rules_eN.json) without touching the committed rules.json.
