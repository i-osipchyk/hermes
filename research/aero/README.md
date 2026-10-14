# Aero MT5 — reverse-engineering research (2026-10-09 .. 10-10)

**Question.** A third-party MetaTrader EA, *Aero MT5* (MQL5 Market product 176860, version 3.60,
updated 2026-08-30), trades breakouts on gold profitably in its tester. How does it work, is its
edge real, and what can be reused in our own strategies?

**Short answer.**
- The **mechanics** were reverse-engineered and reproduced (a 1m simulator matches the EA's
  logged trade outcomes 89% of the time). Applied to *every* gold day-high/low break, those
  mechanics break even (PF 1.02).
- The **whole edge is the selection** of which breaks to take: its picks earn +$4.7..8.7/oz per
  trade in every year 2018-2026 (+0.23 R), the average break ~0.
- That selection is ~85% *imitable* from observable features (where/when it trades), but the
  imitation earns ~nothing. No feature set, kNN variant or data resolution (1m, 5m, H1, D1, tick
  volume, news timing) recovered more than ~+0.03 R.
- Silver did not confirm a transferable selection over 2018-2026 (p = 0.20). Memorised training
  outcomes (the kNN's case base likely overlaps the backtest years) remain a live explanation.
- **Trust score: ~3/10** — real mechanics, unverifiable edge, vendor descriptions contradicted by
  the logs (it *does* use a grid with growing lots and *does* chase at market).
- Reusable outcome: the gold **Tokyo-range breakout** (`research/tokyo/`), validated as
  "ready to forward test".

Detailed, numbered findings with all numbers: [`FINDINGS.md`](FINDINGS.md) (sections A-O).
Backlog: `hermes ideas` — 87eb623e (EA replica), 15f8fcda (walk-forward kNN), e61a4161 (approach
speed), 7f3d026e (dominant swings), af10eff8 (old-level pattern), fc2d8d73 (kNN lab), 0945d084 (Tokyo).

## What the EA does (reconstructed)

| Part | Rule | Evidence |
|---|---|---|
| Levels | current broker-day running high/low (server day = 17:00 New York), or an H1 swing; also previous-day extremes, old H1/H4 swings | 592/704 orders (84%) are day-extreme or H1-swing breaks (FINDINGS A, H) |
| Selection observables | level held >= 2h (take rate 0.9% -> 15.5% at 8-24h), pullback ~0.5 ATR, 1-2 prior touches, after 12:00 server, sharp 1-5 min arrival, $12 lock small vs current volatility | FINDINGS A-D, G, J |
| Pacing | <= 2 orders per broker day, ~2-4h cooldown, one position | FINDINGS D |
| Trigger | price trades $0.50 through the level (tick-timed) | FINDINGS E |
| Entry | limit at the level for ~1.5 min; if unfilled, market chase when <= ~$2.9 away (303/577 trades), else abandon (127 orders) | FINDINGS E, J |
| Exits | TP $20 (rarely hit); at +$12 stop -> +$10, then trail $2 behind in $0.35 steps; at 3h if under water a grid (legs at 0.5/1.0/1.5 ATR(4h,20), growing lots) whose first fill closes the basket = stop at -0.5 ATR; catastrophe stop ~3.6 ATR; separate hedge basket (magic + 555000) | FINDINGS E; vendor manual (blog post 770050) |
| Session | no entries 00:00-02:30 and 23:00-24:00 server; FOMC blackout (embedded dates) | inputs, manual |
| Data it can see in the tester | XAUUSD bars only ("every tick generating" = ticks synthesised from M1), no calendar or web; plus whatever is embedded in the 343 KB .ex5 (kNN cases, FOMC dates) | tester logs |

## Timeline of the research

1. **Log parsing** (`logs/20261009.log`, 2026 run): 103 orders, 77 trades, 84% win, +6.75%. Server
   time UTC+2/+3 (EU DST) fixed by matching prices to Pepperstone bars (100%).
2. **Replica** (`strategies/aero_replica*.py`): fidelity run `8bad8a01` reproduces 96% of shared trade
   outcomes. Unfiltered over 2018-2026 it has no edge (`ca318f86` literal, `b724746d` ATR-scaled);
   break-side vs 7 random-side controls (`0d9f1f8b`, `77f8d257`..) -> direction adds little; profit
   only in 2025-26.
3. **Full 2018-2026 tester log** (`logs/20261009 copy.log`): $10k -> $27.2k, profitable every year,
   57% of profit from market chases. Selection lift is uniform across every observable stratum.
4. **Myfxbook live account** (screens, 2026-09-21..10-09): 11 gold episodes, Aero-pattern singles
   7/8 at +$9.8-12.7/oz; 74% of profit from one hedge basket; open trades private.
5. **Feature hunts** on EA choices: approach speed (FINDINGS C), swing dominance (H1 fits better than
   M30 once level counts are equal), level age (strongest, AUC 0.742), pullback, touches, time of day;
   rule-combination coverage/selectivity tables (FINDINGS G, `aero_rule_table.py`).
6. **Data upgrades**: 5m and 1m gold history (2018-2026) and silver 1m; engine fix for stop-entry
   take-profits on the fill bar (`src/hermes/execution/simulated.py`, tests in
   `tests/test_review_fixes.py`) — the old rule both missed fill-bar wins and over-credited next-open
   "gaps"; net effect slightly worse results.
7. **Mechanics on all breaks** (FINDINGS I): EA picks +$6.29/oz PF 3.79, all breaks +$0.25 PF 1.02.
8. **kNN attempts**: v1 (`aero_knn_filter.py`), v2 with 19 features and EA execution
   (`aero_knn_v2.py`: in-sample +$2.86/oz, out-of-sample +$0.09, worse than random), lab of six
   families incl. raw price shape, imitation, weighting, two-stage (`aero_knn_lab*.py`, FINDINGS N):
   ceiling ~+0.03 R.
9. **Silver**: 2026 window looked like strong confirmation (FINDINGS M) but the full 2018-2026 run
   (`logs/20261009 copy 3.log`, FINDINGS O) shows p = 0.20 and negative early years — corrected.
10. **Trust assessment** ~3/10 and the open decisive tests below.

## Open, decisive tests (need MetaTrader)

| Test | Status |
|---|---|
| Mirrored gold (price flipped) vs control on custom symbols | data ready: `research/output/XAUUSD_MIRR_M1.csv`, `XAUUSD_CTRL_M1.csv` (MT5 bar-import format, server time); steps in session notes: Symbols -> Create Custom Symbol (copy XAUUSD) -> Bars -> Import -> tester 2018.02.01-2026.08.06, every tick |
| Gold before ~2016 (outside "ten years") | needs a broker with older history |
| v3.60 from 2026-08-31 to today | small but clean |
| Vendor's live-signal histories | unavailable at time of writing |

## Files

Run everything from the repo root with `.venv/bin/python research/aero/<script>.py`. Outputs go to
`research/output/` (git-ignored). Order matters: later scripts read earlier outputs.

| Script | What it does | Reads | Writes |
|---|---|---|---|
| `aero_knn_filter.py [5m\|15m]` | day-high/low break events + replica exits; first walk-forward kNN | bars | `aero_knn_events_{tf}.parquet`, `aero_knn_scored_{tf}.parquet` |
| `aero_approach_speed.py` | EA-log parser (orders, busy times, server->UTC), approach-speed features vs EA picks | events 5m, `logs/` | `aero_approach_speed.parquet` |
| `aero_swing_dominance.py` | M30/H1 fractal swing breaks, dominance (prev higher / later swing) vs EA orders | bars, logs | `aero_swing_{rule}_{n}.parquet` |
| `aero_pattern_mining.py` | ~40 features: AUC for EA picks, logistic/tree mimic, winners vs losers | approach parquet, swings | `aero_pattern_mining.parquet` |
| `aero_m1_microstructure.py` | 1m features at the break minute | pattern parquet, 1m bars | `aero_m1_microstructure.parquet` |
| `aero_rule_table.py [30min_1\|1h_1\|1h_2]` | coverage/selectivity of rules R1-R11 and all combinations | events, swings, 1m | `aero_rule_table_{def}.csv/.parquet` |
| `aero_daybreak_sim.py` | EA mechanics on 1m for every day break; simulator check vs EA log | events 5m, 1m, logs | `aero_daybreak_sim.parquet` |
| `aero_new_features.py` | room, lock-vs-volatility, confluence, release proxy, post-trigger retest | daybreak sim | `aero_new_features.parquet` |
| `aero_knn_v2.py` | 19-feature walk-forward kNN with EA entry/pacing, vs EA trades | new features | `aero_knn_v2_features.parquet`, `aero_knn_v2_trades.parquet` |
| `aero_silver.py` | selection transfer to silver, 2026 window (ATR geometry) | 1m bars, logs | `aero_silver_{sym}.parquet` |
| `aero_silver_full.py` | same over the full 2018-2026 silver run | 1m bars, logs | `aero_silver_full_{sym}.parquet` |
| `aero_knn_lab.py`, `aero_knn_lab2.py` | kNN variants: hand/raw shape/blocks/targets/imitation/two-stage/weighted/blend | 1m bars, logs | `aero_lab_{sym}.parquet`, `aero_knn_lab*.csv` |

Strategies (stay in `strategies/` — discovery only scans that folder): `aero_replica.py` (15m, fixed
$12 target), `aero_replica_atr.py` (target 0.33 ATR), `aero_replica_control.py` (break side vs random
side), `aero_replica_5m.py`, `aero_replica_atr_5m.py` (5m wrappers).

`logs/` (git-ignored, 1.8 GB): `20261009.log` (gold 2026), `20261009 copy.log` (gold 2026 + 2018-2026),
`20261009 copy 2.log` (+ silver 2026), `20261009 copy 3.log` (+ silver 2018-2026). Each later file
contains the earlier runs; scripts use the last run of the relevant symbol.

Data cached in `.hermes_cache/bars/pepperstone/`: XAUUSD 1m/5m/15m/1h/1d 2018-2026.09, XAGUSD 1m
2018-2026.08, and 5m 2018-2026.09 for EURUSD, GBPUSD, EURGBP, USDJPY, UK100, JPN225, US500 (NAS100 and
GER40 5m have a 2021-22 gap). Scripts that fetch must load `.env` themselves (only the CLI does).
