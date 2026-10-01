# Hermes User Manual

How to take a trading idea from a hunch to a portfolio using Hermes — **twice over**: once
with an AI agent driving, and once entirely by hand. Both paths use the same library, the
same commands and the same evidence. The agent is a driver, not a privileged user: there
is nothing it can reach that you cannot.

- **Vocabulary**: [`CONTEXT.md`](../CONTEXT.md) — use its words; the code does.
- **Decisions**: [`docs/adr/`](./adr) — why things work the way they do.
- **Conventions**: [`CLAUDE.md`](../CLAUDE.md) — gates, contracts, what never to commit.

---

## First, two different things called "AI"

These get confused constantly, and confusing them costs money.

| | **The agent** | **The AI Advisor** |
|---|---|---|
| What it is | Claude Code driving the research process — reading, writing strategies, running commands | An LLM *inside a strategy*, asked to confirm or veto each candidate trade |
| When it runs | While you work | On every signal, during the backtest |
| Costs | Your Claude Code subscription | A **billed API call per signal** — thousands on a universe run |
| Default | Opt-in (you open the repo in Claude Code) | **Off.** Nothing attaches an advisor unless you ask |
| Affects results? | No — it just drives the tools | **Yes** — it changes which trades happen |
| Reproducible? | n/a | Yes, via a content-addressed decision cache (ADR-0005) |

