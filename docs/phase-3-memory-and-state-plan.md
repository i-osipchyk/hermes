# Phase 3 — memory and state: implementation plan

Status: **implemented** (steps 1–10, branch `feat/phase-3-memory-and-state`); see "As built" at
the end for where the code differs from this plan. Decisions and open questions were confirmed by
the owner (2026-11-17). Read [pipeline-design.md](pipeline-design.md) first (the roles, the state
machine in outline, the approvals rule), then [ADR-0014](adr/0014-research-pipeline-roles-state-and-approvals.md).
Phases 1 and 2 are on `master`; this phase builds on `holdout.json`, `trials.json`, `informed_by`
and the guarded `validated` that phase 2 added.

Rules for the session that implements this: TDD (`tdd` skill), tests offline in `tmp_path`, both
gates green, new public names in the sub-package `__all__` **and** `src/hermes/__init__.py`, new
capability in `hermes/research/` and reachable from the CLI, not in `webui/`. Every file written
goes through `research/jsonsafe.py` (strict JSON). Add new env knobs with the `HERMES_` prefix and
read them at call time.

## What this phase delivers, and what it does not

Delivers: the idea record and status model, **guarded transitions in the library**, the per-idea
artifact contract, the reads log, the approvals store, a **context pack** (everything one
iteration needs, in one command), the thesis template, and a `hermes pipeline` CLI for the
read/answer side. All of it is deterministic and offline: no agent runs, no LLM call.

Does not: the driver that runs roles (`hermes pipeline run`), the advisor cost estimate, the
queue ranking by expected value, the Strategist's writing of the thesis, or any skill rewrite
beyond the pointers listed in step 10. Those are phases 4–6. The seams they need (`next_action`,
`HERMES_ROLE`/`HERMES_IDEA`, the approval `estimate` field) are defined here so phase 4 plugs in.

## Findings from reading the code that shape the plan

- `IdeaBook.update()` accepts any status in `STATUSES`. The `validated` guard
  (`_require_passed_holdout`) lives in `cli.cmd_ideas`, so Python code can still set `validated`
  directly. ADR-0012 says validated is earned; the guard belongs in the library.
- `hermes holdout unlock` calls `book.update(status="validated"|"rejected")` itself; it must keep
  working through the new transition rules.
- Per-idea files already live at `research/ideas/<id>/{trials,holdout}.json` (`--out-dir` /
  `--holdout-dir`, default `research/ideas`). New artifacts use the same directory.
- `TrialRegistry.effective_trials` reads only the **declared** `informed_by`. ADR-0013 says the
  union of declared and audited; the reads log (step 5) is what makes the audit possible.
- `Idea.from_dict` already ignores unknown keys, so records written by an older version load.
  Additive fields are safe; renaming is not.

## 1. Status model and transitions — `research/ideas.py`

### Reconciling the design's names with the existing ones

`pipeline-design.md` listed `specified` and `backtested`; the code already has `quantified` and
`testing` with the same meaning, and `hit_rate`/`is_open`/the skills use them. **Keep the existing
names**, add only what is new, and treat `reflected` as a fact, not a status.

| Status | Meaning | Set by |
|---|---|---|
| `raw` | captured | anyone |
| `literature_done` *(new)* | Researcher has the sources and the falsifiable claim | Researcher |
| `quantified` | `spec.json` exists: unambiguous rules | Researcher / Quant |
| `coded` *(new)* | strategy file exists and runs | Quant |
| `testing` | trials exist, no verdict | Quant |
| `validated` | **only** by a passed holdout unlock (ADR-0012) | `hermes holdout unlock` |
| `rejected` | tested and did not survive, or a duplicate | Validator / owner |
| `needs_infra` *(new)* | blocked on a Developer extension | Quant |
| `awaiting_approval` *(new)* | parked on an owner decision; `resume_status` + `approval_id` recorded | any role |
| `ready_to_forward_test` *(new)* | validated **and** the owner approved the suggestion | approval answer only |
| `parked` | blocked externally (existing) | any role |

**Reflected is not a status.** `validated`/`rejected` must stay what `hit_rate` and `is_open` read.
An idea is *reflected* when `lessons.json` exists; "decided but not reflected" is a queue query.

