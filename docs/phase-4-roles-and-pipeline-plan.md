# Phase 4 — roles and the single-idea pipeline: implementation plan

Status: **implemented** (branch `feat/phase-4-roles-and-pipeline`; see "As built" at the end for where the code differs from this plan). Decisions confirmed by the owner (2026-11-17, "sounds good"). Read
[pipeline-design.md](pipeline-design.md) first, then [ADR-0014](adr/0014-research-pipeline-roles-state-and-approvals.md)
and [phase-3-memory-and-state-plan.md](phase-3-memory-and-state-plan.md) (its "As built" section
lists what this phase plugs into). Phases 1–3 are on `master`.

Rules for the session that implements this: TDD (`tdd` skill), tests offline in `tmp_path`, both
gates green (`pytest`, `ruff check .`), new capability in `hermes/research/` and reachable from the
CLI, not in `webui/`, strict JSON through `research/jsonsafe.py`, new env knobs prefixed `HERMES_`
and read at call time. **No test starts a real agent or touches the network**: the agent
boundary is a seam (`RoleRunner`) and tests use a scripted fake.

## What this phase delivers, and what it does not

Delivers: the **driver** that takes one idea from its current status to a verdict by repeatedly
asking `next_action` and running the named role; the **role definitions** (prompt, model, tools,
permissions) and the skill changes that go with them; the **advisor cost estimate** and its
approval; the **Validator's mechanical half** (filling `validation.json` from `analysis.json`,
trials and holdout state); the **Developer flow** (`needs_infra` → branch → tests → a parked
`dev_merge`); and the stop rules (step, retry, cluster, extension and token limits).

Does not: run an idea against real data with real agents (phase 5), pick the next idea from a
queue, run the Strategist or write `thesis.md`, search literature on a schedule (phase 6), or
re-run the two pre-holdout validated ideas (flagged, not part of this phase).

## Findings from reading the code that shape the plan

- `research/pipeline.next_action(idea, store, ran_variants=…)` is already a pure function from
  status and artifacts to `Action(role, step, requires, produces)`. The driver is a loop around it.
  Resume is "call it again", so the driver keeps **no state of its own** beyond a log.
- Some steps in `next_action` are role steps (`write_spec`, `write_plan`, `review_plan`,
  `write_strategy`, `run_variants`, `validate`, `reflect`, `build_infra`) and some are
  **transitions** the driver can do itself once the preconditions hold (`mark_quantified`,
  `mark_coded`, `mark_testing`, `reject_idea`). Those need no agent, only a library call with the
  right `by`.
- `unlock_holdout` is returned with role `owner`, but ADR-0012 and the design list only the
  *promotion* to `ready_to_forward_test` as an owner decision. See decision 3.
- `validation.json` has a `Validation` artifact (phase 3) that nothing fills yet. Its mechanical
  fields are all computable (`hermes analyze`, `hermes trials`, the holdout store); only
  `verdict` and `objections` are judgement.
- `hermes run` blocks a universe run with an advisor unless `--allow-advisor`; a single-symbol run
  with an advisor has no gate at all. The advisor approval and estimate need an enforcement point
  in the CLI, not only in a prompt.
- The artifact permission model keys on `HERMES_ROLE`; nothing sets it yet. The driver is the
  thing that sets it, per subprocess.
- Audit F13 (an integer `Parameter` discretised to floats in walk-forward) is unchecked and the
  Quant will hit it on the first parameterised idea.

## 0. Spikes, before any code

Two facts about the Claude Code CLI decide the driver's shape; check them, record the answers in
the ADR, and do not design around guesses.

1. **Headless invocation.** How a role is launched non-interactively (`claude -p`), how the model,
   the tool allow/deny list, an appended system prompt and the working directory are set, how
   `--output-format json` reports the result, and whether a per-run turn cap exists.
