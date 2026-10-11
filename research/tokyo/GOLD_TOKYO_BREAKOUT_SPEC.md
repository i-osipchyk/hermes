# Gold Tokyo-range breakout — forward-test specification

Self-contained spec for re-implementing and forward testing the strategy in a new project.
Everything needed to reproduce the reference backtests is here; section 7 lists the numbers
your re-implementation should match.

- **Origin:** Hermes repo, idea `0945d084` (`research/ideas.json`), research notes
  `research/tokyo/FINDINGS.md`, strategy `strategies/misc/gold_tokyo_breakout_v2_flat.py`.
- **Status:** ready to forward test, **not proven**. Backtests only; no live or paper results yet.
- **Spec date:** 2026-10-11. Forward-test parameters fixed on that date (section 2).

---

## 1. The idea in one paragraph

On gold (XAUUSD), when the Asian session (00:00–09:00 UTC) has been unusually wide, the
London/New York session tends to continue in the direction it breaks out of that range. Enter on
a stop order just beyond the range edge, risk 1 ATR, hold to the end of the US session, and be
flat before the daily break. It is gold-specific: the same rules fail on silver, US500, NAS100 and
GER40. A plausible mechanism: gold's London/NY session trends (intraday variance ratio ≈ 1.0–1.2)
while silver's chops; a wide Asian range marks a day with real directional flow.

---

## 2. Rules (forward-test version)

All times **UTC**. Base bars **5 minutes**, bid prices.

### Parameters

| Name | Value | Meaning |
|---|---|---|
| `width_ratio` | **1.0** | Trade only if today's Tokyo range ≥ `width_ratio` × median of the previous 20 Tokyo ranges |
| `stop_atr` | **1.0** | Stop distance in ATR |
| risk per trade | 1% of equity | sized against the stop distance |

`width_ratio = 1.0` ("today's Asian range is at least a typical one") was chosen by the user for
the forward test on 2026-10-11. It was **not** in the walk-forward grid; the validated, walk-forward
tested value was 1.31 (section 8). Keep it fixed for the whole forward test.

### Constants (not tunable)

| Constant | Value |
|---|---|
| Tokyo range window | bars with open time 00:00 ≤ t < 09:00 |
| Range valid if | weekday (Mon–Fri) and ≥ 50 five-minute bars in the window |
| Yardstick | median of the previous **20** valid Tokyo ranges (needs ≥ 15) — today excluded |
| Entry window | orders are (re)placed while processing each bar from the 09:00 bar through the 19:55 bar (bar open times); an order placed on bar *i* can fill on bar *i+1*, so the earliest fill is on the 09:05 bar and the latest on the 20:00 bar |
| Entry offset | 0.02 × ATR beyond the range edge |
| ATR | Wilder ATR(20) on **4-hour** bars, 4h bars anchored to the 17:00 New York day (17, 21, 01, 05, 09, 13 NY), **including the forming 4h bar** |
| Flatten | at the 20:45 bar a market close is sent; it fills at the **20:50** bar open |

### Day logic

1. **00:00–09:00:** record the Tokyo high and low.
2. **First bar at/after 09:00:** decide once for the day:
   `trade_today = weekday and bars ≥ 50 and len(history) ≥ 15 and (high − low) ≥ width_ratio × median(last 20 ranges)`.
3. **While flat, on every 5m bar from 09:00 until 19:55** (if `trade_today`):
   - cancel last bar's unfilled orders and re-place, using the current ATR:
     - **buy stop** at `tokyo_high + 0.02·ATR`, stop-loss `entry − stop_atr·ATR`
     - **sell stop** at `tokyo_low − 0.02·ATR`, stop-loss `entry + stop_atr·ATR`
     (the stop-loss is measured from the **order's trigger price**, not the fill)
   - each side may fill **at most once per day**; after a long has filled (and closed), only the
     short can still trigger that day, and vice versa;
   - **one position at a time**: while a trade is open, no orders are placed.
4. **Exit:** stop-loss hit, or end-of-day flatten (close sent at 20:45, filled at 20:50 open).
   No take-profit, no trailing.
5. **End of UTC day:** if it was a valid weekday session, append today's Tokyo range to the history.

### Live-trading notes

- **Holidays:** on CME early-close days (Christmas Eve, Black Friday, US holidays) flatten
  before the early close — the backtest held 5 such trades overnight.
- **Spread:** gold's real Pepperstone spread was $0.12–0.22 in 2018–2026 (measured from cTrader
  ticks); the backtest charged $0.12. Judge live results against ~$0.18.
- **Friday:** the 20:50 flatten is before the Friday close (21:00 UTC summer / 22:00 winter).

---

## 3. Execution model of the reference backtest

Reproduce these exactly or the numbers will not match.