### Legal transitions (everything else raises `IllegalTransition`)

```
raw             → literature_done | quantified | parked | rejected
literature_done → quantified | parked | rejected
quantified      → coded | needs_infra | parked | rejected
needs_infra     → quantified | coded | parked | rejected     (after the Developer branch merges)
coded           → testing | needs_infra | awaiting_approval | parked | rejected
testing         → validated* | rejected | awaiting_approval | needs_infra | parked
awaiting_approval → <resume_status> | rejected | parked       (via the approval answer)
validated       → ready_to_forward_test†
parked          → <any earlier status the idea had>
rejected        → (terminal; `hermes pipeline reopen <id> --reason …` → quantified)
* only through `unlock` (checks `holdout.json` state=done, passed=true)
† only through an approval answer of kind `forward_test`
```

- Any status may go to `awaiting_approval` or `parked` from a non-terminal state.
- **No `--force`.** An agent with a shell would use it. The one escape is `reopen`, which needs a
  reason and is recorded.
- The `validated` guard moves from the CLI into the library; the CLI calls the same function.

### New fields (additive, defaulted so old records load)

`schema_version: int = 2`, `parent: str | None`, `failure: dict | None`
(`{kind, assumption, detail}`, `kind` from a closed vocabulary: `no_edge`, `cost_killed`,
`regime_dependent`, `overfit`, `data_limit`, `holdout_failed`, `duplicate`, `infra`, `other`),
`links: list[str]` (source URLs), `resume_status: str | None`, `approval_id: str | None`,
`priority: int | None` (manual order; lower runs first), `validation_basis: str | None`
(`"holdout"` or `"none (pre-holdout)"`), `history: list[dict]`.

`history` is appended on every status change: `{at, from, to, by, reason}`, where `by` is
`HERMES_ROLE` or `"owner"`. It is the audit trail the Validator and the owner read.

### Rules attached to transitions

