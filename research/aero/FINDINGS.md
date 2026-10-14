# Aero MT5 — what the logs show (reverse-engineering notes)

Sources: MT5 tester logs `20261009.log` (2026-01-01..08-06) and `20261009 copy.log`
(2018-01-01..2026-08-06, same inputs), the Myfxbook account (2026-09-21..10-09), Pepperstone
XAUUSD 1m/5m/15m bars. Backlog ideas: 87eb623e, 15f8fcda, e61a4161, 7f3d026e, af10eff8.

Conventions: **server time** = UTC+2, UTC+3 during EU DST (server midnight = 17:00 New York).
**ATR** = Wilder ATR(20) on H4 (the EA's logged ATR reads ~7% higher). **AUC** = how well a
feature separates the EA's orders from breaks it skipped while free to trade (0.5 = not at all).
**Take rate** = share of breaks in a bucket the EA placed an order on. **R** = event-simulation
net P&L / ATR (replica exits, $0.20/oz costs). Universe for selection stats: 24,964 day-range
breaks (5m) with the EA flat, 498 EA orders matched.

## A. The level

| # | Finding | Numbers |
|---|---|---|
| 1 | The level is the **current broker day's running high/low** (since 00:00 server), not the previous day's | 2026 log: 65% of limit prices within $1.5 of it (most within $0.30); previous-day high/low 20%. Full log: 530/704 orders |
| 2 | Same levels seen as swings: **M30 fractal, 1 bar each side** fits best | 553/704 orders (H1 n=1: 525, H1 n=2: 478) |
| 3 | **Dominant swing**: beyond the previous swing *and* a lower swing formed after it | 29% of breaks, 68% of orders; take 3.5%; neither condition 1 in 8,409 |
| 4 | **The level must have held for hours** (strongest single factor) | AUC 0.742; picks median 2.7h vs 0.08h. Take: <15m 0.9%, 15–30m 1.4%, 30–60m 1.1%, 1–2h 1.5%, **2–4h 4.2%, 4–8h 9.4%, 8–24h 15.5%** |
| 5 | **Real pullback** under the level before the break | AUC 0.677; median 0.54 vs 0.24 × ATR (look-alikes: 0.97 vs 0.73) |
| 6 | **Tested before**: touches within 0.1 × ATR while it held | AUC 0.661; median 2 vs 0 |
| 7 | Position vs yesterday's range is **not** a factor | level median 0.27 × ATR inside yesterday's extreme; AUC ~0.46 |

## B. Timing

| # | Finding | Numbers |
|---|---|---|
| 8 | **No entries 00:00–02:30 and 23:00–24:00 server** (inputs PauseAfterOpen=150, PauseBeforeClose=60) | breaks during the pause get an order at 02:30:00 if still valid |
| 9 | **Afternoon / New York session** | picks median 14:00 server vs 10:45; tree: level age > 3.2h **and** hour > 12:10 → take 5–10%; old level before 12:00 → 0.5% |
| 10 | With the 6-month trend helps a little | within (old level, afternoon): r26w > 0.45 ATR → take 10.3% vs 5.1% |
| 11 | Weekday: no preference | AUC 0.47–0.49 |

## C. How price arrives

| # | Finding | Numbers |
|---|---|---|
| 12 | **Fast arrival on 5m**: far from the level at the last closed 5m bar | `gap_now` AUC 0.635; holds 0.59–0.70 after controlling for trigger overshoot (feed-mismatch check) |
| 13 | **Sharp arrival on 1m**: further away 1–3 min before the trigger | 1 min before: 0.040 vs 0.017 × ATR (AUC 0.632); 2 min 0.628; 3 min 0.615 |
| 14 | **Not a grind**: little time pressing against the level | minutes of last 30 within 0.1 × ATR: 5 vs 8 (AUC 0.385); last 0.1 × ATR covered in 3 vs 4 min (0.396) |
| 15 | **Not after an extended run-up** | distance 2h ago (4 M30 bars) lower for picks among large-overshoot breaks (AUC ~0.40) |
| 16 | No signal | tick volume before the break (AUC ~0.50), consecutive closes, path straightness, spikes, M30 bar size |

## D. Pacing / state

| # | Finding | Numbers |
|---|---|---|
| 17 | **≤ 2 orders per broker day**, rarely the same side twice | days with 1 order: 450, with 2: 127; per day per side 1: 540, 2: 82 |
| 18 | **Cooldown after a trade** | take after becoming flat: <30m 0.1%, 0.5–1h 0.5%, 1–2h 1.0%, 2–4h 1.5%, 4–8h 3.6%, 8–24h 4.3% |
| 19 | One position at a time | |

## E. Execution

| # | Finding | Numbers |
|---|---|---|
| 20 | **Trigger**: price trades **$0.50 through** the level; tick-timed | bid/ask offset 0.50–0.56 on every order; 7% of orders at :00 seconds (mostly the 02:30:00 session open) |
| 21 | **Entry**: limit at the level; filled in ~0.6 min median | 2026 log fill rate 61% |
| 22 | **Market chase** if not filled in ~1.5 min (1.1–2.0) | 303 of 577 trades are chases, 57% of profit; win 70.6% (chase) vs 79.2% (limit); live: 4 of 12 chased $2.6–7.8 beyond the level |
| 23 | **Take-profit** $20 (input 200), almost never reached | hit 6 of 577 |
| 24 | **Trail**: at ~+$12 the stop jumps to entry + $10, then steps $0.35 | realised win $10.35–11.40/oz median, every year 2018–2026 |
| 25 | **Catastrophe stop** ≈ 3.6 × ATR | never hit |
| 26 | **"Recovery grid"**: 3h after entry, if losing, limits at entry ∓0.5 / 1.0 / 1.5 × ATR with growing lots; the first fill closes the whole basket | effectively a stop at −0.5 × ATR armed after 3h; losses ~$18–30/oz |
| 27 | **Hedge basket** (live only): sells right after a buy loser closed at Sunday's open | 28 Sep 2026, +€7,087 = 74% of the account's profit; not modelled |
| 28 | Sizing: RiskPercent against the catastrophe stop → small lots | |

## F. Results to remember

| # | Finding | Numbers |
|---|---|---|
| 29 | EA tester 2018–2026.08 | $10k → $27.2k, 577 trades, every year profitable (PF 2.2–8.1), win ~75% |
| 30 | Live Aero trades (Sep–Oct 2026) | 9 of 11 trades won; single trades 7/8 at +$9.8–12.7/oz |
| 31 | **Unfiltered breaks lose**, except in 2025–26 | replica gross PF ≤ 1.01 in every year 2018–24 |
| 32 | **Breaking a just-made extreme loses** | levels < 2h old: R −0.031 to −0.048 (2018–22) |
| 33 | **Old + fast breaks**: the only group positive in both periods | +0.017 R (2018–22), +0.026 R (2023–26) — ~$0.6/oz, too small after doubled costs |
| 34 | Selection is **~85% reproducible** from observables… | logistic mimic fitted 2018–22, AUC 0.850 on 2023–26 choices (0.846 with M1 added) |
| 35 | …but the **profit is not** | mimic top decile ~+0.01–0.03 R vs **EA picks +0.189 / +0.180 R**; among equally EA-like breaks picks +0.185 vs skipped −0.007 |
| 36 | The gap is not a measurement artefact | same trigger overshoot → expected +0.009 R; orders placed on the trigger tick |
| 37 | Winners vs losers among EA trades are nearly indistinguishable | best AUC 0.57 (with yesterday's candle, with the 20-day trend) |
| 38 | Own kNN (20 features, walk-forward) | no reliable edge: 15m p=0.052; 5m month-matched p=0.028 with lift only in 2025–26 |

## Open question

What separates the EA's ~1-in-6 picks from equally good-looking breaks (worth ~+0.18 R)
is not in 1m/5m/higher-timeframe price, tick volume or the EA's state. Next evidence would
come from the live single-trade record or a tester run with the EA's filter disabled.

## G. Rule coverage and selectivity (`research/aero/aero_rule_table.py`)

Universe: 48,700 breaks of a day extreme or an M30 swing (EA flat), 598 of the 704 EA orders
matched. Coverage = % of those 598 orders meeting the rule set; selectivity = % of breaks meeting
it that the EA ordered (baseline 1.23%). Thresholds: R3 held >= 2h, R4 pullback >= 0.40 ATR,
R10 last 5m close >= 0.08 ATR away, R11 close 1 min before the trigger >= 0.03 ATR away.
Highlights: R1+R3 8.7% at 44% coverage; R1+R2+R3 10.6% at 35%; R1+R2+R3+R11 12.2% at 27%;
all six 12.4% at 23%. Full 63-row table: `research/output/aero_rule_table.csv`.

**Swing timeframe (follow-up):** H1 fits better than M30 once the number of levels is accounted
for. Every H1-matched order is also M30-matched; M30 adds only 28 orders (21 of them day
extremes) while doubling the levels. With day extremes: H1 n1 explains 592 orders, M30 n1 599.
Selectivity: M30 n1 1.45%, H1 n1 2.78%, H1 n2 4.34%. In the rule table (`aero_rule_table.py 1h_1`)
R1+R3 is unchanged (8.8% at 44.5% coverage); R1+R2+R3 is 12.6% at 21.3% (H1 n1) vs 10.6% at
35.5% (M30 n1). n=1 vs n=2 on H1 cannot be separated.

## H. Coverage of all 704 orders and the other entry points

Day high/low 530 (75.3%), H1 swing (n=1) 525 (74.6%), both 463; **either 592 (84.1%)**;
adding M30 swings 599 (85.1%). The other 105 orders:

| Group | Orders | What it is |
|---|---|---|
| Session start, placed at exactly 02:30:00 | 35 | a level broken during the 00:00-02:30 pause; limit placed at the level when trading resumes (median $6.2 from the market). Level: previous day's high/low 21, H1 swing 10, H4 swing 3 |
| Placed late, far from the market | 28 | limit at a level price already ran through (median $8.1 away); 25% within 5 min of the EA closing its previous trade. Level: H1 swing 24 (median age 8.5 days, a quarter > 3 weeks), previous day 3 |
| Normal $0.50 trigger, other level | 42 | H1 swing 16 (a quarter > 45 days old), previous day's high/low 12, M15 swing 6, H4 swing 2, round $10 4, unexplained 2 |

So beyond "day high/low or H1 swing broken by $0.50": (1) previous-day high/low breaks,
(2) breaks during the session pause handled at 02:30, (3) late/re-armed limits at an already
broken level (often right after the previous trade closes), (4) old H1 swings (weeks-months),
(5) a few M15/H4 swings and round numbers. All are still breakout entries with a limit at the level.

## I. EA mechanics on every day high/low break (`research/aero/aero_daybreak_sim.py`, 1m bars)

Simulator check on the EA's own 446 logged trades: sim win 79.8% / +$6.65/oz vs EA 76.2% /
+$6.41/oz, outcome agreement 88.8% — the 1m simulator reproduces the EA's mechanics.

| 2018-01..2026-08 | Breaks | Win | $/oz | R | PF | Limit fills |
|---|---|---|---|---|---|---|
| All current-day high/low breaks | 27,603 | 51.6% | +0.25 | +0.008 | 1.02 | 44.0% |
| All breaks, one position at a time | 3,483 | 58.5% | +0.75 | +0.020 | 1.05 | 49.8% |
| Breaks the EA skipped | 27,073 | 51.1% | +0.13 | -0.002 | 1.00 | 44.0% |
| **Breaks the EA ordered** | 530 | **79.8%** | **+6.29** | +0.534 | **3.79** | 43.8% |

EA-ordered breaks earn +$4.7 to +$8.7/oz in every year; all breaks -$0.50 to +$1.67/oz.
Same mechanics, same fill mix: the profit is entirely in which breaks are taken.

## J. Five new candidate features (`research/aero/aero_new_features.py`; outcomes judged on 2018-22 only)

Universe: 27,603 current-day high/low breaks with the EA's 1m-simulated mechanics; "held" = level
held >= 2h (3,434 breaks). Baseline 2018-22: all 44.5% win / -$0.05/oz; held 44.9% / +$0.07.

| Feature | Outcome 2018-22 (low -> high quintile) | EA choice (AUC, held) | Verdict |
|---|---|---|---|
| Room to next obstacle (unbroken H1 swing / prev day-week extreme) | held: win 40.4% -> 50.8%, $/oz -0.51 -> +1.40, monotone | 0.565 | **keep** |
| $12 lock vs volatility (12/ATR, 12/last-hour 1m path) | held: win 55.4% -> 33.4% (12/ATR), $/oz +1.23 -> -0.84 | **0.333 / 0.315** | **keep** (strongest EA preference found among held breaks) |
| Confluence (other levels within $1) | worse, not better: held 34.4% / -$1.62 with confluence vs 46.2% / +$0.29 (n=212) | 0.497 | drop / maybe negative flag |
| Release proxy (08:30/10:00/14:00 NY spike today) | 0-30 min after a spike: all 53.2% / +$1.51; 30 min-8 h after: worse (held -$2.3 to -$2.7) | held take 7.0% at 0-30 min vs 3.8% | **keep** (as minutes-since-release bucket) |
| Post-trigger retest within ~1.5 min | held: retest 46.8% / +$0.22 vs none 43.5% / -$0.02 | n/a (post-trigger) | weak |

EA second decision: 127 of 704 orders were cancelled and never chased. Chases happen with the market
at most $2.89 beyond the limit (median $0.98); abandoned orders had price $4.73 beyond (median) at chase
time. The abandoned breaks would have done as well (sim 81.5% win), so it is a slippage cap, not edge.

## K. Own kNN with the findings' features, EA entry model (`research/aero/aero_knn_v2.py`)

19 features (level age/pullback/touches, 1m+5m arrival, 2h extension, hour, 20d/6m trend, previous
day, room, lock vs ATR and vs 1m path, release timing); target = net $/oz under the EA mechanics;
monthly walk-forward; 48 configs, chosen on 2019-22: held >= 2h breaks, full features, k=50, score > $0.50.
Traded with the EA entry model (limit/chase, abandon > $2.90), one position, <= 2 per day.

| | Trades | Win | $/oz | Total oz | PF |
|---|---|---|---|---|---|
| kNN, 2019-22 (in-sample selection) | 357 | 59.7% | +2.86 | +1,021 | 1.68 |
| **kNN, 2023-01..2026-08 (scored once)** | 553 | 59.1% | **+0.09** | +52 | **1.01** |
| All breaks, EA pacing, 2023-26 | 1,352 | 59.7% | +1.00 | +1,347 | 1.16 |
| Held >= 2h, EA pacing, 2023-26 | 888 | 59.2% | +0.49 | +434 | 1.07 |
| Random picks at the same rate, 2023-26 | | | +0.46 | | |
| EA actual (log), 2023-26 | 417 | 78.2% | +6.15 | +2,565 | 3.27 |

Out of sample the kNN is worse than random picks (P(random >= kNN) = 0.76); by year -1.73, 0.00,
+1.60, -0.56 $/oz. Overlap: 12.9% of kNN picks were EA orders (base 2.6%) — from the held filter;
within held breaks the EA's orders sit at the 48th percentile of the kNN score (unrelated).
P&L correlation with the EA: daily 0.22, monthly 0.35 (shared level type and days).

## L. Silver run and the vendor's parameter manual

Manual (mql5 blog post 770050): TP 200 pips = $20, trail start 100 pips = $10, activation 20 pips =
$2 (confirms the replica); "ten years of tick-level testing"; news filter named only as an "FOMC
blackout"; hedge = separate opposite order with magic + 555000. No kNN details, no training period,
no switch. Tester inputs list (28) has no kNN or news toggle.

Silver (XAGUSD, 2026-01..08-06, same inputs): $10k -> $11,818 (+18.2%), 65 trades, 15% win. The
trigger scales with digits ($0.05) but TP/trail stay in dollars (+$20 / +$10 lock = ~13% on silver),
so 9 trend trades reached the lock (+$10.15/oz each) and 56 closed via the 3h grid (-$0.92/oz).
Not a test of the selection — the exits do not transfer.

Mirrored-gold test files (not committed): research/output/XAUUSD_CTRL_M1.csv (real Pepperstone
prices) and XAUUSD_MIRR_M1.csv (P' = 6757.76 - P), 1m, server time (midnight = 17:00 NY), MT5 import format.

## M. Does the selection transfer to silver? (`research/aero/aero_silver.py`)

Every 1m day-high/low break 2026-01..08-06 scored with the EA's 2026 gold geometry in ATR units
(lock +0.32 ATR -> +0.27, trail, TP +0.54, 3h -0.5 ATR, cat 3.6), EA picks vs all breaks:

| | Breaks | Win | Mean R | P(random >= picks) |
|---|---|---|---|---|
| Gold, all | 5,006 | 72.2% | +0.039 | |
| Gold, EA picks | 73 | 87.7% | +0.182 | 0.004 |
| Silver, all | 3,081 | 72.3% | +0.035 | |
| Silver, held >= 2h | 204 | 70.1% | -0.000 | |
| Silver, EA picks | 50 | 82.0% | +0.162 | 0.036 |

Not explained by gold co-movement: only 12 of 50 silver picks have a gold EA pick within 60 min,
and the edge is larger without one (38 picks +0.187R vs 12 picks +0.086R). Silver picks coincide
with a same-side gold break less often than silver breaks in general (58% vs 73%); picks with no
gold break within 15 min: +0.124R vs -0.058R for those breaks overall.
Reading: a gold-trained selector picks better-than-average SILVER breaks, where it cannot have
stored outcomes — evidence the selection is real, not memorised (small sample, one regime).

## N. kNN lab: different ways to run the kNN (`aero_knn_lab.py`, `aero_knn_lab2.py`)

Dataset: 22,457 gold day-high/low breaks 2018-2026.08 (one per side per 5 min) + 2,226 silver breaks
2026; outcome R = the EA's 2026 gold geometry in ATR units (stationary across years — the dollar
target in aero_knn_v2 was not); EA labels; hand features + raw price shape (30x1m, 24x5m, 24xH1,
20xD1 closes vs the level). Monthly walk-forward, training rows resolved >= 3 days before the month.
Selection on gold 2019-22 only; checks: gold 2023-26 (seen many times — weak) and silver 2026 scored
by a gold-trained model (clean). "Lift" = top-decile mean R minus universe mean R.

| Config | 2019-22 | Gold 2023-26 | Silver 2026 |
|---|---|---|---|
| E2 raw shape, R, k=50 (best in-sample) | +0.065 | +0.031 | -0.003 |
| E6 hand+raw, R, k=50 | +0.059 | +0.030 | +0.006 |
| E1 hand, lock target, k=200 | +0.053 | +0.030 | +0.071 |
| E6 hand+raw, R, k=200 | +0.041 | +0.033 | +0.030 |
| E3 single blocks: 1m / 5m / D1 | -0.03..+0.02 | ~0 | -0.03..+0.04 |
| E3 single block: H1 (24h), k=50/200 | +0.003/+0.011 | +0.021/+0.044 | +0.063/+0.053 |
| E4 imitate the EA (any features) | +0.01..+0.04 | ~0 | negative; EA AUC 0.68-0.80 gold, 0.74-0.84 silver |
| E8 EA filter first, then kNN | -0.001/-0.040 | +0.047/+0.023 | noisy (n=33-39) |
| E9 distance weighted by 2018-22 |corr| (top: last 1m/5m closes, pullback, room_pd), k=50 | +0.018 | +0.018 | +0.031 (6/8 years) |
| E10 blend E1-lock-200 + E6-R-200 (post-hoc) | +0.032 | +0.039 | +0.018 (8/8 years) |

Reading: every variant tops out near +0.03 R of lift (~$1/oz at 2026 volatility); the EA's own picks
are +0.14..+0.18 R above the same universes. The in-sample winner (raw shape) does not carry to
silver. The EA's choices are imitable from price data (AUC up to 0.84 on silver) but the imitation
does not earn — so whatever separates its winners is not in the features that predict its choices.

## O. Silver over the full 2018-2026.08 backtest (`research/aero/aero_silver_full.py`) — corrects section M

EA silver run: $10k -> $12,968 (+29.7%; gold same years +172%). Same ATR-geometry scoring:

| 2018-02..2026-08 | Breaks | EA picks matched | Picks win | Picks R | All-breaks R | P(random >= picks) |
|---|---|---|---|---|---|---|
| Gold | 31,712 | 495 | 89.1% | +0.231 | +0.006 | 0.0000 |
| Silver | 6,352 | 97 | 79.4% | +0.076 | +0.022 | 0.20 |

Gold lift is positive in EVERY year 2018-2026 (+0.14 to +0.34 R, picks 87-95% win). Silver lift is
negative 2018-2021 (-0.12 to -0.24, 27 picks, R sd 0.95 -> noisy) and positive 2024-2026
(+0.12 to +0.18, 70 picks). Silver matching is weak early: EA silver orders by year 69/0/37/38/1/0/
35/33/95, matched 2/0/11/14/0/0/13/7/50 — the EA's early silver levels are mostly not $0.05
day-high/low breaks in Pepperstone data. Gold co-movement check (full period): picks with no gold
pick within 60 min +0.106 R (78) vs with one -0.044 (19).
Reading: the 2026-only silver result (section M) was the favourable end of a mixed record. Over the
full period silver does NOT show a reliable transferable selection (p=0.20); the memorised-outcomes
explanation is not ruled out. Gold remains uniformly strong in every year.
