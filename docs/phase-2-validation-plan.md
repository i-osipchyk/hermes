# Phase 2 — validation hardening: implementation plan

Status: **steps 1–6 implemented** (see "As built" at the end). Read [pipeline-design.md](pipeline-design.md) and
[audit-2026-11.md](audit-2026-11.md) first. Decisions: [ADR-0012](adr/0012-holdout-enforced-in-the-data-layer.md),
[ADR-0013](adr/0013-trial-clusters-and-deflation.md).

Rules for the session that implements this: TDD (`tdd` skill), tests offline with
`InMemorySource`, both gates green (`pytest`, `ruff check .`), new public names in the
sub-package `__all__` **and** `src/hermes/__init__.py`, new research capability in
`hermes/research/` and reachable from the CLI, not in `webui/`.

## Prerequisite: audit fixes this plan depends on

| Audit item | Why phase 2 needs it | Blocks |
|---|---|---|
| F2 cache key | Holdout policy, engine version and idea id must change/qualify the key, or stale runs masquerade as holdout-clean | trials, holdout |
| F3 default `end` = now | A research run's default end must become the holdout start; "now" would always violate the guard | holdout |
| F5 PSR on bars | DSR is built on PSR; must use daily-resampled (or trade-level) returns with an autocorrelation correction | deflation |
| F4 Monte Carlo | The rubric must not present degenerate end-equity stats next to DSR | review output |
| F6, F7 walk-forward | OOS Sharpe must be comparable and failures must be loud | holdout pass criterion |
| F1 ⚖ reference leak | Not blocking, but a leak would invalidate any holdout result | all |

Pieces that can start before those land: the holdout store and `HoldoutSource` wrapper, the trial
registry and clustering, the null test. Pieces that cannot: DSR (F5), cache-key changes (F2).

## Components

### 1. Holdout policy — `src/hermes/research/holdout.py`

- `HoldoutPolicy`: `holdout_start(source, symbol, timeframe)` → date or `None`.
  Rule: `span = last_bar − first_bar`; `held = max(0.20 × span, 6 months)`;
  if `span − held < MIN_IN_SAMPLE` (proposed 12 months) → `None`, and the idea is capped at
  `exploratory` with the reason recorded.
- **Frozen at first research touch** in committed `research/holdouts.json`
  (`{source/symbol/timeframe: {holdout_start, frozen_at, looks: []}}`). It never slides: new
  data after `holdout_start` simply extends the holdout.
- `HoldoutSource(DataSource)` wraps any source. `history(start, end)` with `end > holdout_start`
  raises `HoldoutViolation` (loud; no clipping, no bypass flag). A reference or conversion-pair
  feed for the same period is guarded by its own entry.
- Research runs get a guarded source; the **default end** for a research run becomes
  `holdout_start` (this also fixes audit F3).
- Enforcement points: `discovery.configured_backtest`, `universe_runs`, `WalkForward`/analysis
  re-runs (they take a template `Backtest`, so wrap its `source`), and `BarCache.read` when
  `HERMES_RESEARCH=1` is set by the pipeline driver for every agent subprocess.
- Honest limit: an agent with a shell can still call a source directly. Mitigations, in order:
  the env-flagged cache guard; ledger rows with `end > holdout_start` are invalidated by
  `hermes analyze`; a Claude Code hook denying Bash that reads `.hermes_cache/bars` outside the
  CLI.
- **Unlock:** `hermes holdout unlock <idea_id> --run-key <key>` requires the idea's latest
  `validation.json` to have passed the rubric, runs the frozen strategy (file hash + params) once
  on `[holdout_start, latest]`, writes `research/ideas/<id>/holdout.json` (burned), appends a
  "look" to the instrument's entry in `holdouts.json`. A second unlock for the same idea is refused
  even if the strategy file changed.
- **Pass criterion (proposed, calibrate):** holdout Sharpe has the same sign as the in-sample
  Sharpe, holdout PSR(0) ≥ 0.8 on daily returns, and trade count meets the exploratory floor; else
  the idea is `rejected` with reason `holdout_failed`.

### 2. Trial registry and clusters — `src/hermes/research/trials.py`

- `RunMeta` gains `idea: str | None` and `kind: "trial" | "diagnostic"` (not part of the cache
  key). `hermes run --idea <id> [--diagnostic]`. `analyze` and sweeps default to `diagnostic`.
- `research/ideas/<id>/trials.json` is derived from the ledger by `hermes trials <idea>`:
  each trial gets a cluster id.
- **Clustering:** daily-resampled equity returns, correlation ≥ `CLUSTER_CORR` (0.9) **or** trade
  overlap (shared entry day and side) ≥ 0.8 against any earlier cluster representative → same
  cluster. Greedy, deterministic order by `created_at`.
- `effective_trials(idea) = clusters(idea) + Σ clusters(i) for i in informed_by(idea) + batch_floor`.
  `informed_by` is the union of the agent's declaration and the audited read log
  (`reads.jsonl`, appended by `show`/`runs`/`ideas show` when `HERMES_IDEA` is set); a mismatch is
  flagged for the Validator. Undiscounted at first.
