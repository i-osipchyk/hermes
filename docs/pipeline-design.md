# Agentic research pipeline — design

Status: **agreed design; phases 1 (audit) and 2 (validation hardening) are implemented, the pipeline driver and agents are not** (as of 2026-11-17). Phase 1 is done:
see [audit-2026-11.md](audit-2026-11.md). This is the document a new session reads first.
Decisions that are load-bearing have their own ADR: [0012](adr/0012-holdout-enforced-in-the-data-layer.md),
[0013](adr/0013-trial-clusters-and-deflation.md), [0014](adr/0014-research-pipeline-roles-state-and-approvals.md).

## Goal

Hermes becomes a hedge-fund-style research pipeline: **literature → hypothesis → code → backtest →
validation → reflection → next idea.** One idea runs end to end first; a queue runner and a
continuous loop come second. The agent works on its own unless something needs the owner's
approval. The existing skills (`hermes-ideas`, `hermes-strategy`, `hermes-research`,
`hermes-extend`, `hermes-portfolio`, `hermes-analyze-results`) are extended, not replaced.

The point of every rule below is the one ADR-0011 states: an agent with good tools and no process
finds a number that looks good. AI generation will multiply the number of ideas, so the
validation must assume the search is adversarial to itself.

## Roles

| Role | Does | Model |
|---|---|---|
| Researcher | Literature for a general idea, or turns a concrete idea into a falsifiable claim. Every idea needs a source link. | Opus |
| Strategist (PM) | Maintains `research/thesis.md` (what works, what fails, recurring mechanisms, open questions); proposes hypotheses from it. | Opus |
| Validator (skeptic) | Reviews test plans; interprets `hermes analyze`. A different agent from the one that wrote the strategy. `validation.json` is read-only for the Quant. | Opus |
| Quant | Writes the strategy and runs variants (`hermes-strategy`, `hermes-research`). | Sonnet |
| Developer | Extends Hermes when infrastructure is missing (`hermes-extend`, built with `tdd`). | Sonnet |
| Mechanical | Bookkeeping: ledger and ideas updates, file hand-offs. | Haiku |

No Fable for now. The agents run on a Claude Code subscription: there is no dollar ceiling, the
only cap is the token limit. When it is hit the run is parked as resumable and continues after
the reset.

## Driver and state

- The state machine and file hand-offs live in the library and CLI (`hermes pipeline …`), per
  ADR-0010 ("new research capability belongs in `hermes/research/`"). A skill (`hermes-pipeline`)
  is a thin front-end. State is files, so a crashed or paused run resumes without repeating work.
- Idea statuses extend today's `raw / quantified / testing / validated / rejected / parked`
  (`research/ideas.py`) and keep them valid:
  `raw → literature_done → quantified → coded → testing → validated`, and the terminal or parked
  outcomes `rejected`, `needs_infra`, `awaiting_approval`, `ready_to_forward_test` (the existing
  names `quantified` and `testing` are kept; "reflected" is the presence of `lessons.json`, not a
  status — see [phase-3-memory-and-state-plan.md](phase-3-memory-and-state-plan.md)). Old records
  stay valid and are backfilled lazily when next touched.
- Per-idea artifacts live in `research/ideas/<id>/` and are committed like `ideas.json`:
  `spec.json`, `plan.json` (the pre-registered test plan), `trials.json`, `validation.json`,
  `lessons.json`, `holdout.json`, `reads.jsonl`. Anything an iteration needs to know about what
  was tested must be readable from there.
- Idea records gain `parent`, `informed_by`, a structured failure reason (which assumption broke),
  and dedupe against rejected ideas.

## Research depth (the "tiny steps" problem)

Observed: ideas ended with few runs (0–7 for most of the 29 in the backlog), and the variants
tried were near-identical, so a new one rarely had a real chance. Causes named by the owner:
the loop stopped on a rule before diagnosing, and it never dug into trade-level evidence.

- Before any variant runs the Quant writes a **pre-registered test plan**: each variant declares
  the **structural axis** it changes (entry mechanism, exit logic, regime filter, instrument or
  asset class, timeframe, holding horizon — a parameter nudge is not an axis), what it tests, and
  which result would change the decision. The Validator reviews it and rejects plans whose
  variants are mostly one cluster.
- Hermes computes return correlation and trade overlap between a new variant and earlier ones;
  above ~0.9 it counts as the same **trial cluster** and is flagged "step too small".
- Sweeps and diagnostics are free; a **trial** is only a run that is a candidate for selection.
  `hermes-research` already says diagnosis is not a trial.
- Budgets: counted by cluster, not by run; ≤ 3 Developer extensions per idea; a per-run cap on
  ideas processed. The existing stop rule stays (three variants with no OOS gain and no new
  diagnosis behind them means stop).

## Validation hardening (phase 2) — see [phase-2-validation-plan.md](phase-2-validation-plan.md)

- **Holdout:** last 20% of history, at least 6 months, per instrument and timeframe, frozen at
  first research touch, blocked in the data layer for research runs, unlocked once per idea.