2. **The token limit.** What a headless run does when the subscription limit is hit: exit code,
   message, whether a reset time is reported. The design promises "park as resumable"; the
   detection depends on this.

Also check **F13** with a one-line reproduction and either fix it or record it as accepted for
this phase.

## 1. The driver — `research/driver.py`

```
hermes pipeline run <id> [--until STEP] [--max-steps N] [--dry-run] [--json]
hermes pipeline step <id>            # exactly one step, for debugging and for tests
```

Loop, per iteration:

1. `act = next_action(idea, store, ran_variants=…)`. `None` → done (print why).
2. **Park check.** A pending approval on this idea, or `act.role == "owner"` for a decision that
   is the owner's → stop with exit `0` and a message naming the approval. The driver never waits.
3. **Transition steps** (`mark_*`, `reject_idea`) → perform the library call with `by="driver"`
   and the reason from the artifact; loop.
4. **Role steps** → `RoleRunner.run(role, idea, step, context)`; then **verify**:
   the files in `act.produces` exist, validate (`validate_spec`, `validate_plan`, …), and
   `next_action` now returns something different. Not verified → retry up to 2 times with the
   problem list appended to the prompt; still not → park the idea with `resume_status` and a
   `driver_stalled` note (an approval of kind `other`, so it shows up in the batch).
5. Append one line to `research/ideas/<id>/run.jsonl`: `{at, step, role, model, attempt, outcome,
   duration_s, tokens, session_id}`. This is the audit trail of the driver, not its state.

Stop rules, all enforced in the driver and not by the prompt:

| Limit | Default | When hit |
|---|---|---|
| steps per `run` | 40 | stop, resumable, exit `0` |
| attempts per step | 3 (1 + 2 retries) | park as above |
| clusters per idea | `plan.budget.max_clusters` | `hermes run --variant` refuses; the Quant must amend the plan (reviewed again) |
| Developer extensions per idea | 3 | the idea goes to `parked` with `failure.kind=infra` |
| token limit | n/a | **paused, resumable** (below) |
| ideas per invocation | 1 here; the cap becomes real in phase 6 | |

**Token limit.** When the runner reports the limit, the driver writes `paused` to `run.jsonl`
with the reset time if known and exits with a dedicated code. `hermes pipeline run <id>` later
picks up from `next_action`; nothing is repeated because completed steps left their artifacts. A
step that was interrupted mid-way is simply run again; steps are idempotent by construction
(they write artifacts atomically and the driver verifies outputs, not exit status).

**`--dry-run`** prints the sequence of steps the driver *would* take with the current files and
stops at the first role step. It needs no agent and is the cheapest way to see where an idea is.

### The agent boundary — `RoleRunner`

```python
class RoleRunner(Protocol):
    def run(self, role: Role, idea: str, step: str, prompt: str) -> RunResult: ...
```

- `ClaudeCodeRunner` (real): launches `claude -p` per the spike, sets `HERMES_ROLE`,
  `HERMES_IDEA`, `HERMES_RESEARCH=1`, the model for the role, the tool allow/deny list, and the
  working directory, and parses the result.
- `ScriptedRunner` (tests): a dict of `(role, step) → callable` that writes artifacts through the
  real `ArtifactStore`, so the tests exercise the real guards (permissions, plan lock,
  transitions).

Each role invocation is a **fresh session** that starts from `hermes pipeline context <id>` and
the step prompt. Nothing is carried in a conversation; everything it needs is on disk. This is
what makes the driver resumable and what keeps one idea's context from growing without bound.

## 2. Role definitions — `src/hermes/research/roles/<role>.md`

Each file has frontmatter `{role, model, tools_allow, tools_deny, skills, writes}` and a body that
is the role's standing prompt. The driver reads them; they are also what a human reads to learn
what a role may do. Prompts are short and point at skills and at `hermes pipeline context`.

