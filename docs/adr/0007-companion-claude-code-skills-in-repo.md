# Companion Claude Code skills shipped in-repo

Hermes ships a set of Claude Code skills under `.claude/skills/`, committed and versioned
with the library, rather than living in a user's global config or a separate distributable
plugin. Because they are **project skills**, they load only when the Hermes repo is open —
so their context cost is paid exactly when relevant and never elsewhere.

The set is `ask-hermes` (a user-invoked router), `hermes-strategy` (a user-invoked
interview that writes `strategies/<name>.py`), and four model-invoked utilities
(`hermes-explore-data`, `hermes-backtest`, `hermes-analyze-results`, `hermes-extend`).
Invocation is **hybrid**: the long interactive flows are hand-invoked so they never fire by
accident; the quick utilities are model-invoked so Claude reaches for them in normal
conversation.

Skills carry only *process* (interview question sets, the analysis rubric, the extension
pattern) inline and **point at the repo's living docs** (`CONTEXT.md`, `examples/`, ADRs,
the public API) for anything API-specific — a single source of truth, so the skills don't
rot as the library changes.

## Consequences
- `.gitignore` excludes only `.claude/settings.local.json`, not the whole `.claude/`
  directory, so the committed skills travel with the repo while local settings don't.
- Naming uses a `hermes-` namespace (plus `ask-hermes`) so the set groups together and
  doesn't collide with a user's global skills.
