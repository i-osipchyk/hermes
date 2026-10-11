# Silver breakouts — findings

Two ideas, both rejected; the gold Tokyo breakout (`research/tokyo/`, 0945d084) remains the only
working breakout. Costs everywhere are the measured Pepperstone ones (spread ~$0.012 in 2018-22,
~$0.018 in 2023-25, ~$0.05 in 2026; no commission; see `research/tokyo/FINDINGS.md`).
Protocol: chosen on 2018-22, 2023-25 looked at once for the chosen variant, 2026 untouched.

| Idea | Variant | 2018-22 net R (n) | 2023-25 net R (n) |
|---|---|---|---|
| e97af039 session ranges (`research/tokyo/silver_gold_led_study.py`) | silver Tokyo break confirmed by gold, wide days | +0.142 (160) | -0.056 (126) |
| a203e796 swing / daily (`silver_breakout_screen.py`) | 1h fractal swing system (n=2, trailed structure stop) | -0.170 (2398) | not chosen |
| | Turtle 20/10, 2 ATR stop | +0.025 (58) | **-0.172** (38) |
| | Turtle 55/20 | +0.135 (26, under the 30-trade bar) | diagnostic -0.426 (19) |

Why gold works and silver does not (measured where possible): silver's London/NY session is
choppier (09:00-20:45 UTC variance ratio below gold's in 6 of 8 years, 0.78-0.99 vs ~1.0), its
spread is ~6 bp vs ~0.5-1 bp, and every silver breakout form tested lost even gross in 2024-25.
Single-market daily trend following has too few trades to settle anything in 8 years and lost
through the 2025 rally (whipsaw); the trend-following evidence is cross-market.

Swap model used for multi-day holds: long pays FedFunds + 3%/yr, short receives FedFunds - 2%,
calibrated to Pepperstone's silver swap on 2026-10-10 (long -1.58, short +0.49 points/lot/day).
