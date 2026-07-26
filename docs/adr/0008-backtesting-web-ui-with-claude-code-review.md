# Backtesting web UI with a Claude Code–driven review

A local, single-user **Streamlit** app (`hermes-ui`, code in `src/hermes/webui/`, deps
behind a `[ui]` extra) that **runs and displays** backtests in-process — it imports
`hermes` directly, with no API layer, auth, or database. It discovers strategies in
`strategies/*.py` by convention (each file exposes a `Strategy` + a default backtest
config), pre-fills an editable run form, executes the `Backtest`, and renders an
interactive equity curve (+ drawdown), metric cards, a trades table, and a review panel.

## The review runs through Claude Code, not the Claude API

The "Claude review" is an AI **diagnosis of the backtest** (is the edge real, biggest
risks, overfitting/look-ahead smells, what to try next). Rather than calling the Anthropic
API from the app (a billed request needing `ANTHROPIC_API_KEY`), the app **drives Claude
Code headlessly** — `claude -p` run in the repo directory so the existing
`hermes-analyze-results` skill loads — reusing the user's subscription auth and the skill
as the single source of truth for the rubric. This supersedes an earlier plan for a
`hermes.ai.review_backtest()` API call.

Mechanics:
- The app serializes the result to `.hermes_cache/reviews/<run_id>/result.json` and
  launches the review as a **background** subprocess that writes
  `.hermes_cache/reviews/<run_id>/review.md`.
- The page is **file-driven**: on refresh it renders `review.md` if present, else shows
  status (idle/running). Review is triggered by a button on any run and **auto-launched
  for AI-generated strategies** (`hermes-strategy` stamps a provenance marker).
- The headless session is **scoped** — `allowedTools = Read/Glob/Write`, no Bash — so it
  reasons over the serialized result + strategy file and writes the verdict; fast, safe,
  no surprise commands.
- **Manual fallback**: the page always shows the exact command + prompt to run in an
  interactive Claude Code session, producing the same `review.md` the page then displays.

## Consequences
- No `ANTHROPIC_API_KEY` needed for reviews; the `[ai]` extra remains only for the
  in-strategy AI Advisor.
- `hermes-strategy` must emit the discoverable `build_backtest()` + provenance marker.
- `BacktestResult` needs a JSON serialization helper (reusable beyond the UI).
- Review quality depends on Claude Code being installed and authenticated; the manual
  fallback and file-driven display keep the page working regardless.
