# S&P 500 Volatile-Dip Limit Buy: strategy specification

A self-contained spec for implementing and forward testing this strategy outside Hermes.
Everything needed to reproduce the rules, check an implementation and judge the forward test
is here. Evidence lives in the Hermes repo (`research/dip_buy/FINDINGS.md`, idea `6f4f38e2`).

| | |
|---|---|
| Status | Backtest validated 2026-10-15. **Ready to forward test, not proven live.** |
| Instrument | US S&P 500 stocks, long only. Venue: Pepperstone share CFDs (`<TICKER>.US`) |
| Timeframe | Daily bars, regular US session (09:30–16:00 America/New_York) |
| Holding | 1–10 trading days (median 2) |
| Origin | Published short-term mean-reversion rule set (Connors/Alvarez style), tested unchanged |

## 1. Idea

In an established uptrend, a sharp one-day selloff in a volatile stock is mostly
liquidity-driven: forced or impatient sellers, stop cascades, overreaction. It is not
information that breaks the trend. A deeper limit order buys only when the selling extends
into the next session, which is when the overshoot is largest. The position is sold into
the first rebound. The edge grows with the stock's short-term volatility, and the selection
rule exploits that.

## 2. Rules

All constants are fixed book values. **Do not tune them during the forward test.**

### 2.1 Definitions (computed on each day's final regular-session bar)

```
TR[t]      = max(High[t] - Low[t], |High[t] - Close[t-1]|, |Low[t] - Close[t-1]|)
ATR5[t]    = simple mean of TR[t-4..t]            # NOT Wilder smoothing
SMA200[t]  = simple mean of Close[t-199..t]
DayRet[t]  = Close[t] / Close[t-1] - 1
VolPct[t]  = 100 * ATR5[t] / Close[t]
```

Prices are split-adjusted, so a split inside the window must not create a fake true range.

### 2.2 Universe

Current S&P 500 constituents that are tradable at the venue: **478 of 503** on Pepperstone
as of 2026-10-15. Refresh the constituent list and the venue's symbol list monthly. Skip names
the venue does not list; the next-ranked candidate takes the slot (cost measured: ~0.8%/yr
CAGR, same Sharpe).

### 2.3 Signal (evaluated after the close of day t)

A stock is a **candidate** for day t+1 when all of these hold:

1. it is in the universe on day t;
2. `Close[t] > SMA200[t]` (uptrend);
3. `DayRet[t] < -0.03` (closed down more than 3%);
4. `VolPct[t] > 3` (sufficiently volatile);
5. no position is currently open in it.

### 2.4 Selection and sizing

- **10 slots**, each sized at **10% of account equity** (equity marked at the day-t close).
  No leverage beyond 1x notional in total, even on a CFD account.
- `free = 10 - open positions`. Sort candidates by `VolPct[t]` **descending** and place orders
  for the top `free` only. Do not over-order to compensate for orders that won't fill.
- If cash is short for a full slot, size the order to the remaining cash, or skip it.
- One position per stock.
- **Forward test at 10 × 10%.** Sizing changes risk, not the edge: Sharpe was 0.87–0.99 for every
  sizing tested (2011–22, swap included). After the forward test, scale with fewer, bigger slots
  rather than leverage. Example: 3 × 33% unlevered gave vol 13.6%, CAGR 13.2%, MaxDD −19.7%, but
  one gap like CRWD's then costs ~10% of the account. 10 × 20% at 2x gave a similar CAGR with MaxDD
  −22.8% and a −14% worst day.

### 2.5 Entry (day t+1)

- **Buy limit** at `L = Close[t] - 0.9 * ATR5[t]`, a **day order** for the t+1 regular session only.
- If the stock opens below `L`, the order fills at the open (better than `L`).
- If it is not filled by the close of t+1, cancel it. The slot stays empty that day.

### 2.6 Exits (first to trigger wins)

Let e be the entry day.

| # | Exit | Active from | Trigger | Fill |
|---|---|---|---|---|
| 1 | **Profit target** | day e+1 | `High[d] >= T[d]`, where `T[d] = Close[d-1] + 0.5 * ATR5[d-1]` | resting sell limit at `T[d]`; at the open if it opens above `T[d]` |
| 2 | **Rebound** | close of day e | `Close[d] > High[d-1]` | sell at the **next open** |
| 3 | **Time** | close of day e+10 | 10 trading days held | sell at the **next open** |
| 4 | Index removal / delisting | — | leaves the universe | sell at that day's close (or next open) |

