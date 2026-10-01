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

The failure this skill exists to prevent is not a bug. It is coming back with
"here's a strategy, it returned 40%" — a number produced by trying things until one
worked. Your job is the opposite: to attack your own best result until it either survives
or dies honestly.

## 0. Budget — set it before you start

You run **unattended**, so you get a leash and you say what it is. Default budget:

- **6 iterations** (an iteration = one variant written, run, and analysed), and
- **≤ 3 tunable Parameters** in any variant you write.

State the budget in your first message, then work. Stop early per §5. If you hit the
budget without a verdict, say so and report what you learned — an honest "inconclusive
within 6 iterations" is a real result and far more useful than a 7th variant found by
fiddling.

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
that can express it. That first run is your baseline; every later variant is judged
against it, not against zero. A variant is only worth running if you can say in advance
what it would prove.

## 4. Run and judge — the testing ladder

Climb it in order; each rung is cheap relative to the one above and kills most candidates
before you pay for the next.

```bash
.venv/bin/hermes run <name> --note "<what this variant tests>"     # --note is not optional
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
4. **Forward testing** — the rung Hermes **cannot** give you. A backtest, however clean,
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

## 5. Decide: iterate, or stop

Stop and report when **any** of these is true:

1. **The rubric holds.** Survives 1x and 2x costs, holds out of sample, adequate sample
   size, not purely regime-dependent, and the gate (if any) was clean. → *the edge looks
   real*, with the caveats the axes raised.
2. **The rubric rules it out** in a way no faithful variant would fix — e.g. profitable
   only at zero costs, or the entire P&L is one trade. → *the idea does not survive*.
   This is a successful outcome; say it plainly and do not soften it.
3. **No improvement in 2 consecutive iterations** on the *out-of-sample* number. Stop.
   Continuing past this point is hand-optimisation: you become the search, and the
   deflated Sharpe stops accounting for it.
4. **The parameter budget is spent.** If the next idea needs a 4th tunable, stop instead
   of adding it. Check `metrics.num_params` against `parameter_adjusted_sharpe` and the
   trade count — a variant with more knobs and fewer trades per knob is worse even when
   its raw Sharpe is higher.
5. **The `params` axis says the result is a spike.** Do not iterate toward a sharper
   peak; that is the search finding the data, not an edge. Either widen the sample so the
   neighbourhood steadies, or drop the parameter.

Otherwise iterate — but only on a change the analysis *pointed at*. "All profit comes
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
3. **The trail** — each iteration: what it tested, what happened, the run key. This is
   what makes the work checkable, and the ledger already holds it.
4. **The biggest remaining threat** to the conclusion.
5. **What you'd do next**, and what you deliberately did not do (and why — budget,
   money, scope).
6. **Where it sits in the pipeline** — update the backlog
   (`hermes ideas set <id> --status validated|rejected --verdict "…" --run-key …`), and
   if it survived, say that **step 4 is next**: a validated strategy is a candidate for a
   portfolio, not a portfolio. Route to `hermes-portfolio` to check whether it actually
   adds anything to what already exists.

Say which axes were **not** measured. A verdict that hides its gaps is worse than no
verdict, because it will be trusted.

Completion criterion: a stated verdict, the axis numbers behind it, the full iteration
trail with run keys, and an explicit list of what went unmeasured.
