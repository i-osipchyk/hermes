---
name: hermes-research
description: Research a trading idea end to end — pin the hypothesis, write variants, run them, judge them against the rubric, iterate, and report a verdict. Use when the user describes a trading idea and wants it investigated rather than just implemented, says to "research"/"investigate"/"see if this works", or hands over a hypothesis and expects you to work it out.
---

# Hermes: research an idea

You are the quant researcher. The user gives you an idea; you come back with a verdict
and the evidence for it. The other Hermes skills are your specialists — this one owns the
**loop** and, crucially, decides when to stop.

This is steps 2–3 of the repo's five-step process (ADR-0011). Step 1 (idea generation)
is **`hermes-ideas`**; step 4 (portfolio by correlation) and step 5 (what to research
next) are **`hermes-portfolio`**. Record the idea in the backlog before you start and
link your runs back to it — a verdict with no traceable evidence is an opinion.

There are two failures this skill exists to prevent, and they are equally real:

- **Finding a number.** "Here's a strategy, it returned 40%" — produced by trying things
  until one worked.
- **Rejecting an idea you never understood.** Running the textbook form of an idea,
  watching it fail, and filing it as dead. A generic expression failing says the
  *generic expression* fails — not that the idea has no edge. Real edges are usually
  found by dissecting a flawed baseline: seeing which trades make money, why, and
  building the version that isolates that. Three backtests and a rejection is not
  rigor, it is a shrug.

Your job is to *understand* the idea well enough to build its best faithful form, then
attack that result until it survives or dies honestly. Rigor lives in the **holdout and
the trial count**, not in a small iteration budget.

## 0. Budget — set it before you start

You run **unattended**, so you get a leash and you say what it is. It has three parts:

- **Holdout, enforced.** Hermes reserves the last 20% of each instrument's history (at
  least 6 months), frozen in `research/holdouts.json`, and **research runs cannot read it**:
  a request past it raises `HoldoutViolation`, and a run with no `--end` stops where the
  holdout starts. You do not hold yourself to it — the data layer does. If the history is
  too short to carve one, the idea is capped at `exploratory` and cannot be validated; say
  so rather than working around it. The holdout is read **once per idea**, by
  `hermes holdout unlock` (§5), and each look makes the next idea's bar higher.
- **Trial counter.** Pass `--idea <id>` on every variant you *score*; that is a trial. A
  run that explains without selecting (a sweep, a dissection re-run) takes `--diagnostic`
  and is not counted. `hermes trials <id>` groups trials into **clusters** — a variant that
  barely moves the return stream or the entry days is the same bet, not a new one — and
  reports **effective trials**, including the clusters of any idea this one drew on
  (`hermes ideas set <id> --informed-by <other>`: declare it honestly, undeclared reading is
  how a search launders trials). The deflated Sharpe uses that number. Report it.
- **Test plan and cluster budget.** Before the first run you write `plan.json` (pipeline: the
  Quant), the Validator approves and locks it, and every scored run carries
  `--idea <id> --variant <variant id>`. A variant changes one *structural* axis (entry, exit,
  regime filter, instrument, timeframe, horizon) — a parameter nudge is not a variant, and a
  plan whose variants mostly share an axis is refused. The plan's `max_clusters` replaces the
  old ~15-variant cap: `hermes run --variant` refuses to start a cluster past it (a
  `--diagnostic` stays free), and another variant is a reviewed amendment
  (`hermes pipeline amend-plan`). **≤ 3 tunable Parameters** in any variant. Hard-coded economic constants (a session open, a
  round-number level, a 14-day lookback) are not knobs. Scale the budget to the idea: a
  rich one earns more, but say so in your first message.

State all three, then work. (Run by the pipeline, the Quant role follows this skill and the
Validator — not you — decides continue or reject; see `hermes-pipeline`.) Stop per §6. If you hit the budget without a verdict, say
so and report what you learned — "inconclusive after N trials, here is where the edge
seems to live" is a real result.

**Stop and ask before spending money or widening scope:**

- attaching an **AI Advisor** (`ClaudeProvider`/`DeepSeekProvider`) — every signal is a
  billed API call, and a universe run means thousands;
- a **paid data source** (`TiingoSource`, Polygon/Massive enrichers) or a first fetch
  large enough to take many minutes;
- changing the **instrument, asset class, or idea** from what the user described. A
  different idea is a new research task, not iteration 7. Report and ask.

## 1. Pin the hypothesis

Write down, in the framework's words (`CONTEXT.md`), what is being claimed:

> **Hypothesis** — on <instrument>, <this condition> precedes <this move>, exploitable
> via <entry/exit> on <timeframes>, because <the inefficiency>.

The "because" is not decoration. An idea with no mechanism cannot be distinguished from
a pattern you found by looking, and it tells you which variants are *faithful* to the
idea versus which are fishing.

