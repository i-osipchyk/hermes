# Research pipeline: roles, file-based state and parked approvals

Hermes research is driven by a pipeline of role agents over a file-based state machine, which
parks anything needing the owner's approval and carries on.

ADR-0010 made Hermes drivable by an agent and ADR-0011 defined the process. The skills work
separately and comfortably; nothing chooses the next idea, runs several in a row, or stops the
loop. The goal is a hedge-fund-style workflow: literature, hypothesis, code, backtest,
validation, reflection, next idea.

## Decision

- **Roles:** Researcher, Strategist (PM), Validator, Quant, Developer, plus Haiku-level mechanical
  steps. The Validator is a different agent from the one that wrote the strategy, and
  `validation.json` is read-only for the Quant: nothing grades its own work. Model split is
  Opus (Researcher, Strategist, Validator), Sonnet (Quant, Developer), Haiku (mechanical).
- **Driver:** the state machine and hand-offs live in the library and CLI (`hermes pipeline …`) per
  ADR-0010; a `hermes-pipeline` skill is a thin front-end. State is files, so a crashed or paused
  run resumes without repeating work and the later continuous loop is the same driver on a schedule.
- **Idea state:** extends the current statuses and keeps them valid. Per-idea artifacts
  (`spec`, `plan`, `trials`, `validation`, `lessons`, `holdout`, `reads`) live in
  `research/ideas/<id>/` and are committed. Each artifact must make clear to the next iteration what
  was tested.
- **Pre-registered test plan:** before any variant runs, each variant declares the structural
  axis it changes and what result would change the decision. The Validator reviews it.
- **Approvals are parked, not blocking.** An unattended agent sets the idea to `awaiting_approval`
  with the exact question and a recommended answer, and moves on. `hermes pipeline approvals` lists
  them. Needs approval: an AI Advisor (with a cost estimate, below), changing the instrument or
  the idea, engine-semantics changes (ADR-0001/0002/0004), a Developer branch merging to local
  `master`, and promoting an idea to `ready_to_forward_test`. Not needed: Polygon, Tiingo.
- **Advisor approvals carry an estimate:** signal count from an advisor-off run, tokens per call from
  past `LLMObservabilityLog` rows (else a ~5-call sample), llmkit's price for the model (default
  `deepseek-flash`), minus cached signals, as a low/high range multiplied across planned advisor
  variants. The approval covers that model and estimate; exceeding the high figure parks again.
- **Developer** works on `dev/<topic>` with the `tdd` skill and green gates. Additive extensions
  merge to local `master` after their own tests and review, whatever the idea's fate, as a visible
  approval. Engine-semantics changes always wait. Agents never push.
- **Cost control:** agents run on a Claude Code subscription, so there is no dollar ceiling. When the
  token limit is hit the pipeline parks the run as resumable.
- **Synthesis:** the Strategist maintains `research/thesis.md` and proposes hypotheses from past
  results, with `informed_by` set so deflation (ADR-0013) counts what was used. It runs after every
  5 processed ideas, when the queue is empty, and on demand. The owner's manual priority goes first.

## Considered and rejected

- **A skill-only pipeline:** loses state when the session ends and cannot become a scheduled loop.
- **Blocking on every approval:** defeats an unattended run; parking lets other ideas proceed.
- **A single agent grading its own work:** the failure the process exists to prevent.
- **A dollar ceiling per run:** the wrong cap on a subscription; the real limit is tokens.
- **A database or vector store for memory:** at ~30 ideas, JSON and `hermes runs --note-contains`
  are enough. Revisit if the backlog grows by an order of magnitude.

## Consequences

- "Ready to forward test" stays a suggestion; Hermes has no live venue. The owner reviews it and
  tests in a separate repo.
- The existing skills are updated rather than replaced (`hermes-research`, `hermes-ideas`,
  `hermes-strategy`, `hermes-extend`).
- Validation hardening (ADR-0012, ADR-0013) must land before the agents, because they will exploit
  any weakness in the rubric.
