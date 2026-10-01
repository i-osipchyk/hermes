# Working in Hermes

Hermes is a bar-based backtesting framework aimed at being driven by an AI research agent
(ADR-0010). Read these three before writing code — they are source, not commentary:

| File | What it is |
|---|---|
| `docs/MANUAL.md` | The user manual — the five stages, with an agent and by hand. Where to look when you don't know the workflow. |
| `CONTEXT.md` | The ubiquitous language (~50 terms). Use these words; the code does. |
| `docs/adr/` | The load-bearing decisions. Cite them when behaviour hinges on one. |
| `src/hermes/__init__.py` | The complete public inventory (156 names). If it's in Hermes, it's importable from `hermes`. |

## Environment

Use the project venv — `.venv/bin/python`, and `.venv/bin/hermes` for the CLI. Extras
(yfinance, pyarrow, matplotlib, streamlit) are installed there and nowhere else.

## Drive the loop through the CLI

Don't write a one-off script for a question the CLI answers. `--json` gives one parseable
document on stdout (progress goes to stderr); exit `0` fine, `2` ran but produced nothing
usable, `1` bad request.

```bash
.venv/bin/hermes strategies        # what's runnable; broken files listed with their error
.venv/bin/hermes run <name> --note "why"   # run + record. --symbol --start --end -p k=v
.venv/bin/hermes runs              # the experiment log — CHECK IT BEFORE RE-RUNNING
.venv/bin/hermes runs --note-contains "gap"      # find your own past reasoning
.venv/bin/hermes show <key>        # a stored run; key prefixes work
.venv/bin/hermes analyze <key>     # the rubric, computed; writes analysis.json
.venv/bin/hermes review <key>      # a verdict over that evidence
```

Runs are input-keyed and cached, so an identical configuration is free. **Consult
`hermes runs` before running something that may already have been tried** — that ledger
is the project's memory across sessions, and it is the whole reason it exists.

**Always pass `--note`.** It records *why* a configuration was run — the hypothesis, or
what changed since the last variant. Without it `hermes runs` is a list of hashes; with
it, it is a research log you (or a future session) can actually read. The note is not
part of the cache key, because intent is not an input.

Two rules about the review:
- **Hermes computes, the reviewer interprets.** Never have an LLM calculate a rubric
  axis. `hermes analyze` produces the numbers; `hermes review` reads them. The headless
  review runs without Bash on purpose.
- **Never present an unmeasured axis as clean.** An axis with `ran: false` carries its
  reason in `error`; report it as not measured and name the command that would measure
  it.

## Gates

Before calling anything done, both of these must pass:

```bash
.venv/bin/python -m pytest          # 337 tests, offline, ~2.8s
.venv/bin/python -m ruff check .     # clean across the whole repo
```

`mypy` is configured `strict` over `src/hermes` as a **target**, and is currently **not green**
(~340 pre-existing errors; `hermes.webui.*` is exempt). Don't treat a mypy error in untouched
code as your regression, and don't start a typing campaign unless that's the task. Do keep the
file you're editing from getting worse.

Tests must stay **offline and fast**. Never add a test that hits the network — use
`InMemorySource`, or a recorded fixture, as the existing tests do.

## Conventions that bite

**Strategy files** (`strategies/*.py`) follow a discovery contract — the UI, the skills and the
`discovery` module all rely on it:

- `GENERATED_BY = "hermes-strategy"` when the skill wrote it (provenance; the UI badges it);
- `build_backtest(**overrides) -> Backtest`, constructing a **fresh** `Strategy` each call so
  re-runs are clean, and applying `overrides` (symbol/start/end/starting_cash/params);
- `if __name__ == "__main__": build_backtest().run()`;
- omit `start`/`end` unless the strategy needs a specific window — the default Trading Window
  is year-to-date.

A universe-scale run returns a `UniverseBacktest`, not a `Backtest`, so it exposes
`build_universe_backtest(**overrides)` instead (`strategies/gap_breakout_sp500.py`).

**New research capability?** It belongs in `hermes/research/`, reachable from the CLI, not
inside `webui/`. The Streamlit app is one front-end over the library, not a home for logic.

**Adding anything public** means adding it to its sub-package's `__all__` **and** to
`src/hermes/__init__.py`. `tests/test_public_api.py` fails otherwise. This isn't bookkeeping:
the `hermes-strategy` skill tells the agent to verify generated imports against that file, so a
name missing there is a capability the agent will believe doesn't exist.