| Item | Reference backtest |
|---|---|
| Data | Pepperstone cTrader XAUUSD **5m bid** trendbars, UTC, timestamped at bar **open**, 2018-01-01 → 2026-08-06 (end exclusive) |
| Instrument | contract 100 oz/lot, volume step 0.01 lot, tick 0.01 |
| Order timing | orders placed while processing bar *i* can fill from bar *i+1* |
| Buy stop | triggers when bar **high (bid) ≥ stop**; raw fill = `max(stop, open)`; then **+ spread** (buys pay the ask) |
| Sell stop | triggers when bar **low (bid) ≤ stop**; raw fill = `min(stop, open)`; sells fill at bid (no spread added) |
| Stop-loss | long: bid low ≤ SL → exit at `min(SL, open)` (bid); short: bid high ≥ SL → exit at `max(SL, open)` **+ spread** |
| Fill bar | the stop-loss is checked on the entry bar too (a trade can open and stop out in one bar) |
| Market close | fills at the next bar's open (bid; + spread for buys-to-close) |
| Spread | $0.12 full spread, ×5 from 16:55–17:05 New York on weekdays, ×10 Sunday 17:00–18:00 New York |
| Commission | $3.50 per lot per side ($7 round trip) — matches the Pepperstone account |
| Slippage | none |
| Financing | 0.5%/yr per night held (only the few holiday overnight trades) |
| Sizing | units = 1% × current equity / (|trigger price − stop-loss| × 100 oz), rounded to 0.01 lot; equity compounds |
| Starting equity | $100,000 |

---

## 4. Costs for silver (for reference only — silver is not traded)

Pepperstone XAGUSD is spread-only (commission 0, 5,000 oz lot); measured spread ~$0.012
(2018–22), ~$0.018 (2023–25), ~$0.05 (2026). Silver failed on these rules even with correct costs.

---

## 5. Expectations for the forward test

| | Width 1.0 reference |
|---|---|
| Trades per year | ~130 |
| Win rate | ~47% |
| Mean result per trade | ~+0.10 R (R = 1% of equity) |
| Median holding time | ~7 hours |
| Losing year probability (if the edge is real) | roughly 1 in 5 (none of 2018–2026 lost; 2019 +1.1%) |
| Drawdown to plan for | median −19%, worst −46% (Monte Carlo, 1% risk) |

## 6. Kill / continue rules (fixed before the forward test)

1. **Execution check, from trade one:** for each live trade, compare entry, exit and spread with
   the backtest on the same day. Persistent extra slippage > ~0.05 R per trade erodes half the edge
   — fix execution or stop.
2. **Stop the forward test** if either happens:
   - drawdown exceeds the Monte Carlo worst case for the reference: **−46% at 1% risk**
     (section 7.1). At that size this is a large loss — consider forward testing at 0.25–0.5%
     risk, where the same rule scales to roughly −12% to −23%;
   - after ~100 trades the mean result is clearly negative (below about −0.1 R per trade).
3. **Do not change parameters during the test.** A changed rule restarts the clock.
4. Expect to need **2–3 years** of live results to confirm a Sharpe near 1; one year can only
   falsify it.

---

## 7. Reference results — match these

Hermes ledger keys in brackets; look them up with `.venv/bin/hermes show <key>` in the Hermes
repo. All runs: rules of section 2, execution of section 3.

### 7.1 Forward-test configuration: width 1.0, stop 1.0 — run `2fe13c7c79f3ee20`

2018-01-01 → 2026-08-06, $100k, full costs.

| Metric | Value |
|---|---|
| Trades | **1,121** (buy 580 / sell 541) |
| Total return | **+176.7%** (CAGR 12.6%) |
| Sharpe / Sortino | **1.10** / 1.58 |
| Max drawdown | **−17.2%** |
| Win rate | 47.3% |
| Profit factor | 1.23 |
| Exit reasons | 747 end-of-day, 374 stop-loss |
| Gross P&L / commission | +$189,973 / $13,298 |
| Net P&L by side | buy +$78,130, sell +$98,545 |

By year:

| Year | Trades | Win % | Net P&L | Mean R | Return |
|---|---|---|---|---|---|
| 2018 | 128 | 40.6 | +8,227 | +0.072 | +8.2% |
| 2019 | 120 | 41.7 | +1,169 | +0.014 | +1.1% |
| 2020 | 123 | 47.2 | +13,741 | +0.102 | +12.6% |
| 2021 | 141 | 51.1 | +21,102 | +0.119 | +17.1% |
| 2022 | 133 | 51.1 | +27,894 | +0.145 | +19.3% |
| 2023 | 130 | 47.7 | +39,087 | +0.165 | +22.7% |
| 2024 | 157 | 45.9 | +25,279 | +0.079 | +12.0% |
| 2025 | 124 | 49.2 | +14,336 | +0.053 | +6.1% |
| 2026 (to 08-06) | 65 | 53.8 | +25,842 | +0.157 | +10.3% |

