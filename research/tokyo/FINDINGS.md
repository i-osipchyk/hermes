# Gold Tokyo-range breakout — findings (idea 0945d084)

Our own strategy, built from lessons of the Aero EA research (`research/aero/README.md`).
Status: **validated as "ready to forward test", not proven** (Hermes cannot forward test).
Frozen rule since 2026-10-10: `gold_tokyo_breakout_v2_flat` — see "v2 engine" below.

Units: R = result per trade after costs, in ATR(4h,20). "2018-22" = where rules were chosen;
"2023-26" = scored once per rule (this period has now been looked at three times across Tokyo
variants, so it is getting less clean).

## The rules

Two versions; only the first has been through the Hermes engine.

| | v1 `strategies/misc/gold_tokyo_breakout.py` (engine-tested) | v2 TradingView window (event study only) |
|---|---|---|
| Range | high/low 00:00-06:00 UTC | high/low 00:00-09:00 UTC |
| Filter | range >= 1.2 x ATR(4h,20), decided at 06:00 | range >= 1.31 x median of the previous 20 Tokyo ranges (top fifth on 2018-22) |
| Entry | stop orders 0.02 ATR beyond each edge, 06:00-20:00 UTC; each side once a day; one position | same, 09:00-20:00 UTC |
| Stop | 1 ATR from entry, fixed | same |
| Exit | no target; flat at the 20:55 UTC bar (fills next bar — in summer the 22:00 reopen) | same |
| Size | 1% equity risk per trade | — |

Live-trading change worth making: flatten before the 21:00 UTC daily break.

## Evidence by rule

| Rule | Evidence | Strength |
|---|---|---|
| Follow the break (not fade) | fade at the same moment loses (-0.079 / -0.052 R) while following wins (+0.127 / +0.155); years mirror | medium-strong |
| Exit: 1 ATR stop held to the close | best of 6 exits on all 2018-22 breaks (+0.011 vs -0.024..-0.002); best on wide days in both periods; stop 0.875-1.25 ATR on a plateau | medium |
| Wide Tokyo range | v1: +0.127 (2018-22) / +0.155 (2023-26, p = 0.062), 7/9 yrs. v2 (yardstick B chosen over A on 2018-22): +0.088 / **+0.198** (p = 0.028), 8/9 yrs. Unfiltered ~+0.01 / +0.07 | medium |
| Gold only | silver ~0 before costs, negative after (spread ~0.25 ATR in 2018-19) | strong for "not silver" |

Engine check, v1 (run `c991eba4`, 2018-01..2026-08-06, 5m, full costs): 645 trades, PF 1.19, Sharpe
0.64, +59.5% (CAGR 5.6%), max DD -18.9%; 2x costs Sharpe 0.47; params plateau; PSR 0.95; tier
"deployable"; Monte Carlo worst DD **-42.9%**; first 70% Sharpe 0.07 vs last 30% 1.76 (losing 2018,
2019, 2023); walk-forward (1 window, fit 2018-2024.01 -> range 1.5, stop 1.625): OOS Sharpe 0.82,
PF 1.33, 141 trades, DD -5%. The engine version is weaker than the event study in 2018-22 (~+0.03 R
vs +0.13) — one-position rule and live ATR.

## Tested and rejected

| Idea | Result |
|---|---|
| Level held >= 2h (the Aero finding) | no help alone (v2: -0.005 / +0.083 vs +0.014 / +0.067; 78% of breaks qualify); **hurts** with the width filter (B +0.066 vs +0.088; A +0.006 vs +0.056) — on trending Asian days extremes are set late and young breaks carry the momentum |
| Pullback depth, touches, room to previous-day extreme, break hour, arrival speed (v1) | no consistent pattern; more room was *worse* (-0.114) |
| Session window 00-06 vs 00-09 | roughly equivalent |
| Other exit styles (EA-like lock/trail, range stop with 1R/2R, ATR brackets) | worse than holding to the close |

## Promising, not yet confirmed

**Slow, pressing approach** (v2 wide days, 2018-22 only, thirds): last 5m close nearest the level
+0.212 vs farthest -0.024; 1 min before +0.342 vs ~0; most minutes pressing within 0.1 ATR +0.233.
Same direction on non-wide days (fastest third -0.119). Opposite of the Aero EA (whose quick +$12
target likes sharp arrivals): a spike into the edge often reverses, a grind carries. Proposed
pre-declared test: "5m close before the break within 0.09 ATR of the level", scored once on 2023-26
(it would be a third parameter).

