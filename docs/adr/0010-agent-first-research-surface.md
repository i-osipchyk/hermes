# Agent-first research surface

Hermes is being re-aimed. Its primary operator is an **AI research agent** that is told a
trading idea, interrogates it in conversation, then works as a quant researcher — writing
strategies, running backtests, reading the results, and iterating — with the human supervising
rather than driving each step.

The library was already well-shaped for this: a run is a pure function of (Parameters, data,
config), the vocabulary is written down in `CONTEXT.md`, and the honest-backtest rules are in
ADRs 0002/0004 rather than in someone's head. The gap was never the engine. It was that **the
surfaces an agent reads and calls had drifted from the code**, and that several capabilities
existed only inside the Streamlit layer.

## What this changes about how we treat the repo

**The docs are load-bearing, not commentary.** `CONTEXT.md`, the ADRs, and
`src/hermes/__init__.py` are what an agent uses to decide what Hermes *can do*. When they lag,
the agent doesn't degrade gracefully — it confidently reports a capability as missing and
hand-rolls a worse version. Concretely, before this: `CONTEXT.md` still said Optimization was
"deferred from v1" while `WalkForward` had shipped, and 45 public names — including `CostModel`
and every research primitive — were absent from the top-level `__all__` that the
`hermes-strategy` skill instructs the agent to verify against. Keeping these in step is now
part of the definition of done for a feature, and `tests/test_public_api.py` enforces the
export half mechanically.

**Silent wrongness is the expensive failure mode.** A human running one backtest in the UI
notices an odd number. An agent running a hundred unattended does not. So anything that can be
quietly wrong must be made loud:
- unknown LLM model ids now return `None` from the pricing table rather than matching a bare
  `claude-*` prefix and billing at another generation's rates (which overstated Opus-tier cost
  3x);
- a [[fail-open]] advisor decision is counted in `llm_summary.fail_open_calls`, so a run whose
  gate errored is distinguishable from a genuinely gated one;
- every live advisor call is logged even when the provider reports no token usage, instead of
  being dropped from the log entirely;
- one unimportable `strategies/*.py` no longer aborts discovery of all the others — an agent
  writing strategies *will* produce a broken file, and the failure must be attributable.

**One cache root.** Everything Hermes caches lives under `.hermes_cache/`, so there is a single
directory to inspect, clear, or reason about. The point-in-time enricher cache moved from
`.cache/pit/` to `.hermes_cache/pit/` accordingly.

## The structure that came out of it

**A new seam, `hermes/research/`.** The UI-free machinery that was stuck inside `webui/` is
library surface: `discovery` (what can I run?), `ledger` (what have I run?), `analysis` (is
it any good?), `review` (say so in words), plus the `sources` registry and `universes`.
`hermes.webui` keeps re-export shims so `from hermes.webui import discovery` and
`from hermes.webui import run_cache` still resolve; the Streamlit app is now one front-end
over these, not their owner.

**The ledger is the agent's memory.** `RunLedger.find()` answers "have I already tried
this?" and `entries()` answers "what have I tried?" — the two questions that separate a
research loop from an amnesiac one. The key hashes the strategy's *file contents* alongside
symbol, window, parameters and sizer, so an identical configuration is free to repeat and an
edited strategy can never be served a result its current code would not produce.

**`hermes` is a real CLI.** One JSON document on stdout under `--json`, progress on stderr,
and exit codes that distinguish *worked* (0) from *worked but produced nothing usable* (2)
from *bad request* (1) — so a caller can branch without parsing prose. A zero-trade backtest
is the case that matters: it is not an error, but it is not an answer either.

**The rubric is computed, not narrated.** `hermes analyze` runs eight axes — P&L
concentration, cost sensitivity, in/out-of-sample, statistical validation, regime split,
whether the AI gate was in effect, plus walk-forward and random-baseline on request — and
writes `analysis.json`. Each axis carries a one-line `note` and its `data`. An axis that
cannot run records `ran: false` and why, because a partial analysis is worth far more than an
exception, and because "not measured" and "clean" must never be confusable.

The division of labour is the load-bearing decision here: **Hermes computes the numbers, the
reviewer interprets them.** `hermes review` therefore still runs with
`--allowedTools Read Glob Write` — no Bash. Granting a detached background LLM shell access
to recompute what Hermes already computes deterministically would be slower, unauditable,
non-reproducible, and a real escalation of what an unattended job can do to the repo. Instead
the prompt hands over the evidence; when no `analysis.json` is attached it states plainly
that the axes are unmeasured and names the command that would measure them, so a review can
never quietly imply a check it did not make.

Two bugs surfaced while wiring this, both of the silent kind the ADR is about: the
advisor's observability log dropped **every** call from any provider that reported no token
usage (so `total_calls` and `approval_rate` read as though the gate never ran), and two
analysis notes read "holds up" / "profit in both directions" for runs that were losing money
in both halves.

## What is still to do

**Universe runs are not in the CLI.** `hermes run` covers single-symbol backtests;
`UniverseBacktest` and `PortfolioBacktest` remain library- or UI-only. Discovery recognises
`build_backtest` but not `build_universe_backtest`, so the two run shapes still need one
enumeration mechanism.

## Consequences

- New research capability goes in `hermes/research/` and should be reachable from the CLI.
  `webui/` is a presentation layer.
- `mypy --strict` over `src/hermes` is the stated target but is **not** green (~340
  pre-existing errors; `webui` is exempt). The authoritative gates are `pytest` and
  `ruff check .`, which are. Claiming a gate that nobody can pass trains an agent to
  ignore gates, so the discrepancy is recorded here and in `CLAUDE.md` rather than hidden.
- Provider-facing configuration is namespaced `HERMES_*` (`HERMES_AI_MODEL`,
  `HERMES_AI_MAX_TOKENS`, `HERMES_EDGAR_CHARS_PER_SECTION`, `HERMES_NEWS_COUNT`). Bare `MODEL`
  and `MAX_TOKENS` were collision-prone and, being read at import time, could not be overridden
  after `import hermes`. Third-party credential names (`POLYGON_API_KEY`, `CTRADER_*`,
  `TIINGO_API_KEY`, `EDGAR_CONTACT_EMAIL`) keep their conventional spelling.