- **Trial clusters and deflation:** effective trials = the idea's own clusters + the clusters of
  every idea in `informed_by` (declared, audited against a read log) + a small batch floor.
  Sharpe is deflated by that count.
- **Null test for rule-based strategies** (audit gap G1) so there is a distribution to deflate
  against and a way to run planted-answer tests.

## Memory and synthesis

- The Strategist runs after every 5 processed ideas, when the queue is empty, and on demand. It
  reads results and lessons, rewrites `research/thesis.md`, and proposes hypotheses that enter the
  backlog with `informed_by` set. Manual priority from the owner always goes first.
- Reflection writes `lessons.json` and may add child ideas, deduped against rejected ones; children
  inherit `parent` and `informed_by`, so deflation counts the parent's trials automatically.
- The thesis file is what an agent reads at session start, not all idea records. No database or
  vector store: at this size JSON plus `hermes runs --note-contains` is enough.

## Approvals

Needs the owner: an AI Advisor (billed per signal), changing the instrument or the idea,
engine-semantics changes (ADR-0001/0002/0004), the merge of a Developer branch to local `master`,
and promoting an idea to "ready to forward test". Not needed: Polygon and Tiingo (subscriptions).

An unattended agent **parks** the idea as `awaiting_approval` with the exact question and a
recommended answer, then moves on. `hermes pipeline approvals` lists them for a batch answer.
Agents never push.

**AI Advisor requests carry a cost estimate** (default model `deepseek-flash`, which has a price
row in llmkit):
1. signal count from a run with the advisor off;
2. tokens per call from past `LLMObservabilityLog` rows for that component and model, else a
   ~5-call sample;
3. cost = signals × average tokens × llmkit price, minus signals already in the advisor cache;
4. a low/high range, multiplied across advisor variants in the plan.

The approval is for that model and estimate. Exceeding the high figure parks the run again; a
different model needs a new approval; a model with no llmkit price row is "unpriced" and the
request names the platform PR that fixes it.

## Developer

`dev/<topic>` branch, `tdd` skill, tests and ruff green, a test per addition. Additive
extensions (indicators, data sources, research tools) merge to local `master` after their own
tests, ruff and review, whatever happens to the idea, and the merge is a visible pending approval.
Anything touching fills, costs, bar or forming-bar logic, or look-ahead rules waits for the owner.
Never pushes.

## Phases

1. **Audit** — done, [audit-2026-11.md](audit-2026-11.md).
2. **Validation hardening** — holdout, trial clusters, deflation, null test. Plan in
   [phase-2-validation-plan.md](phase-2-validation-plan.md). Depends on audit fixes F2–F7.
3. **Memory and state** — richer idea records, the state machine. **Implemented**; plan and
   as-built notes in [phase-3-memory-and-state-plan.md](phase-3-memory-and-state-plan.md), decision
   in ADR-0017.
4. **Roles and the single-idea pipeline**, including the advisor cost estimate. **Implemented**; plan and as-built
   notes in [phase-4-roles-and-pipeline-plan.md](phase-4-roles-and-pipeline-plan.md), decision in ADR-0018.
5. **Test of the workflow** — one real raw idea from backlog to verdict with no human input beyond
   parked approvals; a crash resumes from files; planted-answer ideas (one bogus, one real) as a
   regression check on the validation.
6. **Queue runner, Strategist synthesis, literature loop.**

## Skill changes this implies

- `hermes-research`: holdout 30% → 20% and enforced rather than promised; the ~15-variant cap
  becomes the cluster-based budget; add the test-plan step.
- `hermes-ideas`: literature search, dedupe, `informed_by`.
- `hermes-strategy`, `hermes-extend`: called by the Quant and Developer roles.
- New: `hermes-pipeline` (front-end), plus one short skill per new role where the existing ones do
  not already cover it.

## Open details (not yet decided)

- Exact thresholds: cluster correlation 0.9 is a starting value; the DSR bar and the holdout pass
  criterion are proposed in the phase 2 plan and must be calibrated with the planted-answer tests.
- Minimum in-sample span below which no holdout is carved (proposed 12 months).
- Whether holdout "looks" by several ideas on the same instrument are deflated as well (proposed:
  yes, see ADR-0012).
- Which null-test construction to use (see phase 2 plan, G1).
- Dependency discount for `informed_by` (starts undiscounted).

## Decisions log (owner's answers, 2026-11-16)

Automation is one idea end to end first, continuous loop second; AI Advisor needs approval,
Polygon and Tiingo are fine; forward test is a suggestion that the owner reviews in a separate
repo; input may be a concrete idea or a general topic for the Researcher to find literature on;
hedge-fund roles with a separate adversarial Validator; audit before agents, with a findings list
approved first, and missing infrastructure is supplied by a Developer agent; holdout, trial
clustering and deflation all yes; trial counting depends on the pipeline's ideas and on which
earlier results the agent actually used; synthesis from past results is a first-class source of
hypotheses; Opus/Sonnet/Haiku split; skills are updated, not replaced.
