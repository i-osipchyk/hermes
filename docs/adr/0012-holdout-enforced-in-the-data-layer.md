# Holdout enforced in the data layer

Research runs cannot read the last 20% of an instrument's history (at least 6 months). The block
is in the data layer, so it holds without relying on an agent's discipline.

The `hermes-research` skill already asks for a held-out ~30% and says "do not look at it while
exploring". That is a promise, and an autonomous pipeline that generates many ideas will break a
promise it has no mechanism to keep: the cheapest way to find a number that looks good is to look.
ADR-0011 puts the rigour in "the holdout and the trial count"; this ADR makes the holdout real.

## Decision

- Per (source, symbol, timeframe), `holdout_start` = the end of the first 80% of available history,
  but never less than 6 months of holdout. It is **frozen at first research touch** in the committed
  `research/holdouts.json`, so it does not slide as data grows (new data extends the holdout).
- `HoldoutSource` wraps any `DataSource` for research use. A request whose `end` passes
  `holdout_start` raises `HoldoutViolation`. There is no clipping and no bypass flag. A research
  run's default end is `holdout_start`.
- The holdout is **unlocked once per idea**, only for an idea whose latest `validation.json`
  passed the rubric. The unlock runs the frozen strategy (file hash and params) once and records
  it in `research/ideas/<id>/holdout.json`. A second unlock is refused even if the strategy file
  changed.
- If the history is too short to carve a holdout (proposed: under 12 months left in-sample), no
  holdout is carved and the idea's evidence tier is capped at `exploratory`.
- Each unlock is recorded as a "look" on the instrument's holdout. Several ideas looking at the
  same holdout is itself multiple testing, so the holdout pass criterion is deflated by the number
  of looks (see ADR-0013).
- The two ideas already `validated` are marked "validated without holdout" and are re-tested on
  their own schedule, not quietly.

## Considered and rejected

- **Trust the skill text** (status quo): not enforceable against an unattended agent.
- **Sliding holdout** (always the last 20% of whatever exists): yesterday's holdout silently
  becomes tomorrow's in-sample, so a result can be "confirmed" on data that was already looked at.
- **Silent clipping to `holdout_start`:** the strategy would run and look fine on less data with no
  signal that it was truncated. Failing loudly is preferable.
- **Unlimited unlocks:** the holdout would become a second in-sample.

## Consequences

- Honest limit: an agent with a shell can call a source directly. The guard therefore also sits in
  `BarCache.read` when `HERMES_RESEARCH=1` (set by the pipeline driver for every agent subprocess;
  built, with the in-process `guard_suspended` for carving and the unlock);
  ledger rows with `end > holdout_start` are invalidated by `hermes analyze`; and a Claude Code
  hook denies Bash that reads the bar cache outside the CLI. This reduces the risk. It cannot
  remove it against a determined agent, and the doc says so.
- Short Pepperstone histories (~3 years) will often be capped at `exploratory`; that is the
  correct reading of the evidence, not a defect.
- Depends on audit F2 and F3: the cache key must include the holdout policy and a research run's
  default end can no longer be "now".