**Parameter discipline.** `metrics.parameter_adjusted_sharpe` = Sharpe × √((n_trades − k)/n_trades)
where k = declared `Parameter` count. Target ≤ 3 for a first version. Prefer a hard-coded
economic constant over an extra tunable knob.

**Look-ahead is the cardinal sin.** Indicators see closed history plus the current Forming Bar
and nothing else (ADR-0002); fills are next-open / OHLC-touch with Stop-first on a clash
(ADR-0004); `ContextEnricher`s are keyed on `(ticker, as_of)`. If a change could let
information from the future reach a decision, say so explicitly rather than shipping it quietly.

**Optimize in-sample only.** Parameters get chosen on IS data and judged on OOS data
(`WalkForward`). Reporting a number that came from fitting the whole sample is the failure this
codebase is organised to prevent.

## Never commit

- `.hermes_cache/` — every local cache lives here (`bars/`, `ai/`, `pit/`, `runs/`, `reviews/`,
  `random_baselines/`). Delete the root to start cold.
- `research/output/` — hundreds of MB of intermediate CSVs. The scripts in `research/` are
  fine to track; their output is not.
- `.env` — credentials (`CTRADER_*`, `TIINGO_API_KEY`, `POLYGON_API_KEY`).

**Do commit `research/ideas.json`.** The idea backlog is work product, not cache — losing
it loses the thinking. It is the one Hermes artifact that is neither code nor cache.

## Configuration

Hermes' own knobs are namespaced `HERMES_*`: `HERMES_AI_MODEL`, `HERMES_AI_MAX_TOKENS`,
`HERMES_EDGAR_CHARS_PER_SECTION`, `HERMES_NEWS_COUNT`, `HERMES_NO_TRUSTSTORE`. Third-party
credentials keep their conventional names. Add new knobs with the `HERMES_` prefix and read
them at call time, not import time.

The LLM pricing table in `src/hermes/ai/observability.py` carries a `PRICING_VERIFIED_ON` date.
If you touch model defaults, check it — a stale table silently misreports what a research run
cost.

## The five-step process (ADR-0011)

Work here sits somewhere in: **idea generation → quantification → testing → portfolio →
repeat.** Each step has tooling, and the process exists because an agent with good tools
and no process will find a number that looks good instead of an edge that is real.

```bash
.venv/bin/hermes ideas --open        # 1. what is there to work on (+ --hit-rate)
                                     # 2. quantification: hermes-strategy, no command
.venv/bin/hermes run <name> --note   # 3. test: historical …
.venv/bin/hermes analyze <key>       #    … robustness + out-of-sample
.venv/bin/hermes correlate           # 4. portfolio: is this a new bet or the same one?
```

Three things that are easy to get wrong:

- **An `exploratory` evidence tier is not a result.** `analyze`'s `validation` axis
  reports the tier and why it stopped there. 30 trades is enough to form a view and
  nowhere near enough to risk capital. Quote the tier.
- **`params` fragile means the number was found, not earned.** If the metric collapses
  one notch from the chosen parameter value, don't iterate toward a sharper peak — widen
  the sample or drop the parameter.
- **Hermes cannot forward test.** There is no live venue. A strategy that survives the
  rubric is *ready to forward test*, never "deployable" — say it that way.

When the task is "does this idea work?" rather than "implement this", the
**`hermes-research`** skill owns steps 2–3 under a declared iteration budget, and stops on
an explicit rule rather than when the number looks good. Two rules it encodes that apply to
any research work here:

- **Never tune on the whole sample.** Parameters are chosen in-sample and scored out of
  sample (`hermes analyze --axes walkforward`). A number from fitting all the data is not
  a result.
- **Stop when improvement stops.** Two iterations with no out-of-sample gain means you
  have become the search; further fiddling is hand-optimisation that the deflated Sharpe
  no longer accounts for.

Ask before spending money (an AI Advisor makes a billed call per signal; Tiingo/Polygon
are paid) or before changing the instrument or the idea — that is a new research task,
not another iteration.

## Where things are going

ADR-0010's structural work is done: discovery, the ledger, the rubric and the review live
in `hermes/research/`, and `hermes` is a real CLI. `hermes.webui` keeps back-compat
re-exports (`from hermes.webui import discovery` still resolves) — new code should import
from `hermes.research`.

Still open: **universe runs are not in the CLI.** `hermes run` handles single-symbol
backtests; `UniverseBacktest`/`PortfolioBacktest` remain library- or UI-only, and
discovery recognises `build_backtest` but not `build_universe_backtest`. That is the next
piece.