Ask the user only what genuinely blocks you, **batched into one message, at most once**.
Anything you can pick a defensible default for, pick it and say so. If the idea is clear
enough to run, don't ask at all.

## 2. Check the ledger before you run anything

```bash
.venv/bin/hermes runs --json
.venv/bin/hermes runs --note-contains "<keyword from the idea>"
```

The ledger is your memory across sessions, and it is input-keyed — an identical
configuration returns instantly with `"from_cache": true`. Look before you work: you may
have already tested this, including in a previous session. Reading a stored result costs
nothing; re-deriving it costs minutes and tells you nothing new.

## 3. Write the variant

Use **`hermes-strategy`**'s file contract (`GENERATED_BY`, `build_backtest(**overrides)`,
a fresh Strategy per call). Inside this loop, skip its interview — the hypothesis is
already pinned; just record the choices you make and why.

**Start with the most faithful, simplest expression of the idea** — the fewest parameters
that can express it. That first run is your baseline, not your verdict: it is the
specimen you dissect in §4b. Every later variant is judged against it, not against zero.
A variant is only worth running if you can say in advance what it would prove.

## 4. Run and judge — the testing ladder

Climb it in order; each rung is cheap relative to the one above and kills most candidates
before you pay for the next.

```bash
.venv/bin/hermes run <name> --idea <id> --note "<what this variant tests>"   # --note is not optional
.venv/bin/hermes analyze <run-key>                                 # the cheap seven
.venv/bin/hermes analyze <run-key> --axes walkforward              # fitted params, out of sample
```

1. **Historical backtest** — does it do anything at all? A result on 30 trades is a
   *view*, not a conclusion. `analyze` reports an **evidence tier** on the
   `validation` axis: `exploratory` (too little to conclude), `credible` (worth real
   work), `deployable` (years of history *and* hundreds of trades). Quote the tier, and
   never describe an exploratory result as working.
2. **Robustness** — two independent questions, both already computed:
   - the `params` axis sweeps each parameter around its chosen value. A **plateau** means
     the neighbourhood performs like the reported point; a **spike** means the number was
     found, not earned, and the live result will be the neighbourhood average.
   - the `validation` axis runs a Monte Carlo over trade *order*. Read
     `monte_carlo.max_drawdown_worst`, not the single historical drawdown — the realised
     sequence was one draw, and the worst across simulations is closer to what to expect.
   - the `costs` axis is the third: an edge that exists only at zero costs is an artifact.
3. **Out of sample** — `oos` for the cheap split, `walkforward` when parameters were
   fitted. The stitched OOS number is the only one worth quoting.
4. **Deflation and the null** — two opt-in axes that ask whether the winner is luck *given
   the search*:
   - `hermes analyze <key> --axes deflation` is the deflated Sharpe over the idea's
     effective trials. It is `ran: false` for a run with no `--idea` or under 10 trades:
     report that as not measured.
   - `hermes analyze <key> --axes null` replays the same trades at a random time through
     the engine and ranks the observed Sharpe against it. A p-value near 0.5 means the
     *timing* carries no information. Single-symbol, rule-based runs only.
5. **The holdout** — see §5. It is the last look, not a rung you climb repeatedly.
6. **Forward testing** — the rung Hermes **cannot** give you. A backtest, however clean,
   has seen all its data; the honest next step is paper or minimum size and comparing
   realised fills to the backtest over the same window. Hermes has no live venue
   (ADR-0001: the seam exists, the adapter does not), so say this explicitly in the
   report rather than implying the work is finished. Do not call a strategy deployable;
   call it ready to forward test.

`--note` is how the ledger becomes a research log instead of a list of hashes; future-you
in another session has only that note to reconstruct your reasoning.

Read the axes. `hermes analyze` computes them — you interpret them, you never recompute
them by eye. An axis with `ran: false` is **not measured**; say so and name what would
measure it. Never report an unmeasured axis as clean.

A zero-trade run already diagnoses itself (`zero_trade_diagnosis`): no bars stepped is a
data/window problem, everything vetoed is the gate, bars-but-no-signals is the
thresholds. Fix the stated cause; don't restate all three.

## 4b. Dissect the baseline — find where the edge lives

This is the step that turns a rejected idea into a found one. When the baseline is flat,
negative, or merely mediocre, **do not write variant #2 yet.** Open the trades and ask
what separates the ones that work from the ones that don't. Do it on in-sample data only,
as a script under `research/<topic>/` (output to `research/output/`), reading the run's
stored trades.

Cut the trades by whatever the idea says should matter, then by whatever it doesn't:

- **When:** hour/session, weekday, month, distance from the open, time held.
- **State of the market:** volatility regime (ATR percentile), trend vs range, gap size,
  distance from a moving average, prior-day range, proximity to a level.
- **Trade shape:** MAE/MFE — do winners go straight up or after a drawdown? Is the
  stop too tight (many stopped then reversed) or the target too far (MFE reached then
  given back)? Which exit is leaking?