- `literature_done` needs ≥ 1 entry in `links` unless `source` is `discretionary` or `data`
  (the design's "every idea needs a source link").
- `quantified` needs a valid `spec.json` (step 4).
- `testing` needs a locked, Validator-approved `plan.json`, so no variant runs unplanned.
- `rejected` needs a `failure` (kind + assumption) and a `verdict`.
- `needs_infra` needs a note naming the missing capability (goes in `failure.detail`).

### Dedupe

`ideas.similar(text, k=5)` → candidates by token-overlap over title + hypothesis + tags (no LLM:
Hermes computes, the agent interprets). `hermes ideas add` prints the matches and records them in
`similar_to`; a near-exact match (≥ 0.8) with a `rejected` idea is refused unless `--parent <id>`
is given. Thresholds are starting values.

## 2. The context pack — `research/context.py` *(the owner's "sufficient context" requirement)*

`hermes pipeline context <id> [--json] [--max-chars N]` prints everything one iteration needs to
understand what was tested, deterministically and capped in size:

1. the idea record (status, failure, links, parent, priority, `history` tail);
2. `spec.json` (claim, mechanism, rules, invented numbers, falsifier);
3. the plan: each variant, its axis, and whether it ran (joined to ledger keys and `--note`s);
4. trial clusters with their representative metrics, `N_eff` and where each count came from;
5. `validation.json` (read-only view) and `holdout.json` state;
6. `lessons.json` of the **parent** and of every `informed_by` idea (what they learned);
7. an excerpt of `research/thesis.md` (headings relevant to the idea's tags);
8. open approvals for the idea.

It is the only thing a fresh session needs at start; it replaces reading all idea records.
Reading it for another idea is logged as a read of that idea (step 5).

## 3. Per-idea artifacts — `research/artifacts.py`

Directory `research/ideas/<id>/`, committed. One typed dataclass per artifact, a `schema_version`,
atomic write (temp file then rename), strict JSON.

| File | Written by | Contents |
|---|---|---|
| `spec.json` | Researcher, Quant | claim, mechanism, instruments, timeframes, data needs, entry/exit/sizing rules, declared `Parameter`s (target ≤ 3), every invented number named, a look-ahead statement, expected sign and size, **falsifier**, infra needs, strategy path |
| `plan.json` | Quant drafts, Validator reviews | variants `{id, axis, change, tests, decision_rule}`, budget `{max_clusters, max_extensions: 3}`, `review {approved, objections, reviewer}`, `locked_hash` set on approval |
| `trials.json` | CLI (`hermes trials`) | existing |
| `holdout.json` | CLI (`holdout unlock`) | existing |
| `validation.json` | **Validator only** | per-axis pointers into `analysis.json`, tier, `N_eff`, DSR, null p, holdout state, verdict (`reject`/`continue`/`ready_for_unlock`/`validated`), objections, **axes not measured with the command that would measure them** |
| `lessons.json` | Researcher (reflection) | what was tested (cluster ids, run keys), outcome, `failure`, what would change my mind, `next_hypotheses [{title, hypothesis, informed_by}]`, infra gaps, surprises |
| `reads.jsonl` | CLI | the reads log (step 5) |

Rules:
- **Role permissions.** `ArtifactStore.write(idea, name, data, role=…)` checks the role against the
  table; the role comes from `HERMES_ROLE`. `validation.json` is read-only for the Quant: any
  other role raises `ArtifactPermission`.
- **Plan lock.** After the Validator approves `plan.json` its hash is stored; a later edit to the
  variants raises `PlanLocked`. A new variant is a plan amendment that needs review again.
- **Variants reference the plan.** `hermes run --idea <id> --variant <plan variant id>`; the id is
  recorded in `RunMeta` (not in the cache key). `hermes trials --plan` lists planned-but-unrun
  and run-but-unplanned variants.
- **Honest limit** (same as ADR-0012): the role is an env var, so a determined agent with a shell
  can bypass it. The driver (phase 4) launches each role as a subprocess with its own
  `HERMES_ROLE`, the files are made read-only after the Validator writes them, and `history`
  records every status change with its `by`, so a bypass is visible after the fact, not prevented.

## 4. Spec and plan validators — `research/artifacts.py`

`validate_spec(spec)` and `validate_plan(plan)` return a list of problems (empty = valid): every
number named, ≤ 3 parameters unless justified, a falsifier present, each variant has an axis from
the closed vocabulary (`entry`, `exit`, `regime_filter`, `instrument`, `timeframe`, `horizon`) and
a parameter nudge is rejected as an axis, a decision rule per variant. These are structural checks
only; whether the plan is *good* is the Validator's judgement.

## 5. Reads log and the `informed_by` audit — `research/reads.py`

- When `HERMES_IDEA` is set, `hermes show`, `analyze`, `ideas` (single idea), `trials` and
  `pipeline context` append to `research/ideas/<HERMES_IDEA>/reads.jsonl`:
  `{at, role, command, target, target_idea}` (`target_idea` resolved from the ledger's
  `meta.idea` or the idea id).
- **Targeted reads count** (a run or idea that belongs to another idea). **Listings** (`hermes
  runs`, `ideas` without an id) are logged as `kind: listing` and reported to the Validator, but
  are not counted automatically, otherwise looking at the table of contents would deflate
  everything. Recommended; revisit if agents learn to read results through listings.
- `audit_informed_by(idea)` returns `{declared, read, read_not_declared, declared_not_read}`.
- **`TrialRegistry.effective_trials` takes the union** of declared and read-and-targeted ideas
  (ADR-0013), excluding the idea itself; `read_not_declared` is also surfaced in the `deflation`
  axis note and in `validation.json` as a flag for the Validator.

## 6. Approvals — `research/approvals.py`

`research/approvals.json`, committed. `Approval`: `id`, `idea`, `kind`
(`advisor` | `instrument_change` | `engine_semantics` | `dev_merge` | `forward_test` | `other`),
`question`, `recommendation`, `evidence` (paths), `estimate` (dict or `None`; phase 4 fills it for
`advisor`), `branch` (for `dev_merge`), `status` (`pending` | `approved` | `denied`),
`created_at`, `answered_at`, `answered_by`, `note`.

- `ApprovalBook.park(idea, kind, question, recommendation, …)`: creates the approval, sets the idea
  to `awaiting_approval` with `resume_status` + `approval_id` (one library call, one history entry).
- `answer(id, decision, note)`: `approved` → idea returns to `resume_status` (or goes to
  `ready_to_forward_test` for `forward_test`); `denied` → idea goes to `parked` or `rejected` as the
  answer says. Not answerable while `HERMES_ROLE` is a role name (only unset or `owner`).
- CLI: `hermes pipeline approvals [--pending] [--json]`, `hermes pipeline answer <id>
  approve|deny [--note …]`. The listing shows question, recommendation, evidence and estimate, so a
  batch answer is a read-through.

## 7. Thesis template — `research/thesis.py` and `research/thesis.md`

Fixed headings so it can be read as context and checked: *Survived* (id, tier, `N_eff`, DSR,
holdout, one line), *Failed* (grouped by `failure.kind`), *Recurring mechanisms and regimes*,
*Open questions*, *Next hypotheses* (each with `informed_by`). Front-matter: `updated`,
`ideas_included`. Only the Strategist writes it (phase 6); this phase ships the template, a reader
for a heading's text, and `due(book, threshold=5)`: true when five or more ideas have a
`lessons.json` newer than `updated`, or the queue is empty.

## 8. `next_action` — the seam for phase 4

`pipeline.next_action(idea) -> Action(role, step, requires, produces)`: a pure function from
status and artifacts on disk to the next step. Examples: `quantified` without `plan.json` →
Quant writes the plan; plan unreviewed → Validator reviews; plan locked, variants unrun → Quant
runs them; trials but no `validation.json` → Validator; `validation.json` says
`ready_for_unlock` → owner-gated or automatic unlock; decided with no `lessons.json` →
Researcher reflects. Resume is "call `next_action` again", which is why a crash costs nothing.
`hermes pipeline status [--json]` prints the backlog by status, pending approvals, ideas awaiting
reflection, whether the thesis is due, and each open idea's `next_action`.

## 9. Backfill and data fixes

- Old records load unchanged; new fields default. `history` stays empty until an idea is next
  touched; nothing rewrites the 29 existing ideas.
- The two `validated` ideas (`gold_tokyo_breakout_v2_flat`, `dip_buy_sp500`) get
  `validation_basis: "none (pre-holdout)"` as ADR-0012 promised. `hit_rate` reports them separately
  (`validated_without_holdout`).
- **Not part of this phase but flagged:** their old run keys were retired by the engine
  fingerprint (audit F2); re-run both under the current engine before anyone cites them.

## Acceptance tests (offline)

1. **Transitions are exhaustive:** parametrised over every (from, to): each legal edge passes, each
   other raises `IllegalTransition`, and `history` records `from/to/by/reason`.
2. **`validated` cannot be set in the library** without a passed `holdout.json`, including through
   `IdeaBook.update`; `unlock` still sets it.
3. **`ready_to_forward_test`** is reachable only through a `forward_test` approval answer.
4. **Source link rule:** `literature_done` without a link is refused unless the source is
   `discretionary`/`data`.
5. **Artifacts:** schema round-trip; old-version file loads; a write from the wrong role raises
   `ArtifactPermission`; editing a locked plan raises `PlanLocked`; spec/plan validators reject a
   parameter-nudge axis and an unnamed invented number.
6. **Reads log:** a targeted read of another idea's run appends a line and shows in
   `audit_informed_by` as `read_not_declared`; a listing is logged but not counted;
   `effective_trials` equals own + declared ∪ read.
7. **Approvals:** park → pending → approve resumes the idea at `resume_status`; deny parks or
   rejects; an agent role cannot answer.
8. **Context pack:** deterministic (same input, same output), respects `--max-chars`, includes the
   parent's and `informed_by` ideas' lessons, and logs the read.
9. **`next_action`** returns the expected step for a fixture idea at each status; calling it twice
   changes nothing; killing the process between two steps and calling it again resumes.
10. **Tolerant load:** a snapshot of today's `research/ideas.json` (29 ideas) loads and lists with
    no change; strict JSON everywhere (`allow_nan=False`).
11. `tests/test_public_api.py` passes with the new names.

## Order of work

1. Status model, `IllegalTransition`, `history`, new fields, the library-level `validated` guard,
   `unlock` and the CLI moved onto it. Tests 1–4.
2. Dedupe (`similar`) and `hermes ideas add` output.
3. `artifacts.py` (stores, role permissions, plan lock, validators). Test 5.
4. `reads.py`, the CLI hooks, `audit_informed_by`, the union in `effective_trials`. Test 6.
5. `approvals.py` and `park`/`answer`. Test 7.
6. `context.py` and `hermes pipeline context`. Test 8.
7. `thesis.py` + template, `next_action`, `hermes pipeline status`. Tests 9–10.
8. `hermes pipeline` CLI wiring (`status`, `context`, `approvals`, `answer`, `reopen`); public API.
9. Backfill the two validated ideas; `hit_rate` split.
10. Docs: `CONTEXT.md` terms (**Artifact**, **Approval**, **Park**, **Context Pack**, **Reads
    Log**, **Thesis**), `docs/MANUAL.md`, `hermes-ideas` skill (new statuses, links, dedupe),
    `pipeline-design.md` phase status, and a short **ADR-0017** recording the move of the transition
    guard into the library and the removal of any `--force`.

## Decisions (confirmed by the owner, 2026-11-17)

1. Keep `quantified` and `testing`; do not introduce `specified`/`backtested` (pipeline-design.md
   is updated to match).
2. `reflected` is the presence of `lessons.json`, not a status.
3. The Researcher writes the per-idea reflection; the Strategist aggregates it into the thesis.
4. No `--force` on transitions; `reopen` with a recorded reason is the only way out of `rejected`.
5. Listings in the reads log are reported, not counted.
6. Near-exact duplicate of a rejected idea is refused without `--parent` (threshold 0.8, to
   calibrate on the real backlog).

## Open questions — resolved (owner: "go with the recommendations")

- `priority` is a plain integer on the idea record for now; revisit at phase 6.
- A denied `advisor` approval sends the idea back to its previous status with a note, so the
  pipeline keeps moving without the advisor. It does not park the idea.

## As built (deviations and open items)

- **Dedupe thresholds changed.** On the real backlog the closest sibling pair scores 0.28 Jaccard
  and nothing reaches 0.5, so 0.8 would never fire. Built: Jaccard over stopword-filtered title +
  hypothesis + tags, **warn ≥ 0.25, refuse ≥ 0.6** against a *rejected* idea unless `--parent`.
  The refuse value is untested on a real duplicate.
- **No `earned` token for `unlock`.** `unlock` writes `holdout.json` before it sets the status, so
  the ordinary guard covers it. It now refuses an idea that is not `testing` *before* spending the
  look.
- **`ideas_dir`** is a read-only alias of `IdeaBook.holdout_dir`; the CLI flag is `--ideas-dir`
  (`--holdout-dir` still works).
- **Resuming is not re-judged**: leaving `parked` or `awaiting_approval` skips the artifact and
  holdout rules, so legacy ideas (including the two validated ones) survive a park/deny cycle.
- **Transitions into `quantified`/`testing` need artifacts only for new moves**; `hermes run` does
  not require a spec or plan, only `--variant` does.
- **Reads log**: location is `HERMES_IDEAS_DIR` (default `research/ideas`) for every command, not
  `--out-dir`. There is no single-idea read command except `pipeline context`. It logs the Hermes
  CLI only; an agent reading the ledger or `research/output` directly is not seen.
- **Added to the plan**: `hermes run --variant` (`RunMeta.variant`, outside the cache key),
  `hermes trials --plan`, `hermes pipeline write` and `approve-plan` (so artifacts can be written
  from the CLI under a role), `Lessons.written_at`.
- **Denial defaults**: `advisor` and `forward_test` denials return the idea to where it was; other
  kinds park it, or reject it with `--reject`.
- **Not exported** in `hermes/__init__.py`: the new modules (`artifacts`, `approvals`, `reads`,
  `pipeline`, `context`, `thesis`) are internal like phase 2's, reached through the CLI.
- **Still open** (phases 4–6): the driver that launches roles with their own `HERMES_ROLE`; making
  files read-only after the Validator writes; filling `Validation` automatically and the advisor
  `estimate`; queue ranking beyond `priority`; the Strategist writing the thesis; a Claude Code
  hook narrowing what a role can read; re-running the two pre-holdout validated ideas under the
  current engine (audit F2).
