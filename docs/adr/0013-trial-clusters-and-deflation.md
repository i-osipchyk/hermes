# Trial clusters and deflation of the Sharpe

A Sharpe is deflated by the number of **independent** things that were tried to find it, counted
from the ledger, including the earlier results the agent actually used.

With AI idea generation the number of ideas, and of variants per idea, grows by an order of
magnitude. A rubric that judges each idea alone gives every one of them a fresh draw. The search
can also launder trials: a "new" idea on the same data that was informed by a rejected one.

## Decision

- A **trial** is a run that is a candidate for selection. Diagnostics and sweeps are not trials
  (they explain, they do not select). `RunMeta` records `idea` and `kind: trial | diagnostic`
  without changing the cache key.
- Trials are grouped into **trial clusters**: a new trial joins an existing cluster when its
  daily-resampled return correlation is at least 0.9 or its trade overlap is at least 0.8 against
  the cluster representative. Nine near-identical variants are one trial, not nine. The thresholds
  are starting values, calibrated by the planted-answer tests.
- **Effective trials** `N_eff` = clusters of the idea + clusters of every idea in `informed_by`
  + a small batch floor that grows with the number of ideas processed in the pipeline run.
  `informed_by` is the agent's declaration, audited against a read log of what its session
  actually opened; the union is used and any mismatch goes to the Validator.
  A child idea written by Reflection inherits `parent` and `informed_by` automatically.
- If an earlier result was *not* used, it does not count. A gold trend strategy that informed a
  gold mean-reversion idea deflates it; an unrelated one does not.
- The deflated Sharpe ratio (Bailey and López de Prado) uses `N_eff` and the variance of the
  cluster representatives' Sharpes. With `N_eff = 1` it equals the probabilistic Sharpe and the
  axis says so.
- Initially undiscounted: a dependent idea inherits its sources' full cluster count. A discount
  would be a judgment call, so it waits for data.

## Considered and rejected

- **Count every run in the ledger:** punishes thorough diagnosis and rewards fewer variants,
  the opposite of the depth we want.
- **Count per idea only:** undercounts, since the agent can start a new idea on the same data.
- **Count per family (e.g. all `gold_*`) automatically:** counts studies the agent never looked at.
- **Declaration only:** gameable. **Read log only:** noisy. The union, audited, is the compromise.

## Consequences

- Prerequisite: PSR/DSR must be computed on daily-resampled or trade-level returns. Today's PSR
  treats every bar as an independent observation (audit F5), which saturates near 1 on intraday
  data and would make deflation meaningless.
- Variants must differ structurally to count as new evidence (pre-registered test plan, see
  ADR-0014); correlation clustering is what makes that checkable.
- Run keys must change when the engine does (audit F2), or `N_eff` would be computed from stale
  trials.