- **Side:** longs vs shorts; one side carrying the other.
- **Concentration:** top-N trades, one year, one month, one regime.

You are looking for a **pocket**: a conditioning variable where expectancy changes
materially, with enough trades in each bucket to mean something (say so when a bucket
is thin). Then apply the mechanism test: *why* would this pocket exist? "Gap-ups under
0.5 ATR fade because stops cluster at the prior high" is a hypothesis. "Tuesdays at 14:35"
is a pattern found by looking — drop it unless a mechanism appears.

A pocket that has a mechanism becomes the next variant: a filter, a different exit, a
split-by-side, a regime gate. That variant is faithful to the idea (it sharpens it) and
is a trial like any other. Cheap structure (a filter on a state variable) beats tuned
numbers; prefer one clean condition to three thresholds.

Re-dissect after each real improvement and each real failure. A failed variant is
information about the mechanism: ask why it failed before building the next one.

## 5. Decide: iterate, or stop

Stop and report when **any** of these is true:

1. **The rubric holds.** Survives 1x and 2x costs, adequate sample size, not purely
   regime-dependent, and the gate (if any) was clean. Then earn the look:
   `hermes holdout check <id> --run-key <key>` measures the in-sample gate (tier at least
   `credible`, deflated Sharpe ≥ 0.9, null p < 0.05; an unmeasurable check fails) and burns
   nothing. Only when it passes, `hermes holdout unlock <id> --run-key <key>` runs the
   **frozen** strategy once on the holdout. It passes with the same Sharpe sign, at least 10
   trades and a PSR that rises with every earlier look at that instrument. A pass makes the
   idea `validated`; a fail makes it `rejected` as `holdout_failed`. **There is no second
   look**, even if you change the strategy afterwards. → *the edge looks real*, with the
   caveats the axes raised.
2. **The rubric rules it out** — e.g. profitable only at zero costs, or the entire P&L is
   one trade — **and the dissection (§4b) found no mechanism-backed pocket** that a
   faithful variant could isolate. Rejecting without having dissected is not allowed:
   say what you cut the trades by and that nothing held. → *the idea does not survive*.
   This is a successful outcome; say it plainly and do not soften it.
3. **Three consecutive variants with no out-of-sample gain and no new diagnosis behind
   them.** Variants that follow a fresh finding from §4b reset the count; variants that
   are just fiddling do not. Past this point you are the search, and the deflated Sharpe
   no longer accounts for it. Go back to §4b or stop.
4. **The build budget is spent.**
5. **The `params` axis says the result is a spike.** Do not iterate toward a sharper
   peak; that is the search finding the data, not an edge. Either widen the sample so the
   neighbourhood steadies, or replace the parameter with a structural condition from §4b.

A 4th tunable is not itself a stop: first ask whether a structural condition
(a state filter, a different exit) does the job without a knob. If it really needs one,
check `metrics.num_params` against `parameter_adjusted_sharpe` and trades per knob, and
justify it in the notes.

Otherwise iterate — but only on a change the analysis *pointed at* (§4b is where it
points). "All profit comes
from Bull regimes" suggests a trend filter. "In-sample Sharpe doesn't carry out of
sample" suggests fewer parameters, not more. "Top 5 trades are 100% of winning P&L"
suggests the sample is too small to conclude anything, so widen the window or the
instrument list before touching the logic.

**Never tune on the whole sample.** Parameter choices belong in-sample; use
`hermes analyze --axes walkforward` to score them out of sample. A number obtained by
picking the best parameters over all the data is not a result.

## 6. Report

Deliver, in this order:

1. **The verdict** — does the edge look real? One sentence, no hedging.
2. **The evidence** — the decisive axis readings, quoted with their numbers.
3. **The trail** — the **trial count**, then each iteration: what it tested, what
   happened, the run key; and the dissection findings that drove the pivots, including
   pockets you dropped for lacking a mechanism. This is
   what makes the work checkable, and the ledger already holds it.
4. **The biggest remaining threat** to the conclusion.
5. **What you'd do next**, and what you deliberately did not do (and why — budget,
   money, scope).
6. **Where it sits in the pipeline** — update the backlog
   (`hermes ideas set <id> --status rejected --verdict "…" --failure-kind <kind> --failure-assumption "…" --run-key …`; `validated` is
   set by a passed `hermes holdout unlock` and refused by hand), and
   if it survived, say that **step 4 is next**: a validated strategy is a candidate for a
   portfolio, not a portfolio. Route to `hermes-portfolio` to check whether it actually
   adds anything to what already exists.

Say which axes were **not** measured. A verdict that hides its gaps is worse than no
verdict, because it will be trusted.

Completion criterion: a stated verdict, the axis numbers behind it, the full iteration
trail with run keys, and an explicit list of what went unmeasured.
