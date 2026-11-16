# S&P 500 volatile-dip limit buy: findings (idea 6f4f38e2)

**Verdict (2026-10-15): the edge looks real. It survives 2x costs and the 2023-26 holdout,
and it is ready to forward test.** Not deployable: Hermes cannot forward test, and the data is
survivor-biased (below).

## Rule (book values, untuned)

Point-in-time S&P 500; close > SMA200; close down > 3% vs prior close; 100*ATR(5)/close > 3;
next day limit buy at close - 0.9*ATR(5); with 10 equal slots, prefer the highest ATR(5)/close.
Exit: target = previous close + 0.5*ATR(5) (from the day after entry), or a close above the
previous day's high (exit at the next open), or after 10 bars (exit at the next open). 5 bp a side.
ATR(5) is a simple mean of the true range.

## Evidence

Event study `dip_study.py` (models the cross-sectional ranking) and the engine
`strategies/dip_buy/dip_buy_sp500.py` (one leg per symbol, no ranking: first come by symbol).

| | trades | avg/trade | t | Sharpe | CAGR | MaxDD |
|---|---|---|---|---|---|---|
| IS 2011-22, study | 991 | +0.90% | 6.5 | 0.93 | 7.5% | -11.6% |
| IS, random uptrend entries, same exits | ~1220 | +0.19 to +0.20% | | 0.36-0.39 | | |
| IS 2011-22, engine `9a4033697a743c49` | 959 | | | 0.83 | 5.6% | -10.8% |
| **Holdout 2023-26, study** | 451 | +0.99% | 4.4 | **1.22** | 12.3% | -9.8% |
| Holdout, study, 2x costs | 451 | +0.89% | 4.0 | 1.10 | 11.0% | -9.8% |
| Holdout, random entries | ~630 | -0.02 to +0.17% | | -0.05 to 0.50 | | |
| Holdout, engine `ee8902705d645df2` | 417 | | | 0.83 | 7.1% | -10.1% |

- `analyze` (engine, IS): tier **deployable** (959 trades / 12y), probabilistic Sharpe 0.996,
  Sharpe 95% CI [0.25, 1.43], MC worst MaxDD -11.6%, top-5 trades 4.1% of wins.
  Holdout: tier **credible** (417 / 3.8y), PSR 0.94, Sharpe CI [-0.26, 1.98], MC worst -13.7%.
- `analyze` axes **not measured** on universe runs: `costs`, `params`, `oos`, `walkforward`,
  `baseline` (no re-run path for universe runs), `regime` (no benchmark), `gate` (no advisor).
  Substituted by the study: 2x costs (above); depth sweep 0.6/0.75/0.9/1.05/1.2 gives IS Sharpe
  0.79-0.86 (pre-fix code), a **plateau**; IS/holdout split by date; random-entry control.
- Per year (study, fixed): 15 of 16 years positive, worst 2011 -1.9%; 2020 crash -4.9%,
  2022 bear +1.8%. The SMA200 filter switches it off in breakdowns.
- Engine vs study: the 307 shared holdout trades have identical exits and returns. The gap is
  selection when slots bind: the ATR/close-ranked picks averaged +1.7%, the engine's
  symbol-order picks +0.5%. The book's ranking rule earns its keep out of sample.

## Dissection (IS)

- Edge is monotone in volatility: 100*ATR5/close 3-3.4 earns ~0 (t 0.5), > 4.9 earns +1.9%/trade
  (t 5.0). That's the mechanism the ranking encodes. A floor of 4 (variant B) nearly doubled
  per-trade edge but halved trades. Portfolio Sharpe did not improve (holdout 1.22 vs A 1.48,
  pre-fix code). **Not adopted.**
- The limit entry matters: buying at the next open instead gives +0.22%/trade, MaxDD -31%.
- Capital is idle: on average 0.7 of 10 slots are used, and 65% of days are flat. The
  modest CAGR comes from low exposure, not from a weak edge.
- Friday entries are weaker. No mechanism, so dropped.

## Trail

Trials scored on IS: 11 (A; open-entry; depth x4; slots 5/20; fill 0.2% through the limit;
vol floor 4). Holdout touches: 3 (A, B, and A again after the bug fix below). Engine runs: 2.

Bug found by the engine cross-check: the cached yfinance folder includes 27 FX pairs whose
dates fall on weekends and holidays. Their dates leaked into the study's day index and made
a target skip after each phantom day. Fixed by restricting the panel to ever-S&P members and
using New York dates. Pre-fix numbers in this file are marked.