- **The target is re-set every evening.** After each close, cancel yesterday's take-profit and
  set `T = today's Close + 0.5 * today's ATR5` for the next session. On the entry day, set it
  after the close, so it is live from e+1.
- **The target can be below the entry price** after further declines. It is then a loss-taking
  exit, and that is intended (see the CF reference trade).
- **No stop loss.** Stops were tested and every variant was worse (§6).
- On the evening of day d, if the rebound or time rule triggers, send a market-on-open sell for
  d+1 and cancel the take-profit. If the target fills intraday on d, the position is closed and
  the evening rules are skipped.

### 2.7 Daily schedule (America/New_York)

| When | Action |
|---|---|
| After 16:00 close (bars final) | Mark equity. For each open position: check rebound/time (queue next-open sell) or re-set the take-profit. Compute candidates, rank, compute limit prices. |
| Before 09:30 next session | Cancel stale orders. Place exit market-on-open orders, take-profit limits and entry day-limit orders. |
| 16:00 | Unfilled entry limits expire. |

## 3. Costs assumed (Pepperstone share CFDs, read from the account 2026-10-15)

| Item | Value | Per trade (2023–26 trades) |
|---|---|---|
| Commission | USD 0.02 per share per side, min 0.02 | ~0.05% round trip |
| Overnight swap, long | −6.35%/yr of notional (benchmark-linked; check monthly) | ~0.07% (avg 3.7 nights) |
| Spread / slippage | assumed 3 bp per side (**not measured**) | ~0.06% |
| Minimum size | 0.1 share | — |

The edge survives up to ~23 bp per side. At 53 bp per side it is gone.

## 4. Reference trades (implementation parity check)

Recomputed from yfinance adjusted daily bars. A correct implementation on the same bars should
reproduce these to the cent. On Pepperstone's own feed, expect small price differences.

**META, target hit next day**
```
signal 2026-09-25  prev close 777.59  close 751.66  DayRet -3.33%  SMA200 625.90
                   ATR5 41.937  VolPct 5.58  -> limit 751.66 - 0.9*41.937 = 713.92
2026-09-28  O 750.04 H 750.58 L 713.19 C 715.62  -> low <= 713.92, bought at 713.92
            ATR5 32.181 -> target for 09-29 = 715.62 + 0.5*32.181 = 731.71
2026-09-29  O 725.00 H 740.45 -> high >= 731.71, sold at 731.71   (+2.49% gross)
```

**EXPE, target hit next day**
```
signal 2026-09-08  close 274.55  DayRet -7.88%  ATR5 14.182  VolPct 5.17  -> limit 261.79
2026-09-09  O 266.40 L 261.43 C 272.60  -> bought at 261.79; target for 09-10 = 279.57
2026-09-10  O 269.60 H 283.72 -> sold at 279.57   (+6.79% gross)
```

**GLW, target below the entry-day open**
```
signal 2026-10-07  close 163.25  DayRet -3.39%  ATR5 8.064  VolPct 4.94  -> limit 155.99
2026-10-08  O 158.26 L 150.94 C 152.83 -> bought at 155.99; target for 10-09 = 157.11
2026-10-09  O 156.47 H 159.18 -> sold at 157.11   (+0.72% gross)
```

**CF, falling knife, target trails down, gap fill at the open (loss)**
```
signal 2026-09-18  close 127.70  DayRet -4.57%  ATR5 5.850  VolPct 4.58  -> limit 122.44
2026-09-21  O 126.59 L 122.21 C 123.27 -> bought at 122.44; target 126.02
2026-09-22  H 122.40 < 126.02; C 120.59 -> target 123.15
2026-09-23  H 122.61 < 123.15; C 120.64 -> target 122.98
2026-09-24  H 122.19 < 122.98; C 118.44 -> target 120.78
2026-09-25  H 117.98 < 120.78; C 114.67 -> target 116.78
2026-09-28  O 117.30 >= 116.78 -> sold at the open, 117.30   (-4.20% gross)
```

## 5. What to expect: the forward-test yardstick

Backtest, Pepperstone-listed names only, 2023-01 to 2026-10, 3 bp/side (the holdout period;
rules frozen before it was scored):

