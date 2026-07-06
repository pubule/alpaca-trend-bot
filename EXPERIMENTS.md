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

Infra: added `--rules` flag to backtest.py so experiments use variant files
(rules_eN.json) without touching the committed rules.json.