## v2 engine (session 2026-10-10b) — frozen rule

**Frozen rule** (idea 0945d084, written to the backlog before any post-2026-08-06 data was
looked at): `strategies/misc/gold_tokyo_breakout_v2_flat.py` at its defaults — range 00:00-09:00
UTC, trade only if range >= 1.31 x median of the previous 20 Tokyo ranges, stop entries 0.02 ATR
beyond each edge 09:00-20:00 UTC, stop 1 ATR, flat by 20:50 UTC. 2 params.

| Iter | Run | What it tested | Result |
|---|---|---|---|
| 1 | `d05bc50a` | v2 in the engine (flat issued 20:55) | 540 tr, Sharpe 0.96, PF 1.31; IS/OOS 0.81/1.28; 2x costs 0.79; yearly WF stitched OOS 1.41 |
| 2 | `5bc6d89a` | **silver**, same rules, 2018-22, kill if net <= 0 (declared first) | Sharpe -0.41, PF 0.86 (290 tr) — **with wrong costs, see "Silver with measured costs"** |
| 3 | `99702e88` | holdout 2026-08-06..10-09 (June-July warm-up) | 8 tr, 5 wins, +0.86 R; showed 20:55 closes filling at the reopen (Friday -> Sunday) |
| 4 | `47b6e2df` | flatten issued at 20:45 (fills 20:50, before the break) | 540 tr, Sharpe 0.92, PF 1.29, +86%, DD -20%; holdout (`627bc35d`) 8 tr +0.96 R |

v2_flat rubric (`hermes analyze 47b6e2df`): survives 2x costs (Sharpe 1.08 / 0.92 / 0.77 at 0/1/2x);
70/30 split 0.79 / 1.21 (v1: 0.07 / 1.76 — v2's self-normalising width removes most of v1's
time-dependence); Sharpe 95% CI [0.11, 1.74]; PSR 0.99; Monte Carlo worst DD **-31.8%** (median
-14.7%); all four regime cells profitable (Bear/High-Vol 2.30, Bear/Low-Vol 0.01 weakest); top-5
trades 8.9% of winning P&L; tier deployable.

Anchored yearly walk-forward (`tokyo_v2_walkforward.py XAUUSD gold_tokyo_breakout_v2_flat`; IS
2018 -> Y, OOS year Y, grid width {1.15, 1.31, 1.5} x stop {0.75, 1.0, 1.25}): stitched OOS
2021-2026.08 480 tr, **Sharpe 1.23**, PF 1.37, DD -12.9%; by year 1.60 / 0.35 / 2.64 / 1.07 / 1.32 / 2.08.
The fit picks width 1.15 — the grid floor — in every window, and the sweep is bumpy (1.0: 1.10,
1.15: 1.28, 1.31: 0.92, 1.47: 0.57). Reading: the width filter carries less than assumed; anything
above ~the median day works. Do not chase lower thresholds — that is the search finding the data.

**Weekend/break artefact, corrected:** with the flatten at 20:55 the close filled on the next bar,
i.e. after the daily break (or Sunday). It cost little (Sharpe 0.96 -> 0.92) — the trades held
over were mostly already in profit by Friday's close. Five holiday early-close trades are still
held overnight (+$0.5k net); flatten earlier on CME holiday sessions when trading live.

**Regime dependence is mostly noise.** Wide-day breaks: mean +0.09 R, sd 1.26 R, ~66/yr ->
P(losing year | constant edge) ~0.28; shuffling trades across years gives a median of 2 losing
years, exactly what was observed. A regime filter would fit noise; more independent trades is
the only cure.

**Silver with measured costs** (corrects iteration 2). The Pepperstone cTrader account
(ProtoOASymbol, 2026-10-10) shows XAGUSD is spread-only (commission 0, lot 5,000 oz; gold $3.50/lot/side,
100 oz). Spreads from cTrader bid/ask ticks (20-min windows at 09:00 / 14:00 / 20:40 UTC, four
Wednesdays a year): silver $0.008-0.0145 in 2018-22, $0.017-0.019 in 2023-25, $0.047-0.063 in 2026
(~6 bp of price throughout); gold $0.12-0.22 (modelled $0.12 — under-costed after 2019, inside the
2x-cost test). Iteration 2 charged silver $0.025 + $3.5/lot, about twice the real cost.
`silver_tokyo_breakout_v2` (v2_flat rules, measured spread by period, no commission):