| Metric | Value |
|---|---|
| Trades | 444 (~120/yr; 67 in 2023, 171 in 2026) |
| Avg / median trade | +0.98% / +1.77% (sd 4.65%) |
| Win rate | 67%; avg win +3.3%, avg loss −3.9% |
| 5th / 95th pct trade | −7.1% / +7.0% |
| Worst trades seen | −31.6% (CRWD, 2024 outage, all names), −22% (FCX, 2022) |
| Exit mix | target 89%, rebound 9%, time 0.5% |
| Hold | mean 2.6, median 2 trading days |
| CAGR / Sharpe | 12.1% / 1.27 (cash earns 0% in the backtest) |
| Max drawdown | −7.6% (MC worst over 2011–22: −11.6%) |
| Months negative | 29%; worst month −3.1% |
| Slot use | avg ~1.2 of 10 open; up to 10 in a sector selloff |

Longer history, 2011–2022, all names: +0.90%/trade, Sharpe 0.93, 15 of 16 years positive, worst
year −1.9% (2011).

### 5.1 Forward-test decision rules (fix these before starting)

- **Sample.** ~100 trades (about 8–10 months) before any verdict. With a per-trade sd of 4.65%,
  the standard error at 100 trades is ±0.47%.
- **Continue** if the mean trade after costs is > +0.3% at 100 trades.
- **Stop and review** if the mean is ≤ 0 at 100 trades (about 2 SE below expectation), or if the
  drawdown exceeds **−15%** at any point (beyond the backtest's Monte Carlo worst case).
- **Fill quality, judged separately from P&L:**
  - entry fill vs limit `L`: average slippage should be ≤ 3 bp;
  - target fill vs `T`;
  - fill rate: the share of limit orders filled vs the backtest's, on the same signals.
- **Signal parity.** Re-run the backtest over the forward period each month. ≥ 95% of signals
  and orders should match. A mismatch is an implementation or data bug until proven otherwise.

### 5.2 Log every order

Log: date, ticker, `Close[t]`, `ATR5[t]`, `VolPct`, rank, limit `L`, filled?, fill price, each
evening's `T`, exit reason, exit price, commission, swap, equity, and open slots.

## 6. Tested and rejected (do not re-add without new evidence)

| Variant | Result |
|---|---|
| Stop loss 1/1.5/2/3 × ATR5, 10%, 15% | All worse, monotonically. The worst losses gap through the stop overnight. |
| Market entry at next open (no limit) | +0.22%/trade, −31% drawdown. The limit is the edge. |
| VolPct floor 4 instead of 3 | Better per trade, same portfolio Sharpe. Not adopted. |
| Every constant moved one at a time (drop, vol floor, depth, target, max hold, ATR length, SMA length, slots; 38 points, 2011–22) | All profitable, Sharpe 0.71–1.20; the book point (0.93) is mid-range. Removing the SMA200 filter doubles the drawdown (−23.8%). Deeper limits (1.1–1.3) look better in-sample but are not adopted during the forward test. |
| Splitting unlisted names to a second broker | +0.8%/yr CAGR, same Sharpe, deeper drawdown. Not worth it. |

## 7. Known risks

1. **Survivorship.** ~300 delisted former members had no data; the real tail is somewhat worse.
2. **Correlated slots.** In a sector selloff (e.g. semiconductors, July 2026) several slots fill
   with the same bet. The worst backtest day was −7.7%. A per-sector cap is untested.
3. **News gaps.** No stop can help with gaps (CRWD −31.6%). Each slot is 10%, so one disaster ≈ −3%.
4. **Fill assumptions.** The backtest fills on a touch of the limit. Requiring a 0.2% trade-through
   cost ~0.1%/trade. Real queue position is unknown, which is what §5.1 measures.
5. **Swap.** It tracks US rates. Higher rates mean a higher cost, but it stays small at ~3-night holds.

## 8. Implementation notes for Pepperstone / cTrader

- Symbols are `<TICKER>.US` (e.g. `AAPL.US`, `BRK.B.US`). Ignore the `-24` and `_SB*` variants.
  Volume is in 1/100 share (`minVolume` 10 = 0.1 share).
- **Use the regular-session daily bar.** Verify that the venue's D1 bar boundaries match the
  09:30–16:00 ET session before trusting the signals. Pepperstone share-CFD history starts
  2019-08, so a parity check on venue data is possible from then on.
- Commission and swap terms: query them from the account (`ProtoOASymbolByIdReq`:
  `commission`, `swapLong`, `swapCalculationType`) rather than hard-coding.
- If the new repo lives in `~/dev`, generate it from the platform template
  (`platform/docs/new-project.md`). cTrader credentials come from the shared SSM pair
  `/platform/shared/ctrader-*` (`platform/docs/conventions.md` § Secrets). Never refresh or copy
  the token.
- Run on a **demo account** first. The forward test is the evidence, so do not change the rules
  during it.
