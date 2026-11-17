# The driver is a loop over `next_action`, with fresh sessions and verified outputs

Status: accepted (phase 4, 2026-11). Builds on ADR-0014 (roles, state, approvals), ADR-0017
(guarded transitions) and ADR-0012 (the holdout is enforced).

## Problem

Phase 3 made the state machine a pure function (`next_action`) and the guards live in the
library. Something has to *run* it: launch an agent per step, notice when it did not do the job,
survive a crash or a token limit, and never decide for the owner.

## Decision

- **A loop around `next_action`, with no state of its own.** `Driver.run` asks what is next, does
  it, asks again. The only thing it writes is `research/ideas/<id>/run.jsonl`, an audit trail.
  Resume is "call it again": a finished step left its artifact, so it is not repeated, and an
  interrupted one is idempotent (artifacts are written atomically).
- **A fresh session per step; state on disk.** Each role runs as a new `claude -p` with the context
  pack in its prompt. It costs a re-read per step and buys crash-resume and a context that does not
  grow with the idea.
- **Outputs are verified, not exit codes.** After a role step the driver checks the files against
  the validators (`validate_spec`, `validate_plan`, the plan lock, the ran-variant set, …). A clean
  exit that wrote an invalid spec is retried twice with the problem list, then the idea parks on
  an `other` approval (`driver_stalled`). Review of a plan is refused *mechanically* first.
- **The driver does transitions; roles do judgement.** `mark_*`, `reject_idea`, `resume_from_infra`
  and `unlock_holdout` are library calls made as `HERMES_ROLE=driver` once their preconditions
  hold. `unlock_holdout` is automatic when the Validator's verdict is `ready_for_unlock` (one look
  per idea, recorded; parking it would stall every successful idea). The owner still decides
  `ready_to_forward_test`.
- **The recommendation is computed and can only be tightened.** `validation_fill` applies the
  in-sample gate's thresholds to the stored analysis and writes a `recommendation`;
  `ArtifactStore` refuses a verdict more permissive than it and requires an `override_reason` for
  a different one. The agent cannot rewrite the computed fields.
- **Files are sealed.** After the Validator's step the driver makes `plan.json` and
  `validation.json` read-only; the store refuses to write a sealed file (an atomic replace would
  otherwise ignore the mode). A new round unseals through the sanctioned path (`amend_plan`,
  `write_mechanical`), and `amend_plan` archives the old verdict as `validation.round<N>.json`.
- **Developer extensions are classified mechanically.** The diff, not the Developer, decides
  `dev_merge` or `engine_semantics` (any engine path: backtest, execution, core, indicators, the
  holdout/unlock/idea/artifact/approval guards). Merge happens on `hermes pipeline answer … approve`,
  from the CLI, after both gates are re-run on the branch; nothing in the flow pushes.
- **The advisor needs an approval that is a number.** A run with an advisor, for an idea or by a
  pipeline role, needs `--advisor-approval` of an approved advisor approval for that model whose
  estimated `high` is not yet spent; an unpriced model cannot be approved. Spending the estimate
  stops the next run and parks a new approval with the new figure.
- **Roles are files in the package** (`research/roles/<role>.md`), versioned with the code.

## Spikes (recorded, not designed around)

- **Headless invocation** (`claude` 2.1.326): `claude -p <prompt> --model … --output-format json
  --allowedTools … --disallowedTools … --append-system-prompt … --session-id <uuid>
  --permission-mode acceptEdits`, with the working directory and environment set on the process.
  `--output-format json` reports `result`, `is_error`, `session_id` and `usage`. **There is no
  per-run turn cap flag**, so the bounds are the driver's step and attempt limits and a process
  timeout (`HERMES_ROLE_TIMEOUT`, default 1800 s).
- **Token limit: not provoked.** The headless behaviour at the subscription limit could not be
  reproduced in a spike, so detection is by message (`usage limit`, `limit reached`, `resets at …`)
  on a failed run, which becomes a *pause* (exit 3, `paused` in `run.jsonl`, resumable). A miss is
  an ordinary failed step — retried, then parked — never a silent success. Confirm against a real
  limit before relying on the reset time.
- **F13** (integer `Parameter` discretised to floats in walk-forward) reproduced and fixed: an
  integer parameter with integer bounds is searched over distinct integers.

## Consequences

- Nothing in the tests starts an agent: `RoleRunner` is the seam, `ScriptedRunner` writes through
  the real guards.
- The agent-side boundaries are the same honest ones as ADR-0012/0017: env vars and tool lists stop
  an honest agent, not a determined one with a shell; `history`, `reads.jsonl` and `run.jsonl`
  make a bypass visible.
- Not done: a live 5-call token sample for a never-used advisor model (the caller supplies tokens
  per call instead; the estimate says `source: given`); advisor spend is checked before a run, so
  one run can overshoot the estimate; one idea per invocation (the per-run cap is phase 6).