| Window | Run | Trades | Sharpe | PF | Zero-cost Sharpe | 2x costs |
|---|---|---|---|---|---|---|
| 2018-22, $0.012 | `0c94c4f2` | 290 | **0.44** | 1.13 | 0.98 | -0.10 |
| 2018-22, old $0.025+comm, flat exit | `5f917ca7` | 290 | -0.20 | 0.92 | | |
| 2023-25, $0.018 | `61f7a2c2` | 207 | **-0.32** | 0.91 | **0.04** | -0.68 |

Reading: the first rejection was a cost-model error, but silver still does not carry the edge —
positive in 2018-22 only at realistic (not doubled) costs, and no gross edge at all in 2023-25.
Not tradeable on these rules; the tick samples are quiet minutes, so break-time spreads are if
anything wider.

**Silver-specific variants (idea e97af039, rejected)** — `silver_gold_led_study.py`, event study,
measured costs, 1-ATR stop, flat 20:45 bar; chosen on 2018-22, 2023-25 looked at once:

| Variant (wide = top fifth of 2018-22 range / 20-day median) | 2018-22 net R (n) | 2023-25 net R (n) |
|---|---|---|
| A gold-led: silver entered at next bar after gold's Tokyo break | +0.027 (310) | not chosen |
| B silver London range 07-12 UTC broken 12-20 UTC | +0.027 (327) | not chosen |
| **C silver Tokyo break confirmed by same-side gold break** | **+0.142** (160) | **-0.056** (126), gross +0.011 |
| C without gold confirmation | -0.020 (142) | |

Diagnostic after the decision: wide-day gross R is negative in 2024 and 2025 for A, B and C alike.
Silver breakouts followed through in 2018-22 and stopped doing so after 2023 in every form tested.

**Other instruments** (event study, wide days, net R 2018-22 / 2023-26): silver -0.10 / -0.11,
US500 -0.12 / -0.10, NAS100 -0.06 / -0.03, GER40 +0.20 / -0.05. Gold-specific.

**Contamination.** 2021-26 was looked at by the earlier event studies (v1, v2, width A/B), and the
1.31 cut came from 2018-22, which overlaps the first walk-forward years. Only post-2026-08-06 data
and a forward test are clean.

## Next

1. ~~v2 through the rubric~~ done (above).
2. **Forward test the frozen rule** (paper / minimum size), unchanged. Judge on equity drawdown
   against the Monte Carlo (-32% worst), and expect losing years (~1 in 4).
3. A pre-declared test whether the width filter earns anything at all (width 0 vs 1.31, one look,
   on post-freeze data only) — the sweep suggests it is weak.
4. The slow-approach filter: parked — it would be a third parameter scored on seen data.
5. Diversification needs new ideas, not silver (weak in 2018-22, nothing gross in 2023-25): gold's Tokyo signal traded on silver; a London
   range broken in NY on gold; platinum (needs a first data fetch).
6. Portfolio step (`hermes correlate`) once there is a second working strategy.

## Files

| File | What |
|---|---|
| `tokyo_breakout_study.py` | v1 event study on 1m: 6 exit styles, gold + silver, features |
| `tokyo_multi_market.py [MARKET ...]` | v2 window on 5m, held >= 2h, G4/G2b, any cached market |
| `tokyo_filters.py` | v2 width filter (A vs B, chosen on 2018-22, one look at 2023-26), held x width, approach speed |
| `strategies/misc/gold_tokyo_breakout.py` | v1 Hermes strategy (2 params: range_min, stop_atr) |
| `strategies/misc/gold_tokyo_breakout_v2.py` | v2 Hermes strategy (2 params: width_ratio, stop_atr); per-symbol spread |
| `strategies/misc/gold_tokyo_breakout_v2_flat.py` | **frozen rule**: v2, flat before the daily break |
| `strategies/misc/silver_tokyo_breakout_v2.py` | v2_flat on XAGUSD with measured Pepperstone silver costs |
| `silver_gold_led_study.py ["VARIANT"]` | silver variants A/B/C, 2018-22; one variant's 2023-25 on request |
| `tokyo_v2_walkforward.py [SYMBOL] [STRATEGY]` | anchored yearly walk-forward 2021-2026.08, 3x3 grid |

Outputs in `research/output/`: `tokyo_events_*.parquet` (v1), `tokyo09_*.parquet` (v2),
`tokyo_events_XAUUSD_age.parquet` (age/pullback/touches), `tokyo09_XAUUSD_speed.parquet`.
