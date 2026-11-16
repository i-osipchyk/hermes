# Rapid breakout through M30/H1 fractals, confluence-weighted (idea 16458973) — rejected

Session 2026-10-14. XAUUSD 1m 2018-01..2026-08-05. Selection on 2018-22; 2023-01..2026-08-05 scored
once per candidate (2 touches); post-2026-08-06 untouched. Units: ATR(4h,20) per trade; costs $0.20/oz.
Control = same side, same UTC hour, same month, random 1m entries (removes gold's drift).

Scripts: `rapid_break_events.py` (events + IS tables), `rapid_break_dissect.py` (IS cuts),
`rapid_break_candidates.py` (T1/T2, `--holdout`). Event files `research/output/rapid_break_*.parquet`.

## In-sample (2018-22), 4 fractal defs (M30/H1 x n=1/2) x 3 horizons
- Tier order is **day > session ≈ fractal-only**. Day-extreme fractals beat the control by
  +0.025..+0.04 ATR at 120 min (about one cost); session extremes add nothing over a plain fractal.
- **Speed has the wrong sign.** Fastest v15 quintile is worst in every config (excess -0.02..-0.08,
  driven by fractal-only levels); inside the day tier speed is flat. Same as 7f3d026e.
- Day-tier drift lives in NY hours (12-18 UTC: +0.05..+0.09 excess) and in longs; 2021 negative.
- Dropped pocket: fractal-only + slow approach on H1 n=2 (+0.12 net, ~196 events) — contradicts the
  idea, no mechanism, inconsistent on M30.

## Candidates (exit chosen on IS: 0.5 ATR stop, 120 min time exit)
| | IS net | IS t | HOLDOUT net | HOLDOUT t | 2x cost holdout | yrs + |
|---|---|---|---|---|---|---|
| T1 faithful: H1 n=2, fast top-40%, size 1/1.5/2 by tier | +0.018 | 1.3 | +0.004 | 0.2 | -0.012 | 2/4 |
| T2 refined: H1 n=2, day extreme, NY 12-18 UTC | +0.066 | 3.8 | +0.025 | 1.2 | +0.008 | 3/4 |

T2 holdout split by "level is the Tokyo (00-09 UTC) range edge": yes +0.067 (543), no -0.007 (723).
The part of T2 that survives is the already-validated Tokyo breakout (0945d084); the fractal /
confluence / speed components add nothing out of sample.

Trials: 18 IS readings (12 tier tables + 6 candidate/exit), 2 holdout touches.

## Follow-up: Aero entry/exits with ATR-scaled TP (`rapid_break_aero_exits.py`)

Same events. Aero entry (limit at the level ~1.5 min, then market chase) and Aero exits with the
dollars replaced by TP = k x ATR(LTF,14), k calibrated so the 2018-22 median TP = $20
(5.61 x ATR(1h) / 11.86 x ATR(15m)); lock/trail keep Aero's ratios to TP (0.6 / 0.5 / 0.1 /
0.0175); 3.6 x ATR(4h) catastrophe stop; -0.5 x ATR(4h) exit armed from 3h onward.
Control: random minutes, same month/hour/side, market entry, identical exits.

**3h rule corrected.** The EA log shows the grid limits placed exactly 3h after entry *even when the
trade is in profit* (2018-02-02 short 1344.27, market 1341.20 at 3h; 2019-03-19 long 1308.59, market
1310.08), live until the trade closes -- i.e. a -0.5 ATR stop armed permanently from 3h. A one-off
check at minute 180 (as in `aero/aero_daybreak_sim.py`) let ~10% of random breaks run to the
catastrophe stop, which the EA never hits.

IS 2018-22: win 29-34% (random entries with the same exits 48-52%: the limit fills on retests,
the worse half), net ~0. ATR(1h) TP ~ $20 > ATR(15m). Session tier flips sign between H1 n=2 and
M30 n=1 -> noise. Best cell H1 n=2, day extreme, NY: +0.031 ATR4h/trade, PF 1.06, excess +0.060 (t 1.9).

| Holdout 2023-01..2026-08-05 (scored once) | n | win | R | excess vs ctrl (t) | PF | 2x cost R |
|---|---|---|---|---|---|---|
| A all tiers, size 1/1.5/2, ATR(1h) TP | 4712 | 32% | +0.016 | +0.007 (0.1) | 1.03 | 0.000 |
| B day extreme + NY, ATR(1h) TP | 1275 | 34% | +0.020 | +0.019 (0.5) | 1.04 | +0.003 |
| C = B with fixed $20 | 1275 | 50% | +0.006 | +0.042 (1.3) | 1.02 | -0.011 |
| B one position at a time | 645 | 33% | +0.024 | +0.061 (1.2) | 1.05 | +0.008 |

No edge with Aero mechanics either. ATR scaling vs fixed $20: no reliable difference. Confirms again
that the EA's ~80% win comes from its selection, not its mechanics (all-break mechanics win ~30%).

Trials, cumulative: ~54 IS readings, 5 holdout touches.

## Follow-up: what separates winners from losers (`rapid_break_win_loss.py`)

H1 n=2 events, Aero entry/exits, TP 5.61 x ATR(1h). 24 entry-time features ranked on IS 2018-22
(6,911 trades, win 31%): AUC for win, R by quintile, sign stability by year.

- Nothing separates strongly: best single AUC 0.543. Composite of the 7 year-stable features
  (room_pday, atr_level, day_range, ny, age_h, v15, vol_ratio): AUC 0.550 IS / **0.541 holdout** --
  the win-rate ranking holds, but the money does not (holdout top quintile R -0.042, bottom -0.002).
  Higher win odds come with no higher R: wins are capped by the lock/trail, losses are not.
- Speed (v5/v15/v60), tier, overshoot, limit-vs-chase, side, weekday: AUC 0.49-0.53, unstable.
- **One lead: the break carries price beyond the previous day's range.** R by level - previous-day
  extreme (ATR4h, + = still inside yesterday's range): <= -1: +0.152; -1..-0.25: +0.049;
  -0.25..0: -0.031; 0..0.5: +0.036; 0.5..2: ~-0.05; > 2: -0.128 (deep inside yesterday's range).
  Mechanism: a break that also makes a new multi-day extreme is range expansion; a break deep in
  yesterday's range is intraday noise.

| Rule: level >= 0.25 ATR beyond prev-day extreme | n | win | R | excess vs ctrl (t) | 2x cost R | years |
|---|---|---|---|---|---|---|
| IS 2018-22 (where it was found) | 1599 | 35% | +0.091 | +0.094 (2.7) | +0.060 | 3/5 |
| Holdout 2023-26.08 (one look) | 1148 | 33% | +0.059 | +0.037 (0.95) | +0.045 | 3/4 |
| Holdout, rest | 3564 | 31% | -0.001 | -0.009 | -0.017 | |

Not the Tokyo breakout: holdout events away from the Tokyo range edge earn +0.050 (943).
Found by searching 24 features -> data-mined; holdout direction held but is not significant alone.

Trials, cumulative: ~85 IS readings, 7 holdout touches.