- Test-plan check: `hermes trials <idea> --plan` flags plans whose variants share a cluster.

### 3. Deflated Sharpe — extend `src/hermes/backtest/validation.py` and the rubric

- Compute PSR and DSR on **daily-resampled** returns (or trade-level), not bars (audit F5), with
  an effective-sample correction for autocorrelation. Keep the bar-level PSR only as a labelled
  legacy field, or remove it.
- DSR (Bailey and López de Prado): `SR0 = sqrt(V[SR_trials]) × ((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e)))`
  with `N = effective_trials` and `V` the variance of the cluster representatives' Sharpes. For
  `N_eff = 1` there is nothing to deflate and the axis says so.
- New `analyze` axes: `deflation` (DSR probability, `N_eff`, where each count came from) and `null`.
  An axis that cannot run reports `ran: false` with the command that would measure it, never
  clean (CLAUDE.md).
- Evidence tier: `validated` requires holdout passed **and** DSR ≥ 0.9 (owner's choice; calibrate with the planted-answer tests);
  no holdout carved caps the tier at `exploratory`.

### 4. Null test for rule-based strategies (audit G1) — `src/hermes/backtest/null_baseline.py`

Needed because the existing random baseline only randomises the AI veto. Proposed construction:
**circular time-shift of the strategy's entries**, preserving side, holding period and trade count,
re-priced from bars with the same cost model (next-open entry, exit after the same number of bars;
SL/TP path ignored and documented as an approximation). Report where the observed Sharpe sits in
the null distribution (p-value). Decide in the session whether to use this or a full engine re-run
with a randomised signal wrapper; the second is more faithful and slower. Whichever is chosen, a
seeded run must be deterministic.

## Acceptance tests (offline, planted answers)

1. **Bogus idea:** a random-signal strategy on synthetic bars is *not* `validated`; the null test
   p-value is not extreme and DSR is below the bar once ≥ 5 trial clusters exist.
2. **Real effect:** synthetic bars with a known conditional drift; the corresponding strategy
   passes the holdout, DSR and null axes.
3. **Holdout guard:** a research run with `end` past `holdout_start` raises `HoldoutViolation`;
   default end equals `holdout_start`; a second unlock for the same idea is refused.
4. **Clusters:** two variants differing by one parameter nudge land in one cluster; a variant with
   a different exit structure lands in another; `effective_trials` adds `informed_by` clusters.
5. **No deflation without trials:** `N_eff = 1` yields DSR = PSR and says why.
6. **Short history:** a 14-month series carves no holdout and caps the tier at `exploratory`.

## Order of work

1. `holdout.py` store + `HoldoutSource` + tests (3, 6).
2. Trial registry + clustering + `RunMeta` fields + CLI flags + tests (4).
3. Daily-resampled PSR/DSR (after audit F5) + `deflation` axis + tests (5).
4. Null test + `null` axis + tests (1, 2).
5. `hermes holdout unlock` and the evidence-tier rules.
6. Update `hermes-research` and `docs/MANUAL.md`; add terms to `CONTEXT.md`
   (**Holdout**, **Trial**, **Trial Cluster**, **Effective Trials**).

## As built (2026-11)

All six steps are in. Where the code differs from the plan above:

- **Holdout boundary.** `holdout_start` is the first held-out instant: a request with
  `end > holdout_start` raises, and a request ending exactly on it is allowed but never
  receives a bar stamped at or after it (bars are keyed by open time). Probing a history's
  extent goes up to the start of today, never a far-future date — a caching source would
  otherwise mark the empty future as checked.
- **Frozen "no holdout".** A history too short to carve is frozen as `null` too, so more data
  later does not retroactively give an idea a holdout it already saw all of.
- **Guard coverage.** `hermes run` and `hermes analyze` (name or stored key) are guarded.
  **Not yet:** universe runs, `WalkForward` templates built outside the CLI, reference feeds,
  the `BarCache.read` env guard (`HERMES_RESEARCH=1`) and the Claude Code hook.
- **PSR basis.** Trade-level (audit F5), not daily-resampled with an autocorrelation
  correction; the deflation and null axes refuse to run under 10 trades rather than fall back
  to bars.
- **Clusters.** Overlap is measured against the larger trade set (stricter). `informed_by` is
  the agent's declaration only; the audited read log (`reads.jsonl`) and `--plan` are not built.
  `informed_by` is not transitive; the batch floor is 0.
- **Null test.** Engine replay (not re-pricing from bars): one random circular offset per
  sample, stops/targets not replayed, a trade running past the end is closed there.
- **Gate and pass bar.** Gate: tier ≥ `credible`, DSR ≥ 0.9, null p < 0.05 (all must be
  measured). Holdout: same sign, ≥ 10 trades, PSR ≥ 1 − 0.2 / (1 + earlier looks). The
  "validated" tier in the plan is the idea *status*; evidence tiers are unchanged.
- **Unmeasured / open.** Universe runs cannot carry an idea, so cannot be deflated or
  unlocked. The two ideas validated before this existed are not yet labelled "validated
  without holdout". All thresholds are untuned.
