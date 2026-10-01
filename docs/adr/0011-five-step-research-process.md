# The five-step research process

Hermes is organised around an explicit process for getting from "I have a hunch" to "I
have a portfolio": **idea generation → quantification → testing → portfolio → repeat.**

The process is not novel and that is the point — it is the working practice of
practitioners who got from zero algorithms to a live portfolio, and adopting it wholesale
is cheaper than rediscovering it. What ADR-0010 gave us was the machinery to run a loop.
This ADR says *which* loop, and makes the repo's tooling match it stage by stage.

## Why encode a process at all

An agent with good tools and no process optimises for the wrong thing. Given a backtest
and a free hand it will find a number that looks good, because finding numbers is what
search does. Every step below exists to deny it that, and each is backed by something
computed rather than suggested:

| Step | What it prevents | What enforces it |
|---|---|---|
| 1 Idea generation | Working on whatever is nearest, forgetting the rest | `research/ideas.json` + `hermes ideas`, with provenance and a per-source hit rate |
| 2 Quantification | Testing an idea that was never unambiguous | `hermes-strategy` §2 — hunt the ambiguity, declare the units, name every invented number |
| 3 Testing | Believing one favourable draw | the `analyze` rubric: evidence tiers, parameter sensitivity, Monte Carlo, cost sensitivity, walk-forward |
| 4 Portfolio | Owning one bet in four costumes | `hermes correlate` — the matrix and the diversification ratio |
| 5 Repeat | Polishing a finished strategy instead of filling a gap | `hermes-portfolio` names the gap; `hermes ideas --open` supplies the candidate |

## What this added

**Step 1 had nothing.** Ideas are the most valuable and least managed input, so
`hermes/research/ideas.py` gives them a backlog that survives sessions: a claim, a
mechanism, a provenance, a status, and the run keys that produced the verdict. Provenance
is a closed vocabulary (`book`, `discretionary`, `trader`, `course`, `data`) precisely so
`hermes ideas --hit-rate` can later answer *which sources are worth your time* — a
question nobody can answer from memory. The backlog lives in `research/` and **is
committed**: it is work product, and losing it loses the thinking.

**Step 3 was missing a whole half of robustness.** Hermes had `cost_sensitivity`
(does it survive friction?) and Monte Carlo over trade order, but nothing asked whether
the chosen *parameters* were a plateau or a spike. `param_sensitivity` sweeps each
parameter around its configured value, one at a time, and reports **neighbour
degradation**: the base metric against the mean of its immediate neighbours. A Sharpe of
1.4 at `fast=12` that collapses to 0.2 at 11 and 13 is not an edge at 12, it is an
artifact, and the live result will be the neighbourhood average. This is deliberately not
a grid search — a grid answers "what is the best combination", which is optimisation and
belongs to `WalkForward`. Conflating the two is how a search gets mistaken for an edge.

**Step 3 also had no notion of how much evidence is enough.** `MIN_TRADES = 30` was the
only bar, and 30 trades is enough to form a view and nowhere near enough to risk capital.
`SampleQuality` now reports an **evidence tier** — `exploratory` / `credible` /
`deployable` — requiring both a trade count *and* a span of history, because 800 trades
inside one year has not seen a second regime and five years with 40 trades has not seen
enough events. Whichever is short is the one named in `tier_reason`. A fourth tier,
`unknown`, exists for the case where no span is measurable: unmeasurable is not the same
as inadequate, and guessing "exploratory" would read as a finding.

**Step 4 had no support at all.** `PortfolioBacktest` shared an Account between legs but
said nothing about whether the legs were the same bet. `correlate_curves` aligns equity
curves onto a common calendar, correlates their returns, and reports the
**diversification ratio** — an equal-weight blend's volatility over the mean individual
volatility. 1.00 means you own one bet in several costumes; uncorrelated streams give
~1/√N; below that means a pair is negatively correlated and actively cancelling. Pairs
with too little shared history report `None`, never 0.0 — "unknown" and "independent" are
different claims and conflating them manufactures diversification that does not exist.

## Consequences

- **Forward testing (the last rung of step 3) is outside Hermes.** There is no live venue
  (ADR-0001: the seam exists, the adapter does not), so a validated backtest is *ready to
  forward test*, never "deployable". Skills must say so rather than implying the work is
  complete; `TIER_DEPLOYABLE` names the quality of the *evidence*, not a verdict on
  whether to trade it.
- **Hermes is bars-only, and step 2 is where that bites.** Many good discretionary ideas
  are about order flow ("large orders hitting the bid"), which cannot be expressed here.
  Quantification must park those or state plainly that a volume-based proxy is a
  *different idea* — silently substituting one for the other tests something nobody
  proposed.
- **The correlation figures are the optimistic case.** They are measured on backtest
  curves over shared history, and correlations rise together in drawdowns — exactly when
  diversification is supposed to help.
- The backlog is a committed file, which makes it the first Hermes artifact that is
  neither code nor cache. `research/output/` stays ignored; `research/ideas.json` does not.