| Role | Model | Steps | May write | Tools |
|---|---|---|---|---|
| Researcher | Opus | `write_spec`, `reflect` | `spec.json`, `lessons.json`, idea `links`/fields | `hermes` CLI, web search/fetch, file read; no strategy edits |
| Quant | Sonnet | `write_plan`, `write_strategy`, `run_variants`, `amend_plan` | `plan.json` (draft), `strategies/**` | `hermes` CLI, file read/write in `strategies/`; **no** read of `validation.json` content beyond the verdict line |
| Validator | Opus | `review_plan`, `validate` | `plan.json` review block, `validation.json` | `hermes` CLI read commands and `hermes pipeline validate`; **no Bash beyond that**, no file writes outside its artifacts (the headless review already runs without Bash for the same reason) |
| Developer | Sonnet | `build_infra` | `dev/<topic>` branch in its own worktree | full dev tools in the worktree; `tdd` skill; never pushes |
| Mechanical | Haiku | `mark_*`, bookkeeping, formatting the approvals digest | idea status via the library | `hermes` CLI only |

Boundaries the driver enforces, not the prompt:
- `ArtifactStore` already checks `HERMES_ROLE` per artifact (phase 3).
- The driver makes `validation.json` and a locked `plan.json` **read-only on disk** after the
  Validator writes them (phase 3 left this open), so a later write fails at the filesystem.
- A role's `tools_deny` is passed to the CLI; the Validator cannot edit a strategy and the Quant
  cannot edit a review.
- **Honest limit** (as ADR-0012): an agent with a shell and a determined intent can step around
  all of this. The `history` and `run.jsonl` trails make that visible after the fact.

## 3. The Validator's mechanical half — `research/validation_fill.py`

`hermes pipeline validate <id>` (called by the Validator at the start of its step, or by the
driver) computes and writes the mechanical fields of `validation.json`, leaving `verdict` and
`objections` for the Validator:

- per-axis pointers into the latest `analysis.json` of each cluster representative, **including
  axes with `ran: false` and the command that would measure them** (never presented as clean);
- evidence tier and its reason, `N_eff` with where each count came from, DSR, null p;
- holdout state (carved or capped at `exploratory`, unlocked or not);
- the `informed_by` audit (`read_not_declared`, `declared_not_read`) as a flag;
- the plan check: variants that share a cluster, planned-not-run, run-not-planned;
- a **recommendation** computed from the gate (`reject` / `continue` / `ready_for_unlock`) that the
  Validator may override only *toward* more caution, with the reason recorded.

Hermes computes, the Validator interprets (CLAUDE.md). The recommendation exists so a thin
model cannot talk itself into `ready_for_unlock` against the numbers.

## 4. Quant steps and the research-depth rules

`write_plan` is the step that fixes the "tiny steps" problem: variants carry a structural axis,
and `hermes trials --plan` (phase 3) flags variants that share a cluster. This phase wires it in:

- `review_plan` refuses (mechanically, before the Validator's judgement) a plan where more than
  half the variants have the same axis or where `validate_plan` returns problems.
- `hermes run --idea <id> --variant <v>` refuses a variant that is not in a locked plan and
  refuses to start a new cluster once `max_clusters` is reached. Diagnostics (`--diagnostic`) stay
  free.
- The existing `hermes-research` stop rule stays: three variants with no out-of-sample gain and
  no new diagnosis behind them means stop. The driver cannot judge "new diagnosis"; the Validator
  does, in `validate`, and says `reject` or `continue` with the reason.

## 5. Advisor estimate and approval — `research/advisor_estimate.py`

```
hermes pipeline estimate-advisor <idea> <variant> [--model deepseek-flash] [--json]
```

Computed, never asked of an LLM:

1. **Signals:** run the variant with the advisor off (a free diagnostic) and count the signals the
   advisor would see.
2. **Tokens per call:** the mean of past `LLMObservabilityLog` rows for `component="advisor"` and
   that model; with none, a 5-call sample run live (small, disclosed in the estimate as
   `source: sample`).
3. **Price:** llmkit's price row for the model. No row → `unpriced`; the estimate says so, names
   the platform PR that fixes it (CLAUDE.md), and the approval cannot be approved on a number.
4. **Cached signals** (the advisor cache) are subtracted.
5. Output a **low/high** range, multiplied across the planned advisor variants.

The estimate goes into `Approval.estimate` (phase 3 reserved the field) when the Quant parks an
`advisor` approval, so the owner reads the question, the recommendation and the cost in one place.

**Enforcement.** A run with an advisor needs `--advisor-approval <approval id>`; the CLI checks the
approval is `approved`, the model matches, and the estimate's `high` has not been exceeded by the
spend recorded so far for that approval (llmkit rows tagged with the idea). Exceeding `high` stops
the run and parks again with the new figure; a different model needs a new approval. A denied
advisor approval returns the idea to its previous status with a note (phase 3 decision) and the
Quant continues without the advisor. The existing universe-run `--allow-advisor` flag stays.

## 6. The Developer flow — `research/dev_flow.py`

Trigger: a role records `needs_infra` with `failure.detail` naming the missing capability (phase 3).

1. The driver creates a **git worktree** on `dev/<topic>` so the Developer never touches the
   working tree the other roles read.
2. Developer role runs `hermes-extend` and `tdd`: tests first, then the code, both gates green in
   the worktree. A **manifest** (`research/ideas/<id>/extension.json`: topic, files touched,
   what it adds, tests added) is written at the end.
3. **Classification is mechanical.** If the diff touches fills, costs, bar or forming-bar logic,
   or anything under the engine paths listed in `CLAUDE.md`/ADR-0001/0002/0004/0015, the
   approval kind is `engine_semantics` and the branch waits for the owner whatever the Developer
   says. Otherwise it is `dev_merge`. The Developer cannot downgrade it.
4. The driver parks a `dev_merge` approval carrying the branch, the diff stat, test counts and
   ruff output, and the idea continues only when the capability is available. **Merge** happens on
   `hermes pipeline answer <id> approve`: the CLI re-runs both gates on the branch, then merges to
   local `master`. It never pushes. If the gates fail at merge time the approval is refused with
   the output.
5. Limits: ≤ 3 extensions per idea; an extension that is wanted by the idea but not additive in
   nature goes through `engine_semantics`.

## 7. Skill and doc changes

- New `hermes-pipeline` skill: the thin front-end (`status`, `run`, `approvals`, `answer`), with
  the rule that the agent drives the CLI and never edits `ideas.json`.
- Role skills where existing ones do not already cover the role (Researcher reflection and
  literature, Validator): short, pointing at `hermes pipeline context`.
- `hermes-research`: holdout 30% → 20% and enforced; the ~15-variant cap becomes the cluster
  budget; the test-plan step; `--idea`/`--variant`/`--diagnostic` on runs.
- `hermes-ideas`: literature step, `links`, dedupe, `informed_by`, the new statuses.
- `hermes-strategy`, `hermes-extend`: stated as called by the Quant and Developer roles; the
  extension manifest.
- `CONTEXT.md`: **Role**, **Driver**, **Step**, **Role Runner**, **Extension**, **Advisor
  Estimate**. `docs/MANUAL.md`: a "run an idea through the pipeline" section. `CLAUDE.md`:
  the pipeline pointer. `pipeline-design.md`: phase 4 status. **ADR-0018** (reserved): the driver
  as a loop over `next_action` with fresh sessions, verification by outputs not exit codes, and
  mechanical classification of Developer extensions.

## Acceptance tests (offline, `ScriptedRunner`)

1. **Happy path:** a fixture idea at `raw` with scripted roles reaches `validated` (synthetic
   planted-effect bars) with every artifact valid and `history` showing the right `by` at each
   step.
2. **Resume after a crash:** the runner raises after the Researcher step; a second
   `pipeline run` repeats nothing that completed and finishes. Same final files as the happy path.
3. **Verification, not trust:** a runner that exits cleanly but writes an invalid spec is retried
   twice with the problem list, then the idea parks with a `driver_stalled` approval.
4. **Permissions through the driver:** the scripted Quant trying to write `validation.json`
   raises `ArtifactPermission`; after the Validator writes it, the file is read-only on disk.
5. **Plan discipline:** a plan with all variants on one axis is refused before the Validator; a run
   of an unplanned variant is refused; the `max_clusters + 1`th cluster is refused.
6. **Validation fill:** unmeasured axes appear as `ran: false` with their command; the
   recommendation is `reject` for the planted bogus idea and `ready_for_unlock` for the planted
   real one; a Validator verdict more permissive than the recommendation is refused.
7. **Advisor estimate:** arithmetic on a fixture of observability rows (signals × tokens × price,
   minus cached); unpriced model blocks approval; a run past `high` stops and re-parks; a different
   model is refused.
8. **Developer flow:** an additive diff yields `dev_merge`; a diff touching an engine path yields
   `engine_semantics` regardless of the manifest; approve re-runs gates and merges locally (against
   a temp git repo); a failing gate refuses the merge; no step ever pushes; the 4th extension is
   refused.
9. **Limits:** `--max-steps` stops resumably; a token-limit result from the runner writes `paused`
   and leaves files intact; `--dry-run` runs no role and changes nothing.
10. **Parked approvals do not block:** with a pending approval on the idea the driver exits `0`
    naming it; answering resumes the same run.
11. `tests/test_public_api.py` passes; strict JSON everywhere.

## Order of work

0. Spikes: headless invocation, token-limit behaviour, F13. Write the answers into ADR-0018.
1. `driver.py` with `RoleRunner`, `ScriptedRunner`, the loop, transition steps, the park check and
   `run.jsonl`. Tests 1, 2, 10.
2. Verification and retries, stop rules, `--dry-run`, `--max-steps`, token pause. Tests 3, 9.
3. Role definitions, `ClaudeCodeRunner`, `HERMES_ROLE`/`HERMES_IDEA` per subprocess, read-only
   files after the Validator. Test 4.
4. Plan discipline in `hermes run --variant` and `review_plan`. Test 5.
5. `validation_fill.py` and `hermes pipeline validate`. Test 6.
6. `advisor_estimate.py`, the approval estimate, the `--advisor-approval` gate. Test 7.
7. `dev_flow.py`: worktree, manifest, classification, the merge on approval. Test 8.
8. Skills, `CONTEXT.md`, `docs/MANUAL.md`, `CLAUDE.md`, `pipeline-design.md`, ADR-0018.

## Decisions (confirmed by the owner, 2026-11-17)

1. **Fresh session per step, state on disk**, rather than one long agent conversation per idea.
   Costs a re-read of the context pack per step, buys crash-resume and bounded context.
2. **The driver verifies outputs, not exit codes.** An agent that says "done" is checked against
   `produces` and the validators.
3. **`unlock_holdout` is automatic** when the Validator's verdict is `ready_for_unlock` and the
   gate passes. It is not on the design's approval list, it is one-shot per idea and recorded,
   and parking it would stall every successful idea. The owner still decides the promotion to
   `ready_to_forward_test`. (`next_action` currently labels it `owner`; change it to the driver.)
4. **The Validator's recommendation is computed and can only be tightened**, not loosened, by the
   agent.
5. **Developer classification is mechanical** (engine paths in the diff), not self-declared.
6. **Merge happens on approval, from the CLI**, after re-running both gates; the agent never merges.
7. **Role definitions live in `src/hermes/research/roles/`**, versioned with the code, not in the
   user's `~/.claude`.
8. **One idea per invocation** in this phase; the per-run idea cap is a phase 6 concern.

## Open questions

- What does the headless CLI actually do at the token limit, and does it report a reset time?
  (spike 2; the design assumed "park as resumable".)
- Should the Researcher's literature step be allowed unrestricted web access, or limited to a
  list of domains? Proposed: unrestricted fetch, but every link is recorded in `links` and the
  Validator reads the claim, not the source's authority.
- `priority` stays an integer (phase 3); ranking by expected value waits for phase 6.
- Whether the Validator should be a different *model family* from the Quant as well as a
  different session. Proposed: same Opus/Sonnet split as agreed; revisit if the planted-answer
  tests show correlated mistakes.

## As built (deviations and open items)

- **Spikes** are recorded in ADR-0018. The headless flags exist as planned; there is **no per-run
  turn cap**, so bounds are the step/attempt limits and `HERMES_ROLE_TIMEOUT`. The token-limit
  behaviour **could not be provoked**: detection is by message, a miss is a failed step. F13 was
  reproduced and fixed (integer parameters stay integer in the walk-forward grid).
- **`next_action`** gained: `driver` as a role for transition steps (`reject_idea`,
  `unlock_holdout`, `resume_from_infra`), a plan-not-locked check in `testing` (an amended plan is
  reviewed before any run), and a send-back loop (a plan with Validator objections goes to the
  Quant, not back to review).
- **The Validator's `review_plan`** can approve *or* write objections; both verify. The mechanical
  refusal is `validate_plan` (now including "no more than half the variants on one axis"), applied
  by the driver before the Validator is invoked and again by `approve_plan`.
- **Validation** got `recommendation`, `recommendation_reason`, `candidate_run`, `rationale`,
  `override_reason`, `failure` and `plan_check`. The store enforces "verdict no more permissive than
  the recommendation" and restores the computed fields from the filled file. The recommendation is
  `reject` (holdout failed, no positive cluster, or the cluster budget spent with the gate
  failing), `continue` (an axis not measured, gate failing with budget left, undeclared reads) or
  `ready_for_unlock`.
- **Sealing** is a read-only file mode *plus* a store check (an atomic replace ignores the mode).
- **Advisor estimate**: the signal count comes from an advisor-off run's trades (or `--signals`);
  tokens per call from ledger `llm_log` history, else given by the caller (`source: given`). The
  **5-call live sample is not implemented**. `--advisor-approval` is enforced for runs with an idea
  or a role; a human run with neither is unchanged. Spend is checked *before* a run, so one run can
  overshoot the high figure; the next one parks a new approval.
- **Developer flow**: files touched and classification come from `git diff base...branch`; the
  Developer only records `adds`/`tests` (`hermes pipeline extension`). Test counts and ruff output on
  the approval are the gate output captured at submit. `ENGINE_PATHS` also lists the pipeline's own
  guard modules. Worktrees live in `../.hermes_worktrees/<repo>/`. The merge is wired into
  `hermes pipeline answer`, so only the CLI merges.
- **Acceptance tests** map to: 1/2/3/9/10 `tests/test_driver.py`; 4/5 `tests/test_driver_guards.py`,
  `tests/test_validation_guard.py`, `tests/test_variants.py`; 6 `tests/test_validation_fill.py`;
  7 `tests/test_advisor_estimate.py`, `tests/test_pipeline_driver_cli.py`; 8 `tests/test_dev_flow.py`,
  `tests/test_driver_devflow.py`. The "planted real / planted bogus idea" end-to-end through real
  backtests is phase 5; here `ready_for_unlock` and `reject` are exercised on fixture analyses.
- **Not done / open**: the real `claude` runner has never been run in anger (only its command line
  and result parsing are tested); `hermes-research`'s holdout was already 20%; the Researcher's
  unrestricted web access is as proposed (links are recorded, not restricted); queue selection and
  the Strategist remain phase 6.