First trades (check your implementation trade by trade; prices include the spread on buys):

| Entry (UTC) | Side | Entry | Stop-loss | Exit (UTC) | Exit | Reason | Lots | Net P&L |
|---|---|---|---|---|---|---|---|---|
| 2018-01-24 09:20 | buy | 1348.478 | 1343.961 | 2018-01-24 20:50 | 1357.880 | end of day | 2.27 | +2,118.38 |
| 2018-01-25 14:15 | sell | 1355.891 | 1360.861 | 2018-01-25 16:25 | 1360.981 | stop | 2.05 | −1,057.95 |
| 2018-01-29 12:10 | sell | 1345.177 | 1350.809 | 2018-01-29 20:50 | 1341.790 | end of day | 1.79 | +593.81 |
| 2018-01-30 09:50 | buy | 1341.804 | 1335.977 | 2018-01-30 18:25 | 1335.977 | stop | 1.78 | −1,049.69 |
| 2018-01-31 13:40 | buy | 1345.166 | 1339.270 | 2018-01-31 18:00 | 1339.270 | stop | 1.74 | −1,037.95 |

Last trades of the run:

| Entry (UTC) | Side | Entry | Stop-loss | Exit (UTC) | Exit | Reason | Lots | Net P&L |
|---|---|---|---|---|---|---|---|---|
| 2026-07-30 13:35 | buy | 4093.716 | 4061.308 | 2026-07-30 20:50 | 4099.940 | end of day | 0.83 | +510.80 |
| 2026-07-31 09:20 | sell | 4053.709 | 4085.755 | 2026-07-31 20:50 | 4051.480 | end of day | 0.84 | +181.36 |
| 2026-08-05 11:25 | buy | 4180.276 | 4148.834 | 2026-08-05 20:50 | 4244.050 | end of day | 0.86 | +5,478.51 |

Robustness of the width 1.0 configuration (`hermes analyze 2fe13c7c`):

| Check | Result |
|---|---|
| Costs 0x / 1x / 2x | Sharpe 1.34 / 1.10 / 0.85; PF 1.29 / 1.23 / 1.18; max DD −15.4% / −17.2% / −20.5% |
| First 70% / last 30% of the period | Sharpe 1.11 (779 trades) / 1.16 (330 trades) |
| Sharpe 95% confidence interval | 0.31 to 1.84; probabilistic Sharpe 0.998 |
| Monte Carlo over trade order (1,000 runs) | median max DD −18.9%, **worst −45.9%** |
| Width sweep (sweep floor is 1.0) | 1.0: 1.10 · 1.125: 1.14 · 1.25: 1.06 |
| Stop sweep | 0.75: 0.58 · 0.875: 0.95 · 1.0: 1.10 · 1.125: 1.13 · 1.25: 1.06 |
| Regimes (trend × volatility) | all four positive: Bull/High-Vol 0.99, Bull/Low-Vol 0.93, Bear/High-Vol 1.69, Bear/Low-Vol 0.77 |
| Concentration | top 5 trades = 5.1% of winning P&L |
| Evidence tier | deployable (sample size), which in Hermes' words means *ready to forward test* |
| Not measured | walk-forward at width 1.0 (1.0 is outside the tested grid), random-entry baseline |

### 7.2 Post-freeze window, width 1.0 — run `401afaa1725f0a47`

2026-06-01 → 2026-10-10 (June–July is warm-up for the 20-day median); count trades entered on or
after 2026-08-06: **22 trades, 13 wins, net +$629.6** (≈ +0.6 R in total). Too few to mean anything;
use it as a second trade-by-trade check:

| Entry (UTC) | Side | Entry | Stop-loss | Exit | Reason | Net P&L |
|---|---|---|---|---|---|---|
| 2026-08-06 13:05 | sell | 4246.58 | 4283.10 | 4236.63 | end of day | +276.6 |
| 2026-08-07 09:40 | buy | 4317.43 | 4280.59 | 4339.78 | end of day | +601.4 |
| 2026-08-10 16:00 | buy | 4362.90 | 4327.23 | 4388.87 | end of day | +751.1 |
| 2026-08-12 09:15 | buy | 4416.92 | 4381.51 | 4408.42 | end of day | −248.4 |
| 2026-08-13 14:30 | sell | 4363.31 | 4400.32 | 4349.93 | end of day | +372.7 |
| 2026-08-14 11:35 | buy | 4363.00 | 4325.95 | 4375.48 | end of day | +347.5 |
| 2026-08-21 09:05 | buy | 4586.12 | 4547.46 | 4605.77 | end of day | +528.7 |
| 2026-08-24 11:35 | buy | 4660.90 | 4621.47 | 4650.43 | end of day | −273.9 |
| 2026-08-25 13:20 | sell | 4617.15 | 4656.60 | 4656.72 | stop (15:35) | −1,030.6 |
| 2026-09-01 09:05 | sell | 4368.39 | 4409.59 | 4328.42 | end of day | +997.4 |
| 2026-09-03 11:55 | buy | 4444.76 | 4405.63 | 4473.03 | end of day | +733.2 |
| 2026-09-08 12:15 | sell | 4386.96 | 4424.75 | 4347.52 | end of day | +1,063.1 |
| 2026-09-09 11:45 | buy | 4413.62 | 4376.68 | 4376.68 | stop (15:20) | −1,036.2 |
| 2026-09-11 12:30 | sell | 4299.82 | 4337.44 | 4341.62 | stop, same bar | −1,172.4 |
| 2026-09-11 12:40 | buy | 4361.74 | 4324.00 | 4347.45 | end of day | −387.8 |
| 2026-09-14 09:15 | sell | 4302.40 | 4341.40 | 4297.19 | end of day | +133.6 |
| 2026-09-16 10:00 | buy | 4341.75 | 4304.57 | 4304.57 | stop (18:30) | −1,043.2 |
| 2026-09-16 18:35 | sell | 4274.77 | 4311.31 | 4265.97 | end of day | +244.4 |
| 2026-09-17 11:05 | buy | 4336.45 | 4294.71 | 4342.23 | end of day | +137.0 |
| 2026-09-28 09:05 | sell | 4141.14 | 4177.06 | 4112.97 | end of day | +814.9 |
| 2026-10-02 12:30 | buy | 4197.83 | 4164.31 | 4164.31 | stop (14:50) | −1,041.4 |
| 2026-10-02 15:45 | sell | 4132.87 | 4167.63 | 4137.57 | end of day | −138.2 |

(Equity here starts at $100k on 2026-06-01, so sizes are not comparable with 7.1.)

### 7.3 The validated configuration: width 1.31, stop 1.0 — run `47b6e2dfdf313cb6`

For comparison; this is the version that went through the full rubric and walk-forward.

| Metric | Value |
|---|---|
| Trades | 540 |
| Total return / Sharpe | +86.1% / 0.92 |
| Max drawdown | −20.2% (Monte Carlo worst −31.8%) |
| Profit factor / win rate | 1.29 / 48.0% |
| Costs 0x / 1x / 2x (Sharpe) | 1.08 / 0.92 / 0.77 |
| First 70% / last 30% (Sharpe) | 0.79 / 1.21 |
| Anchored yearly walk-forward 2021–2026.08, stitched out-of-sample | 480 trades, Sharpe 1.23, PF 1.37, DD −12.9%, every year positive |
| Sharpe by width (sweep, full sample) | 1.0: 1.10 · 1.15: 1.28 · 1.31: 0.92 · 1.47: 0.57 · 1.64: 0.86 |
| Sharpe by stop (sweep) | 0.75: 0.66 · 0.875: 0.82 · 1.0: 0.92 · 1.125: 0.96 · 1.25: 0.97 |
| Correlation with gold buy-and-hold | daily −0.03, monthly −0.09; beta −0.03 |

---

## 8. Caveats to carry into the new project

- **Contamination.** 2021–2026 was examined by several event studies before the engine runs; the
  only clean data are after 2026-08-06 and the forward test itself.
- **Width 1.0 is a full-sample choice.** Its numbers (7.1) are in-sample; the walk-forward evidence
  is for the 1.15–1.31 range. The sweep shows a broad plateau (all widths 1.0–1.64 positive), so
  this is a reasonable but unvalidated point.
- **Monte Carlo drawdown** is the planning number, not the historical one.
- **Hermes could not forward test.** Everything above is backtest.
- Mechanism is plausible, not proven; gold being the only one of five instruments that worked is
  also consistent with luck at a 1-in-5 rate.

## 9. Where the evidence lives (Hermes repo)

| Path | What |
|---|---|
| `strategies/misc/gold_tokyo_breakout_v2.py` | rules (width, stop as Parameters) |
| `strategies/misc/gold_tokyo_breakout_v2_flat.py` | **the spec'd strategy** (flat at 20:50) |
| `research/tokyo/FINDINGS.md` | full research trail, including rejected variants |
| `research/tokyo/tokyo_v2_walkforward.py` | yearly anchored walk-forward |
| `research/silver/FINDINGS.md` | why silver was dropped |
| `research/ideas.json` → `0945d084` | backlog entry with all run keys |

Re-run a reference in Hermes: `.venv/bin/hermes run gold_tokyo_breakout_v2_flat -p width_ratio=1.0`
(returns from cache with key `2fe13c7c…` while the strategy files are unchanged).