This manual's "with AI / without AI" columns are about **the agent**. The AI Advisor is an
orthogonal choice: you can run the whole five-stage process with an agent and no Advisor
(the common case), or by hand with an Advisor attached. See
[Appendix: the AI Advisor](#appendix-the-ai-advisor).

---

## Setup

```bash
pip install -e ".[dev,yfinance,binance,ai,report,ui]"
.venv/bin/hermes --version
```

Everything below assumes the project venv: `.venv/bin/hermes` and `.venv/bin/python`.

**Where things live**

| Path | What | Committed? |
|---|---|---|
| `strategies/*.py` | Your strategies | ✅ yes |
| `research/ideas.json` | The idea backlog | ✅ **yes** — work product |
| `research/output/` | Bulk CSVs from ad-hoc scripts | ❌ ignored |
| `.hermes_cache/bars/` | Parquet price cache | ❌ ignored |
| `.hermes_cache/runs/` | The run ledger | ❌ ignored (rebuildable) |
| `.hermes_cache/ai/`, `pit/` | Advisor decisions, point-in-time data | ❌ ignored |
| `.env` | Credentials | ❌ ignored |

Delete `.hermes_cache/` to start cold. Do **not** delete `research/ideas.json` — it holds
the thinking, not the data.

**Two global rules**

1. Every command takes `--json`: one JSON document on stdout, progress on stderr. Safe to
   pipe. Exit `0` = fine, `2` = ran but produced nothing usable (e.g. zero trades),
   `1` = bad request.
2. Runs are **input-keyed and cached**. Repeating a configuration is free; editing a
   strategy file changes the key, so you never get a stale result for changed code.

---

## The process

```
   ┌─────────────────────────────────────────────────────────────┐
   │                                                             │
   ▼                                                             │
1 IDEA ──▶ 2 QUANTIFY ──▶ 3 TEST ──▶ 4 PORTFOLIO ──▶ 5 REPEAT ───┘
                                          │
  backlog     if/and rules    the ladder  │  correlation    name the gap
  provenance  + units         + evidence  │  + div. ratio   not the winner
```

Stage 3 is a **ladder**, climbed in order — each rung is cheap relative to the next and
kills most candidates before you pay for it:

```
  historical ──▶ robustness ──▶ out-of-sample ──▶ forward test
  (does it do    (plateau or    (does it hold     (⚠️ NOT IN HERMES —
   anything?)     a spike?)      on unseen data?)   see stage 3.4)
```

---

# Stage 1 — Idea generation

The stage with the least infrastructure anywhere, and the one that decides your ceiling.
Ideas arrive from a book or a trading session and then live in someone's head until they
are forgotten. The backlog exists so they survive, and so the **source** of your wins
becomes measurable.

### With the agent

Just talk. The `hermes-ideas` skill fires on its own when you mention reading something,
noticing something while trading, or ask what to work on next.

> "I've been running MNQ on sim for a few weeks and keep noticing the first pullback after
> the 09:30 push tends to hold. Worth testing?"

The agent captures it with provenance and mechanism, and tells you what it defaulted. Ask
it for ideas and it generates from **mechanism** — who is forced to trade, when, and why —
rather than handing you twenty moving-average variants, which are one idea wearing hats.

### By hand

```bash
hermes ideas add "First pullback after the open holds" \
    --source discretionary --detail "3 weeks MNQ sim" \
    --hypothesis "the first pullback after the 09:30 push finds buyers because \
                  overnight shorts cover into it" \
    --tag futures --tag intraday

hermes ideas                     # the pipeline
hermes ideas --open              # what still needs work
hermes ideas --hit-rate          # which sources actually pay off
hermes ideas set <id> --status quantified
```

Or in Python:

```python
from hermes import IdeaBook

book = IdeaBook()                       # research/ideas.json
idea = book.add("First pullback holds",
                hypothesis="overnight shorts cover into it",
                source="discretionary", source_detail="3 weeks MNQ sim")
book.update(idea.id, status="quantified")
print(book.pipeline())                  # {'raw': 3, 'quantified': 1, ...}
```

### The pipeline

`raw` → `quantified` → `testing` → `validated` | `rejected`, plus `parked`.

**`parked` is for ideas blocked on something external** — data Hermes can't reach, an
instrument with no source — *not* for ideas you've gone cold on. Parked is a to-do;
rejected is a result. A rejected idea **stays in the book**, because knowing you already
tested something is what stops you testing it again in six months.

### Provenance, and why it is a closed list

`book` · `discretionary` · `trader` · `course` · `data` · `other`

Not free text, so `--hit-rate` can answer *which of your sources are worth your time* —
unanswerable from memory, obvious from the backlog:

```
provenance      decided  validated  rate
book                 4          0     0%
discretionary        3          2    67%
trader               1          1   100%
```

Only **decided** ideas score a source; an untested idea is not evidence about where it
came from. Treat each source accordingly:

- **book** — published performance is usually optimistic. The edge may have decayed after
  publication, or the backtest was generous. Test it yourself; the win is rarely the whole
  strategy, usually one mechanism you can lift elsewhere.
- **discretionary** — highest quality, slowest. Weeks of screen time produce a hunch that
  still needs quantifying. Record it the day you notice it.
- **trader / course** — expect to lift snippets, not systems. A cookie-cutter copy rarely
  survives your costs, instruments and sizing.
- **data** — the most dangerous. You found it by searching, and the same search finds
  noise, so it needs the most out-of-sample evidence before you believe it.

### What good looks like

A title is a sticky note. Capture the **claim**, the **mechanism**, and the **provenance**.
The mechanism is not decoration: *"momentum persists because institutions fill large orders
over days"* survives a bad backtest with its reasoning intact and tells you which variants
are faithful to the idea. *"The 13-day EMA works"* does not.

Don't quantify at capture time. Capture should stay cheap, or you filter ideas out before
you have enough of them.

---

# Stage 2 — Quantification

Turning English into rules a machine can execute. **This is the step that decides whether
the rest is possible**, and the step most often skipped.

### The problem, concretely

> "Short when large orders hit the bid after an extension move."

Every reader nods. Nothing in it is testable:

| Word | Hidden decisions |
|---|---|
| **extension** | From the open? VWAP? Yesterday's close? In %, ATR multiples, or bars? Over what window — 09:40–10:00, or any time? |
| **large order** | Absolute size? A multiple of median size? A percentile of today's prints? |
| **after** | Within how many bars? |
| *(implicit)* | What timeframe candle are you measuring on? |

Rewrite it as **if/and statements with numbers and units**, and list every value you had
to invent. An idea that can't be written that way isn't ready to test.

### Two traps worth a whole day each

**1. Hermes is bars only.** No order flow, no tape, no level 2. "Large orders hitting the
bid" is *not expressible*. A volume spike on a bar is the nearest proxy — and it is a
**different idea**. Say which one you're testing, or `parked` the original. Silently
substituting tests something nobody proposed.

**2. Every invented number is a parameter you'll have to defend.** Target **≤ 3** tunables
for a first version. `metrics.parameter_adjusted_sharpe` = Sharpe × √((n_trades − k)/n_trades),
so each extra knob costs statistical power. Prefer a value the idea itself implies — if a
threshold only makes sense at 0.5%, hard-code `0.5` rather than exposing it to be tuned.

### With the agent

`/hermes-strategy` runs a bounded interview — instrument and source, timeframes, the edge
in one sentence, entry, exit, stop and target, sizing, filters — one question at a time,
leading with a recommended default so you can accept in a word. It hunts the ambiguity
first and tells you what it invented.

Inside `hermes-research` it skips the interview entirely (the hypothesis is already
pinned) and records what it defaulted in the file's docstring.

### By hand

Write `strategies/<name>.py` yourself. It must follow the **discovery contract** or the
CLI, the UI and the skills won't see it:

```python
"""First pullback after the open.

QUANTIFICATION
  push      = close(09:45) / open(09:30) - 1 >= 0.30%     [5m bars, exchange-local]
  pullback  = low touches EMA(20) on 5m within 6 bars of the push
  entry     = stop-buy at the high of the touching bar
  stop      = 1.0 x ATR(14) below entry
  target    = 2.0 x the stop distance
  invented  = 0.30% push, 6-bar window, ATR multiple of 1.0, 2:1 target
"""
from hermes import ATR, EMA, Backtest, Parameter, RiskPercent, Strategy, Symbol, Timeframe
from hermes.data import YFinanceSource

GENERATED_BY = "hand-written"          # omit, or name the skill that wrote it
M5 = Timeframe.parse("5m")


class FirstPullback(Strategy):
    def setup(self) -> None:
        self.push_pct = self.param(Parameter("push_pct", 0.30, bounds=(0.1, 1.0)))
        self.ema = self.use(EMA(M5, 20))
        self.atr = self.use(ATR(M5, 14))

    def on_bar(self, bar) -> None:
        ...  # your logic


def build_backtest(**overrides) -> Backtest:
    defaults = dict(
        strategy=FirstPullback(),               # a FRESH instance every call
        source=YFinanceSource(),
        symbol=Symbol("QQQ", "yfinance"),
        timeframes=[M5],
        starting_cash=10_000,
    )
    defaults.update(overrides)                  # symbol/start/end/cash/params
    return Backtest(**defaults)


if __name__ == "__main__":
    print(build_backtest().run().metrics)
```

Three rules, all load-bearing:

- `build_backtest(**overrides) -> Backtest`, building a **fresh** `Strategy` each call so
  re-runs are clean.
- **Omit `start`/`end`** unless the strategy needs a specific window — the default Trading
  Window is year-to-date.
- A universe-scale run returns a `UniverseBacktest`, not a `Backtest`, so it exposes
  `build_universe_backtest(**overrides)` instead (see `strategies/gap_breakout_sp500.py`).

Check Hermes saw it:

```bash
hermes strategies            # a file that fails to import is listed with its error
```

### Sizers, sources, indicators

| Sizer | Size expressed as |
|---|---|
| `Units` | native units / shares / lots / coins |
| `NotionalCash` | position value in cash |
| `EquityFraction` | fraction of equity |
| `LeveragedFraction` | fraction of equity × leverage |
| `RiskCash` | cash at risk between entry and stop |
| `RiskPercent` | % of equity at risk between entry and stop |

Sources in the registry (`--source`): `binance`, `binance-futures`, `yfinance`,
`pepperstone`. `TiingoSource` exists but is not in the registry — construct it directly.
Built-in indicators: SMA, EMA, RSI, ATR, MACD, BollingerBands, ADX, Fractals,
FairValueGap, plus `LibraryIndicator` to wrap pandas-ta / TA-Lib. Need something else:
`hermes-extend`, or subclass `Indicator` yourself.

---

# Stage 3 — Testing

Four rungs. Climb in order.

## 3.1 Historical — does it do anything at all?

### With the agent

`hermes-backtest` fires on its own. "Run it on AAPL over 2021–2023."

### By hand

```bash
hermes run ema_crossover --symbol AAPL --start 2021-01-01 --end 2023-12-31 \
    --note "baseline: does the 8/32 cross work on a mega-cap at all?"
```

```
ema_crossover · AAPL · yfinance
run  35fc688923c61ea9
why  baseline: does the 8/32 cross work on a mega-cap at all?
  trades      13   params 2
  return      24.69%   CAGR 7.68%
  sharpe      0.54   (param-adjusted 0.50)
  max DD      -21.31%   win rate 38.46%   profit factor 1.67
```

**Always pass `--note`.** It records *why* this configuration ran. Without it the ledger is
a list of hashes; with it, it's a research log you — or a future session — can read:

```bash
hermes runs                            # newest first, with the notes
hermes runs --note-contains "mega-cap"  # find your own past reasoning
hermes show 35fc --trades              # key prefixes work
```

**Zero trades?** The run diagnoses itself and exits `2`:

```
⚠️  zero trades — 754 bars stepped over 2021-01-04 → 2023-12-29, so the data is fine
and the engine ran — the entry condition simply never became true. Look at the
thresholds, not the window.
```

Three causes, three different fixes: no bars stepped (symbol/window/Lead-in), bars but
everything vetoed (the Advisor), bars and nothing fired (your thresholds).

In Python:

```python
from hermes.research.discovery import configured_backtest, default_config, discover_all
from datetime import UTC, datetime

entries, broken = discover_all("strategies")
entry = next(e for e in entries if e.name == "ema_crossover")
bt = configured_backtest(entry, ticker="AAPL", source_name="yfinance",
                         start=datetime(2021, 1, 1, tzinfo=UTC),
                         end=datetime(2023, 12, 31, tzinfo=UTC),
                         starting_cash=default_config(entry).starting_cash)
result = bt.run()
print(result.metrics.sharpe, result.metrics.num_trades)
```

## 3.2 Robustness — is this a plateau or a spike?

Three independent questions. All computed.

### With the agent

`hermes-analyze-results` fires on its own, or ask: "is this robust?"

### By hand

```bash
hermes analyze 35fc                    # the cheap seven axes
hermes analyze 35fc --json             # machine-readable; also writes analysis.json
```

```
  ✓ trades       the top 5 trades are 100% of all winning P&L; every trade is buy
  ✓ costs        edge survives 1x and 2x costs
  ✓ params       stable: sharpe holds across the neighbourhood of every swept parameter
  ✓ oos          in-sample Sharpe 0.39 does not carry out of sample (-0.40)
  ✓ validation   13 trades over 3.0y is exploratory — too few trades to conclude anything
  ✓ regime       all profit comes from Bull regimes (bull 28,573 vs bear -3,880)
  · gate         no AI advisor on this run
```

That run reported **+24.69% and Sharpe 0.54**. The rubric took it apart in one command.

**The three robustness questions:**

| Axis | Asks | Fails when |
|---|---|---|
| `costs` | Does the edge survive friction? | Profitable only at 0× costs → a cost artifact, not an edge |
| `params` | Is the chosen parameter value a plateau or a spike? | Metric collapses one notch away → the number was *found*, not earned |
| `validation` → `monte_carlo` | Was the realised trade *order* lucky? | Read `max_drawdown_worst`, not the single historical drawdown |

On `params`: a Sharpe of 1.4 at `fast=12` that drops to 0.2 at 11 and 13 is **not an edge
at 12**. Live, you get the neighbourhood average, not the peak. The number reported is
**neighbour degradation** — base metric vs the mean of its immediate neighbours; below
**0.6** is flagged `fragile`.

This is deliberately *not* a grid search. A grid asks "what's the best combination?", which
is optimisation (`WalkForward`). This holds the combination fixed and asks whether it's
stable. Conflating them is how a search gets mistaken for an edge.

By hand in Python:

```python
from hermes import cost_sensitivity, param_sensitivity, validate

for mult, r in cost_sensitivity(bt, multipliers=(0.0, 1.0, 2.0)).items():
    print(mult, r.metrics.total_return)

ps = param_sensitivity(bt, metric="sharpe", steps=4)
print(ps.fragile)                                  # [] means every parameter is a plateau
print(ps["short_ema"].degradation)                 # ~1.0 = plateau, ~0.1 = spike

sv = validate(result, seed=42)
print(sv.monte_carlo.max_drawdown_worst)           # worst across 1000 reorderings
print(sv.sample_quality.tier, sv.sample_quality.tier_reason)
```

### Evidence tiers — how much is enough?

30 trades is enough to form a *view* and nowhere near enough to risk capital. The
`validation` axis reports a tier requiring **both** a trade count and a span of history,
because 800 trades inside one year hasn't seen a second regime and five years with 40
trades hasn't seen enough events:

| Tier | Needs | Means |
|---|---|---|
| `unknown` | no measurable span | Can't be placed — *not* the same as inadequate |
| `exploratory` | below credible | A view, not a conclusion |
| `credible` | ≥ 100 trades **and** ≥ 2 years | Worth real work and out-of-sample testing |
| `deployable` | ≥ 500 trades **and** ≥ 5 years | Enough evidence to risk capital on |

`tier_reason` names whichever of the two is short. These counts are frequency-dependent by
nature — 1000 trades is routine intraday and unreachable for a daily swing strategy on one
symbol — so read `deployable` as *"this is what deployment evidence looks like"*, not as a
hard gate. **Quote the tier.** A positive `exploratory` result is not a finding.

## 3.3 Out of sample — does it hold on data it hasn't seen?

```bash
hermes analyze 35fc --axes oos              # cheap: one fixed split, same parameters
hermes analyze 35fc --axes walkforward      # expensive: fit in-sample, score out
```

The rule that matters: **never tune on the whole sample.** Parameters get chosen on IS data
and judged on OOS data. A number obtained by picking the best parameters across all the
data is not a result — it's the search reporting on itself.

```python
from hermes import WalkForward, split_isoos

is_r, oos_r = split_isoos(bt, is_frac=0.7)          # no fitting; same params both halves
print(is_r.metrics.sharpe, "->", oos_r.metrics.sharpe)

wf = WalkForward(template=bt, total_start=bt.start, total_end=bt.end,
                 is_frac=0.6, param_grid={"short_ema": [6, 8, 10],
                                          "long_ema": [24, 32, 40]})
res = wf.run()
print(res.oos_result.metrics.sharpe)                # the ONLY number worth quoting
print([w.best_params for w in res.windows])         # differ between windows? unstable optimum
```

If the best parameters change window to window, the optimum isn't stable — that's a finding,
not a nuisance.

## 3.4 Forward test — ⚠️ not in Hermes

The last rung, and the only one that sees data no backtest could have seen: run the
strategy on **paper or minimum size** and compare realised fills to the backtest over the
same window.

**Hermes cannot do this.** There is no live `ExecutionVenue` — ADR-0001 built the seam, not
the adapter. Deployment is "write one adapter," not a rewrite, but the adapter doesn't
exist.

So: a strategy that survives the entire rubric is **ready to forward test**, never
"deployable". `TIER_DEPLOYABLE` describes the quality of your *evidence*, not a decision to
trade. Do this part in your broker's sim account, and compare against the backtest's last
month or a comparable historical regime — equity curve shape and the headline metrics, not
just P&L.

## 3.5 The written verdict (optional)

```bash
hermes analyze 35fc          # compute the evidence first
hermes review 35fc           # then ask for a verdict over it
```

`review` drives Claude Code headlessly (`claude -p` — your subscription, no API key, no
billed request) over `result.json` **and** `analysis.json`, and writes `review.md`.

It runs **without Bash on purpose**: **Hermes computes the numbers, the reviewer interprets
them.** An LLM shelling out to work out whether an edge survives costs would be slower,
unauditable and non-reproducible. If no `analysis.json` is attached, the prompt tells the
reviewer the axes are *unmeasured* and names the command that would measure them — so a
review can never quietly imply a check it didn't make.

Without the `claude` CLI on PATH, it prints the prompt to run by hand.

---

# Stage 4 — Portfolio

A single strategy is a single point of failure, however good its backtest. The goal is
several **uncorrelated** return streams: risk-adjusted return scales with the number of
genuinely independent bets, so *N* uncorrelated streams cut portfolio volatility by ~√N
while *N* variations on one edge cut it by nothing.

Which makes *"is this strategy good?"* the wrong second question. The right one is
**"is it good *and* does it do something my existing strategies don't?"**

### With the agent

`hermes-portfolio` fires when you have more than one working strategy or ask about
diversification.

### By hand

```bash
hermes correlate                         # newest run per strategy
hermes correlate 35fc e815 bd80 f43d     # specific runs
hermes correlate --frequency W           # weekly returns: steadier, needs longer history
```

```
                        0      1      2      3
0 ema_crossover:AAPL 1.00   0.48   0.30  -0.03
1 ema_crossover:MSFT 0.48   1.00   0.51   0.06
2 ema_crossover:NVDA 0.30   0.51   1.00   0.06
3 ema_crossover:GLD -0.03   0.06   0.06   1.00

diversification ratio  0.72  (1.00 = one bet; lower is better)
  ✓  ema_crossover:AAPL and ema_crossover:GLD at -0.03 — genuinely diversifying
```

Read three things:

- **the matrix** — ≥ **0.70** is effectively one bet; ≤ **0.30** genuinely diversifies.
- **diversification ratio** — an equal-weight blend's volatility ÷ the mean individual
  volatility. `1.00` means you own one bet in several costumes. Uncorrelated streams give
  ~1/√N. **Below** 1/√N means a pair is negatively correlated and actively cancelling —
  real, and good.
- **unmeasurable pairs** — fewer than **20** shared periods. That is *unknown*, **not**
  uncorrelated, and must not be counted as diversification.

Note what the example shows: it's the *same strategy code* on gold that decorrelated from
all three equity runs, dropping the ratio from 0.80 to 0.72. Changing the **instrument** is
usually the cheapest real diversifier.

### What moves correlation, in order of effect

1. **Asset class / instrument** — the biggest lever, and free to test.
2. **Direction** — a book with no short exposure has a regime it cannot survive. If the
   `regime` axis shows profit only in Bull regimes, that's a correlation problem.
3. **Holding period** — intraday and multi-week edges answer to different drivers.
4. **Mechanism** — mean reversion against trend following.

Changing parameters, swapping indicators, or adding a filter moves correlation **least**.
A variant correlating ≥ 0.9 with its parent is a *replacement* candidate, not an addition.

### Shared capital is a separate question

Correlation says whether two strategies are the same bet. It says nothing about whether you
can afford both. For legs competing for one account:

```python
from hermes import PortfolioBacktest, correlate_results

m = correlate_results({"AAPL": result_a, "GLD": result_b}, frequency="W")
print(m.pair("AAPL", "GLD"), m.diversification_ratio)

pr = PortfolioBacktest(legs=[bt_a, bt_b], starting_cash=100_000).run()
print(pr.result.metrics)                  # one shared Account — real capacity
```

Run that before believing a blend is tradeable at size. For a bias-free index universe,
`UniverseBacktest` + `ConstituentCalendar` clips each leg to its point-in-time membership.

### Honest limits

- Measured on **backtest** curves over shared history. Check the overlap count.
- Correlations **move**, and they converge in crises — exactly when diversification is
  supposed to help. Treat these as the optimistic case.

---

# Stage 5 — Repeat

The cycle restarts here, and the point is that you come back to Stage 1 with a **named gap**
rather than a blank page.

### With the agent

Ask what to research next. `hermes-portfolio` combines the correlation matrix with the
backlog and proposes the idea that fills the gap.

### By hand

```bash
hermes correlate --json       # where is the redundancy?
hermes ideas --open           # what's available to work on?
hermes ideas --hit-rate       # which sources have been paying off?
```

Name the gap **before** picking the idea — an asset class you have no exposure to, the short
side, a holding period you don't cover. Then choose the open idea that fills it, *not* the
one with the best-looking backtest. A fifth correlated strategy adds risk and no return; a
mediocre uncorrelated one may add more than an excellent redundant one.

Then close the loop:

```bash
hermes ideas set <id> --status validated \
    --strategy ema_crossover --run-key 35fc688923c61ea9 \
    --verdict "survives costs and OOS on MSFT; sample still exploratory"
```

A rejected idea is a **real result**. Record the verdict so you don't retest it.

---

# Hand the whole thing over

With the agent you can skip the hop-by-hop driving entirely. Describe an idea and ask for it
to be **investigated** — `hermes-research` owns stages 2 and 3:

> "I think the first pullback after the open holds on index futures. Go find out."

It declares a budget (**6 iterations, ≤ 3 parameters**), checks the ledger for work already
done, writes variants, runs and judges each against the rubric, iterates only on what the
analysis *pointed at*, and stops on an explicit rule:

1. the rubric holds, or clearly rules the idea out;
2. **no out-of-sample improvement in 2 consecutive iterations** — past that you've become
   the search, and the deflated Sharpe stops accounting for it;
3. the parameter budget is spent;
4. the `params` axis says the result is a spike.

It **stops and asks first** before: attaching an AI Advisor (billed per signal), using a
paid data source, or changing the instrument or the idea — a different idea is a new
research task, not iteration 7.

It reports a verdict, the axis numbers behind it, the full iteration trail with run keys,
and **what went unmeasured**.

---

# Reference

## Commands

| Command | Stage | Does |
|---|---|---|
| `hermes ideas [add\|set\|rm]` | 1 | The idea backlog |
| `hermes strategies` | 2 | What's runnable; broken files with their error |
| `hermes run <name>` | 3 | Run + record. `--note` is effectively mandatory |
| `hermes runs` | 3 | The research log. **Check before re-running** |
| `hermes show <key>` | 3 | A stored run; `--trades` for the blotter |
| `hermes analyze <key\|name>` | 3 | The rubric, computed → `analysis.json` |
| `hermes review <key>` | 3 | A written verdict over that evidence |
| `hermes correlate [keys…]` | 4 | Correlation matrix + diversification ratio |
| `hermes install-skills` | — | Copy the skills into another project |

## Analysis axes

Default seven, cheapest first; two opt-in.

| Axis | Question | Cost |
|---|---|---|
| `trades` | Is the P&L two lucky trades? | free |
| `costs` | Survives 1× and 2× friction? | 3 backtests |
| `params` | Plateau or spike? | ~1 per parameter per step |
| `oos` | Holds on unseen data? | 2 backtests |
| `validation` | Distinguishable from luck? Evidence tier? | free |
| `regime` | Strategy, or bull market? | free |
| `gate` | Was the AI Advisor actually in effect? | free |
| `walkforward` | Do *fitted* parameters hold out of sample? | **grid × windows** |
| `baseline` | Does the Advisor beat a weighted coin? | **N backtests** |

Only `trades` and `gate` can be computed from a *stored* run — the others need real `Trade`
objects or a benchmark series, so `analyze` re-runs the backtest for them. An axis that
can't run reports `ran: false` with the reason; **never read that as "clean"**.

## Skills

| Skill | Invoke | Stage |
|---|---|---|
| `ask-hermes` | `/ask-hermes` | router (you only) |
| `hermes-ideas` | automatic | 1 |
| `hermes-strategy` | automatic or `/hermes-strategy` | 2 |
| `hermes-research` | automatic | 2–3, owns the loop |
| `hermes-backtest` | automatic | 3 |
| `hermes-analyze-results` | automatic | 3 |
| `hermes-portfolio` | automatic | 4–5 |
| `hermes-explore-data` | automatic | supports 1–2 |
| `hermes-extend` | automatic | new Indicator / DataSource / Venue |

## The web UI

```bash
hermes-ui
```

Pick a strategy, tweak the config, Run. Interactive equity curve + drawdown, metric cards,
a trades table, and a Claude review. Reads and writes the **same ledger** as the CLI, so a
run from either shows up in both. A strategy that fails to import is reported inline rather
than taking the picker down.

## Thresholds, and where to change them

| Constant | Value | Where |
|---|---|---|
| `MIN_TRADES` | 30 | `backtest/validation.py` |
| `MIN_TRADES_PER_PARAM` | 10 | `backtest/validation.py` |
| `CREDIBLE_TRADES` / `_YEARS` | 100 / 2.0 | `backtest/validation.py` |
| `DEPLOYABLE_TRADES` / `_YEARS` | 500 / 5.0 | `backtest/validation.py` |
| `PLATEAU_RATIO` | 0.6 | `backtest/param_sensitivity.py` |
| `REDUNDANT_ABOVE` | 0.7 | `backtest/correlation.py` |
| `DIVERSIFYING_BELOW` | 0.3 | `backtest/correlation.py` |
| `MIN_OVERLAP` | 20 | `backtest/correlation.py` |

They're defaults, not laws — but change them deliberately and say so in the run's `--note`.

---

# What Hermes will not do

Knowing the edges saves you from designing against them:

- **Forward test or trade live.** No live venue (ADR-0001). Backtest and paper/live are the
  same Strategy code, but the broker adapter doesn't exist.
- **See below the bar.** No order flow, no tape, no level 2. Many good discretionary ideas
  are about order flow and simply can't be expressed.
- **Trade more than one instrument per Strategy.** One Strategy, one Instrument (ADR-0003).
  Many instruments = `PortfolioBacktest` / `UniverseBacktest`; an instrument you only want
  to *watch* = a `Reference` feed.
- **Credit dividends as cash.** Bars are split-adjusted; dividend-on-ex-date into the
  Account is a known TODO.
- **Run universe backtests from the CLI.** `hermes run` is single-symbol;
  `UniverseBacktest` is library- or UI-only for now.
- **Tell you a strategy is good.** It measures; you decide. Every threshold above is a
  default someone chose.

---

# Appendix: the AI Advisor

An optional gate that can **only confirm or veto an already-formed trade** — it never
invents, sizes or adjusts one (ADR-0005). Off by default.

```python
from hermes import AIAdvisor, Backtest, ClaudeProvider

bt = Backtest(..., advisor=AIAdvisor(ClaudeProvider()))   # needs ANTHROPIC_API_KEY
```

Then in the Strategy:

```python
order = self.buy(RiskPercent(0.01), stop_loss=..., take_profit=...)
if not self.confirm_with_ai(order, "Confirm this long swing setup."):
    self.venue.cancel(order)
```

**Before you attach one:**

- It is **one billed API call per signal**. A universe run means thousands. Price it first.
- Decisions are served from a **content-addressed cache** keyed on model + prompt + inputs,
  so repeat runs are reproducible, cheap and offline. The cache doubles as a record/replay
  fixture.
- It **fails open**: if the provider errors, the trade is approved without a real decision,
  because an outage must not silently rewrite your strategy into one that never trades.
  Those decisions are never cached and are counted in `llm_summary.fail_open_calls`. The
  `gate` axis shouts about it. **A run with fail-opens is less gated than configured and is
  not comparable to a clean one.**
- Configure with `HERMES_AI_MODEL` / `HERMES_AI_MAX_TOKENS`. Providers: `ClaudeProvider`
  (default), `DeepSeekProvider`.
- **Point-in-time context only.** `ContextEnricher`s (fundamentals as reported, 10-K
  sections, news windows) are keyed on `(ticker, as_of)`. An enricher returning *today's*
  fundamentals for a 2019 decision has silently inserted look-ahead.

**And validate it like anything else.** An AI gate that lands inside its own random
distribution has no demonstrated skill — it's just trading less:

```bash
hermes analyze <key> --axes baseline        # N runs approving at the gate's own rate
```
