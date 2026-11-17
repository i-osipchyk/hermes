# Idea transitions are guarded in the library, with no force

Status: accepted (phase 3, 2026-11). Builds on ADR-0012 (validated is earned) and ADR-0014.

## Problem

ADR-0012 says `validated` is earned on the holdout, but the check lived in `hermes ideas set`.
Python code, and an agent with a shell, could set any status through `IdeaBook.update`. The same
goes for every rule the pipeline needs: a spec before `quantified`, a reviewed plan before
`testing`, a failure on `rejected`, an owner's approval before a forward test.

## Decision

- The status graph (`TRANSITIONS`) and the rules attached to its edges live in `IdeaBook`.
  `update` raises `IllegalTransition` and writes nothing; the CLI only calls it.
- **There is no `--force`.** An agent with a shell would use it. The single way out of `rejected`
  is `reopen(id, reason)`, which records the reason. Resuming from `parked` or
  `awaiting_approval` is not re-judged, so records that predate the rules keep working.
- Every status change is appended to `history` with `by` = `HERMES_ROLE` or `owner`. As with the
  artifact roles, an env var cannot stop a determined agent; `history` makes a bypass visible.
- `ready_to_forward_test` is settable only with an approved `forward_test` approval object passed
  to `update`, which only `ApprovalBook.answer` (owner only) does.
- Reflection is the presence of `lessons.json`, not a status, so `validated`/`rejected` stay what
  `hit_rate` and `is_open` read.

## Consequences

- Old shortcuts (`raw → validated`, `quantified → testing`) are gone; skills and the manual say so.
- Ideas validated before the holdout existed keep `validated` with
  `validation_basis: "none (pre-holdout)"`, reported separately by `hit_rate`.
- Calls that edit `ideas.json` by hand are not re-judged on load; the guard is on writes through
  the library.
