# Gold/silver ratio mean reversion — findings (idea f96bcab0, rejected)

`ratio_reversion_study.py`: dollar-neutral XAUUSD/XAGUSD pair on broker-day (17:00 NY) closes.
Fade the log gold/silver ratio when it is >= z_in standard deviations from its L-day mean, enter at
the next open, exit when z crosses 0, no stop. Costs: measured spreads (gold $0.15/0.17 + $0.07
commission, silver $0.012/0.018) and a ~5%/yr pair swap (long leg pays FF+3%, short receives FF-2%).
Grid declared up front; choose on 2018-22 by net Sharpe with >= 15 trades.

| L \ z_in | 1.5 | 2.0 | 2.5 |
|---|---|---|---|
| 20 | -0.77 (61) | -0.72 (46) | -0.44 (30) |
| 60 | -0.78 (18) | -0.54 (17) | -0.39 (11) |
| 120 | -0.68 (10) | -0.65 (8) | -0.44 (8) |

Net Sharpe 2018-22 (trades). Zero costs and carry: still -0.25 to -0.59 in every cell, so it is
not a cost artefact. 2023-25 was never looked at — nothing qualified.

Why: the ratio is a random walk at these horizons. Autocorrelation of k-day ratio changes with the
following k days (2018-22): +0.005 (1d), -0.009 (5d), +0.024 (20d), -0.15 (60d, few independent
windows). Fading without a stop gives frequent small wins and rare large losses (March 2020: silver
crashed, ratio > 100). Year-end ratio: 82.8, 85.0, 72.0, 78.5, 76.1, 86.8, 90.9, 61.1 (2018-2025).

## Round 2 — beta hedge, z-stop, momentum (`ratio_variants_study.py`, user-requested)

Same data, signal, costs (swap now per leg) and protocol: choose on 2018-22, one look at 2023-25.

| Variant | Cells | 2018-22 net Sharpe |
|---|---|---|
| V1 reversion, gold leg = rolling 60-day beta | 9 | all negative, -0.58 to -0.99 (worse than dollar-neutral) |
| V2 reversion + stop at abs(z) 3.0 / 3.5, no re-entry until back inside z_in | 18 | all negative, -0.20 to -0.97 |
| V3 momentum: go with the stretch, exit when z crosses 0 | 9 | all positive, 0.10 to 0.39 |

About 45 cells have been tried on 2018-22 across both rounds; the best of that many noise cells
would show an in-sample Sharpe near 0.9, so V3's 0.39 was weak evidence even before the test.

Chosen by the declared rule (best Sharpe with >= 15 trades): **V3, L = 60, z_in = 1.5** — 2018-22
18 trades, Sharpe 0.39, +31.5%, DD -21.6%. **2023-25, looked at once: 16 trades, 2 wins, Sharpe -0.23,
-16.9%, DD -46%** — 14 of 16 trades lost 0.4-6% as stretches snapped back.

Conclusion: at 20-120 day horizons the gold/silver ratio neither reverts nor trends reliably enough
to trade; 2018-22's mild trendiness reversed in 2023-25. 2026 untouched.