## Threats

1. **Survivorship:** ~300 fully delisted ever-members have no yfinance OHLC. Dip buying is
   exactly where the missing falling knives (bankruptcies, takeunders) would hurt. Tiingo
   OHLC for them (paid) would close this.
2. Limit fills on touch: a 0.2% trade-through requirement cost ~0.1%/trade IS, so the result
   is not touch-dependent, but queue position is unknowable.
3. Earnings gaps are not separated. A miss that gaps far below the limit fills at the open,
   and those are the -20% tail trades.

## Stop losses (2026-10-15, IS 2011-22 only, 6 more trials; holdout not touched)

Stop live from the entry bar, gap through fills at the open, stop-first on a clash.

| stop | avg/trade | Sharpe | CAGR | MaxDD | worst trade | stopped |
|---|---|---|---|---|---|---|
| none (book) | +0.90% | 0.93 | 7.5% | -11.6% | -22.0% | 0 |
| 1 ATR(5) | +0.38% | 0.53 | 3.2% | -9.7% | -16.5% | 341 |
| 1.5 ATR | +0.53% | 0.65 | 4.4% | -11.4% | -21.2% | 197 |
| 2 ATR | +0.65% | 0.74 | 5.4% | -10.8% | -21.2% | 116 |
| 3 ATR | +0.72% | 0.78 | 6.0% | -12.1% | -23.0% | 49 |
| 10% | +0.58% | 0.67 | 4.7% | -11.2% | -21.2% | 100 |
| 15% | +0.77% | 0.82 | 6.4% | -11.1% | -21.2% | 27 |

Every stop is worse, monotonically with tightness, and none cuts the drawdown or the worst trade
much: the worst losses gap through the stop overnight (29 of 100 trades stopped at 10% filled
beyond -10%, avg -11.4%). Trades a 2 ATR stop closes are losers anyway (2% would have ended
in profit), but the stop sells near the low: -8.8% with the stop vs -6.7% when left to the
target/time exit, which catches the bounce. The tail risk here is overnight gaps and correlated
sector selloffs; a price stop cannot address either. Position size and a sector cap can.

## Parameter sensitivity (2026-10-15, IS 2011-22, one at a time, `sensitivity.py`)

38 off-book points, none used to change the rule. **All 38 are profitable (t 4.8-7.7), Sharpe
0.71-1.20, and the book point (0.93) sits mid-range, not on a peak.**

- drop 2-5%: Sharpe 0.80-0.98; -5% falls to 0.71 on few trades (395).
- vol floor 2-4: 0.89-0.96, flat. Stricter means more per trade and fewer trades.
- depth 0.5-1.3: 0.86-1.20, rising with depth; DD -14.5% -> -5.3%, trades 2096 -> 440. A
  trend, not a spike, and consistent with the mechanism (a deeper overshoot pays more).
- target 0.25-1.5 ATR: 0.87-0.96; larger targets raise CAGR (6.5 -> 10%) but DD grows
  (-9.6 -> -21.9%) because positions sit through further declines.
- max hold 3-20: identical from 5 up (time exit almost never binds); 3 slightly worse.
- ATR length 3-14: 0.84-1.06, flat.
- SMA 100-250: 0.85-0.99; **no trend filter: Sharpe 0.78, DD -23.8%.** The filter's main job
  is risk control.
- slots 5-20: 0.88-0.96; fewer slots raise CAGR and DD together (sizing, not edge).

Same-direction tightening of the dip (bigger drop, higher vol floor, deeper limit) always raised
per-trade return. That is evidence for the overshoot mechanism, not a tuning target. Depth
1.1-1.3 looks better IS but is not adopted mid-forward-test; it is a candidate to test on data
after the forward test starts. One-at-a-time does not measure interactions.

## Portfolio (step 4)

`hermes correlate` vs the frozen gold Tokyo breakout `2fe13c7c79f3ee20` (2018-26.08):
with `9a4033697a743c49`, -0.05 daily / -0.04 weekly (diversification ratio 0.71 / 0.73); with
`ee8902705d645df2`, -0.00 daily / +0.03 weekly (0.72 / 0.77). A separate bet. Both streams
are engine runs, so the dip side is the unranked version.

## Next

Forward test (paper) the ranked rule, 10 slots. Close the survivorship gap first if budget
allows. Step 4: `hermes correlate` against the gold Tokyo breakout. Different asset and
mechanism, so likely additive. Consider larger slots (5) only as a sizing decision after
forward testing.
