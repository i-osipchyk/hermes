"""The ``hermes`` command-line entry point — the research loop as commands.

This exists so an agent (or a person) can drive Hermes without writing a script per
question, and get back something parseable. Every command takes ``--json`` and prints a
single JSON document to stdout; without it you get a compact human reading of the same
data. Diagnostics go to stderr, so ``hermes ... --json`` is always safe to pipe.

The loop it supports:

    hermes strategies                 what can I run?
    hermes run <name>                 run it (ledger hit = free) and record it
    hermes runs                       what have I already tried?
    hermes analyze <key>              execute the rubric; write analysis.json
    hermes show <key>                 look at a stored run
    hermes review <key>               turn the evidence into a written verdict

Exit codes: 0 success, 1 usage/lookup error, 2 the work ran but produced nothing
usable (e.g. a zero-trade backtest) — so a caller can branch without parsing prose.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__

EXIT_OK, EXIT_ERR, EXIT_EMPTY = 0, 1, 2


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

def _emit(payload: dict, as_json: bool, lines: list[str]) -> None:
    """One JSON document on stdout, or the human lines. Never both."""
    if as_json:
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        for ln in lines:
            print(ln)


def _warn(msg: str) -> None:
    print(msg, file=sys.stderr)


def _fmt(x: Any, pct: bool = False) -> str:
    if x is None:
        return "—"
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return f"{x:.2%}" if pct else f"{x:,.2f}"
    return str(x)


def _metric_lines(m: dict) -> list[str]:
    return [
        f"  trades      {m.get('num_trades', 0)}"
        f"   params {m.get('num_params', 0)}",
        f"  return      {_fmt(m.get('total_return'), pct=True)}"
        f"   CAGR {_fmt(m.get('cagr'), pct=True)}",
        f"  sharpe      {_fmt(m.get('sharpe'))}"
        f"   (param-adjusted {_fmt(m.get('parameter_adjusted_sharpe'))})",
        f"  max DD      {_fmt(m.get('max_drawdown'), pct=True)}"
        f"   win rate {_fmt(m.get('win_rate'), pct=True)}"
        f"   profit factor {_fmt(m.get('profit_factor'))}",
    ]


def _parse_date(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s).replace(tzinfo=UTC) if len(s) > 10 else \
            datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as e:
        raise SystemExit(f"hermes: bad date {s!r} — use YYYY-MM-DD") from e


def _parse_params(pairs: list[str]) -> dict:
    """``-p fast=10 -p pct=0.5`` → ``{"fast": 10, "pct": 0.5}`` (JSON-typed)."""
    out: dict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"hermes: bad --param {pair!r} — expected name=value")
        k, v = pair.split("=", 1)
        try:
            out[k.strip()] = json.loads(v)
        except json.JSONDecodeError:
            out[k.strip()] = v          # leave it a string
    return out


# ---------------------------------------------------------------------------
# strategies
# ---------------------------------------------------------------------------

def cmd_strategies(args: argparse.Namespace) -> int:
    from .research.discovery import discover_all

    entries, broken = discover_all(Path(args.dir))
    payload = {
        "strategies": [
            {"name": e.name, "path": str(e.path), "generated_by": e.generated_by,
             "ai_generated": e.is_ai_generated}
            for e in entries
        ],
        "broken": [
            {"name": b.name, "path": str(b.path), "error": b.error} for b in broken
        ],
    }
    lines = [f"{len(entries)} runnable in {args.dir}/"]
    for e in entries:
        lines.append(f"  {e.name}" + ("  🤖" if e.is_ai_generated else ""))
    for b in broken:
        lines.append(f"  {b.name}  ✗ {b.error}")
    if broken:
        lines.append("")
        lines.append(f"{len(broken)} file(s) failed to import — `hermes strategies --json` "
                     f"carries the error.")
    _emit(payload, args.json, lines)
    return EXIT_OK if entries else EXIT_EMPTY


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _entry(name: str, directory: str):
    from .research.discovery import discover_all

    entries, broken = discover_all(Path(directory))
    for e in entries:
        if e.name == name:
            return e
    for b in broken:
        if b.name == name:
            raise SystemExit(f"hermes: '{name}' failed to import — {b.error}\n{b.traceback}")
    known = ", ".join(e.name for e in entries) or "(none)"
    raise SystemExit(f"hermes: no strategy '{name}' in {directory}/. Known: {known}")


def _build(args: argparse.Namespace):
    """A configured Backtest plus the ledger inputs that identify it."""
    from .research.discovery import configured_backtest, default_config
    from .research.ledger import cache_key, strategy_hash

    entry = _entry(args.strategy, args.dir)
    defaults = default_config(entry)

    ticker = args.symbol or defaults.symbol.ticker
    source_name = args.source or defaults.source.name
    start = _parse_date(args.start) if args.start else defaults.start
    end = _parse_date(args.end) if args.end else defaults.end
    cash = args.cash if args.cash is not None else defaults.starting_cash
    params = _parse_params(args.param)

    bt = configured_backtest(
        entry, ticker=ticker, source_name=source_name, start=start, end=end,
        starting_cash=cash, params=params, unconstrained=args.unconstrained,
    )
    key = cache_key(
        strategy=entry.name,
        strategy_hash=strategy_hash(entry.path),
        source=source_name,
        ticker=ticker,
        universe=None,
        start=start.isoformat(),
        end=end.isoformat(),
        params=params,
        sizer=repr(bt.sizer),
        unconstrained=args.unconstrained,
    )
    return entry, bt, key


def _meta_for(entry, bt, args, params: dict):
    from .research.ledger import RunMeta, strategy_hash

    return RunMeta(
        strategy=entry.name,
        strategy_hash=strategy_hash(entry.path),
        source=bt.source.name,
        ticker=bt.symbol.ticker,
        universe=None,
        start=bt.start.isoformat(),
        end=bt.end.isoformat(),
        params=params,
        sizer=repr(bt.sizer),
        unconstrained=args.unconstrained,
        created_at=datetime.now(UTC).isoformat(),
    )


def cmd_run(args: argparse.Namespace) -> int:
    from .research.ledger import RunLedger

    entry, bt, key = _build(args)
    ledger = RunLedger(args.runs_dir)

    cached = None if args.no_cache else ledger.load(key)
    if cached is not None:
        result_dict, from_cache = cached.result, True
    else:
        _warn(f"running {entry.name} · {bt.symbol.ticker} · "
              f"{bt.start.date()}→{bt.end.date()} …")
        result = bt.run()
        result_dict, from_cache = result.to_dict(), False
        if not args.no_save:
            ledger.save(key, result_dict, _meta_for(entry, bt, args, _parse_params(args.param)),
                        llm_log=getattr(result, "llm_log", None))

    m = result_dict.get("metrics", {})
    payload = {
        "run_key": key, "strategy": entry.name, "symbol": bt.symbol.ticker,
        "source": bt.source.name, "start": bt.start.isoformat(), "end": bt.end.isoformat(),
        "from_cache": from_cache, "metrics": m,
    }
    lines = [
        f"{entry.name} · {bt.symbol.ticker} · {bt.source.name}"
        + ("   (from ledger)" if from_cache else ""),
        f"run  {key}",
        *_metric_lines(m),
    ]
    gate = result_dict.get("llm_summary")
    if gate:
        payload["llm_summary"] = gate
        fo = int(gate.get("fail_open_calls") or 0)
        lines.append(f"  advisor     {gate.get('total_calls', 0)} calls"
                     + (f"   ⚠️  {fo} FAILED OPEN — run is less gated than configured"
                        if fo else "   gate clean"))
    if not m.get("num_trades"):
        lines.append("")
        lines.append("  ⚠️  zero trades — check the warmup, the entry condition, "
                     "and the date window.")
    _emit(payload, args.json, lines)
    return EXIT_OK if m.get("num_trades") else EXIT_EMPTY


# ---------------------------------------------------------------------------
# runs / show
# ---------------------------------------------------------------------------

def cmd_runs(args: argparse.Namespace) -> int:
    from .research.ledger import RunLedger

    entries = RunLedger(args.runs_dir).entries(
        strategy=args.strategy, ticker=args.symbol, limit=args.limit
    )
    payload = {"runs": [{"key": e.key, **e.meta.to_dict()} for e in entries]}
    lines = [f"{len(entries)} run(s) in the ledger"] if entries else ["ledger is empty"]
    for e in entries:
        lines.append(f"  {e.key}  {e.meta.created_at[:19]}  {e.meta.label}")
    _emit(payload, args.json, lines)
    return EXIT_OK if entries else EXIT_EMPTY


def _resolve(ledger, key: str):
    from .research.ledger import AmbiguousKey

    try:
        return ledger.resolve(key)
    except AmbiguousKey as e:
        raise SystemExit(f"hermes: {e}") from e
    except KeyError as e:
        raise SystemExit(f"hermes: no run matching {key!r}. `hermes runs` lists them.") from e


def cmd_show(args: argparse.Namespace) -> int:
    from .research.ledger import RunLedger

    ledger = RunLedger(args.runs_dir)
    rec = ledger.load(_resolve(ledger, args.key))
    if rec is None:
        raise SystemExit(f"hermes: run {args.key!r} has no stored result")

    payload: dict = {"run_key": rec.key, "meta": rec.meta.to_dict(), "metrics": rec.metrics}
    lines = [rec.meta.label, f"run  {rec.key}", *_metric_lines(rec.metrics)]
    if rec.fail_open_calls:
        lines.append(f"  ⚠️  {rec.fail_open_calls} advisor calls failed open")
    if args.trades:
        payload["trades"] = rec.trades
        lines.append("")
        lines.append(f"  {'side':<5} {'entry':>12} {'exit':>12} {'net P&L':>12}  reason")
        for t in rec.trades:
            lines.append(
                f"  {str(t.get('side')):<5} {_fmt(t.get('entry_price')):>12} "
                f"{_fmt(t.get('exit_price')):>12} {_fmt(t.get('net_pnl')):>12}  "
                f"{t.get('exit_reason')}"
            )
    if args.full:
        payload["result"] = rec.result
    _emit(payload, args.json, lines)
    return EXIT_OK


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

def cmd_analyze(args: argparse.Namespace) -> int:
    from .research.analysis import ALL_AXES, DEFAULT_AXES, analyze
    from .research.ledger import RunLedger

    axes = tuple(a.strip() for a in args.axes.split(",")) if args.axes else DEFAULT_AXES
    unknown = [a for a in axes if a not in ALL_AXES]
    if unknown:
        raise SystemExit(f"hermes: unknown axis {unknown}. Known: {', '.join(ALL_AXES)}")

    ledger = RunLedger(args.runs_dir)
    from .research.discovery import configured_backtest, default_config

    # `analyze` accepts either a stored run key or a strategy name. A key is richer:
    # it pins the exact window and parameters the run used, so the re-run axes
    # (costs, oos, walkforward) reproduce that run rather than a fresh default.
    rec, bt, strategy_name, run_key = None, None, None, None
    try:
        run_key = ledger.resolve(args.target)
    except Exception:  # noqa: BLE001 — not a key; fall through to a strategy name
        run_key = None

    if run_key:
        rec = ledger.load(run_key)
        strategy_name = rec.meta.strategy
        entry = _entry(strategy_name, args.dir)
        defaults = default_config(entry)
        bt = configured_backtest(
            entry,
            ticker=rec.meta.ticker or defaults.symbol.ticker,
            source_name=rec.meta.source,
            start=_parse_date(rec.meta.start),
            end=_parse_date(rec.meta.end),
            starting_cash=defaults.starting_cash,
            params=rec.meta.params,
            unconstrained=rec.meta.unconstrained,
        )
    else:
        args.strategy = args.target
        entry, bt, run_key = _build(args)
        strategy_name = entry.name
        rec = ledger.load(run_key)

    needs_rerun = {"costs", "oos", "walkforward"} & set(axes)
    result = None
    if rec is not None and not needs_rerun:
        # Every requested axis reads a finished run, so the stored one will do.
        from .research.ledger import restore_result
        result = restore_result(rec.result)
    elif bt is not None:
        _warn(f"analysing {strategy_name} ({', '.join(axes)}) — re-running as needed …")
        result = bt.run()

    analysis = analyze(
        bt, result, axes=axes, run_key=run_key, strategy=strategy_name,
        is_frac=args.is_frac, seed=args.seed, baseline_n=args.baseline_n,
        build_backtest=(lambda **kw: bt) if "baseline" in axes else None,
    )

    if run_key and not args.no_save:
        out = ledger.path / run_key
        if out.exists():
            (out / "analysis.json").write_text(json.dumps(analysis.to_dict(), indent=2, default=str))
            _warn(f"wrote {out / 'analysis.json'}")

    lines = [f"{strategy_name} · run {run_key}", ""]
    for a in analysis.axes:
        lines.append(f"  {'✓' if a.ran else '·'} {a.axis:<12} {a.note}")
    _emit(analysis.to_dict(), args.json, lines)
    return EXIT_OK if analysis.ran else EXIT_EMPTY


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

def cmd_review(args: argparse.Namespace) -> int:
    from .research import review as rv
    from .research.ledger import RunLedger

    ledger = RunLedger(args.runs_dir)
    key = _resolve(ledger, args.key)
    rec = ledger.load(key)
    if rec is None:
        raise SystemExit(f"hermes: run {key!r} has no stored result")

    rid = rv.run_id(rec.result)
    rv.write_result(rec.result, rid)
    analysis_path = ledger.path / key / "analysis.json"
    if analysis_path.exists():
        rv.attach_analysis(rid, json.loads(analysis_path.read_text()))
    else:
        _warn("no analysis.json for this run — the review will have no computed evidence. "
              f"Run `hermes analyze {key}` first.")

    existing = rv.read_review(rid)
    if existing and not args.force:
        _emit({"review_id": rid, "status": "done", "review": existing},
              args.json, [existing])
        return EXIT_OK

    if not rv.claude_available():
        _warn("the `claude` CLI is not on PATH — printing the prompt to run by hand.")
        _emit({"review_id": rid, "status": "unavailable",
               "instructions": rv.manual_instructions(rid)},
              args.json, [rv.manual_instructions(rid)])
        return EXIT_ERR

    rv.launch(rid)
    _emit({"review_id": rid, "status": "running", "path": str(rv.review_path(rid))},
          args.json,
          [f"review {rid} launched in the background.",
           f"it will appear at {rv.review_path(rid)}",
           f"check with: hermes review {key}"])
    return EXIT_OK


# ---------------------------------------------------------------------------
# argument wiring
# ---------------------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="emit one JSON document on stdout")
    p.add_argument("--dir", default="strategies", help="strategies directory (default: strategies)")
    p.add_argument("--runs-dir", default=None,
                   help="ledger root (default: .hermes_cache/runs)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes",
        description="Hermes trading framework — the research loop as commands.",
    )
    parser.add_argument("--version", action="version", version=f"hermes {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("strategies", help="list the runnable strategies (and any that fail to import)")
    _add_common(p)
    p.set_defaults(func=cmd_strategies)

    p = sub.add_parser("run", help="run a strategy's backtest and record it in the ledger")
    p.add_argument("strategy")
    p.add_argument("--symbol", help="override the strategy's symbol")
    p.add_argument("--source", help="override the DataSource (binance, yfinance, …)")
    p.add_argument("--start", help="YYYY-MM-DD")
    p.add_argument("--end", help="YYYY-MM-DD")
    p.add_argument("--cash", type=float, help="starting cash")
    p.add_argument("-p", "--param", action="append", default=[], metavar="NAME=VALUE",
                   help="override a declared Strategy Parameter (repeatable)")
    p.add_argument("--unconstrained", action="store_true",
                   help="skip the capital check — orders are never rejected for funds")
    p.add_argument("--no-cache", action="store_true", help="re-run even if the ledger has it")
    p.add_argument("--no-save", action="store_true", help="do not write to the ledger")
    _add_common(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("runs", help="list recorded runs, newest first")
    p.add_argument("--strategy", help="only this strategy")
    p.add_argument("--symbol", help="only this ticker")
    p.add_argument("--limit", type=int, help="at most N")
    _add_common(p)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("show", help="show a recorded run")
    p.add_argument("key", help="a run key, or a unique prefix of one")
    p.add_argument("--trades", action="store_true", help="include the trade blotter")
    p.add_argument("--full", action="store_true", help="include the whole stored result (JSON)")
    _add_common(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("analyze", aliases=["analyse"],
                       help="execute the analyse-results rubric and write analysis.json")
    p.add_argument("target", help="a run key, or a strategy name to run fresh")
    p.add_argument("--axes", help="comma-separated subset (default: the cheap six)")
    p.add_argument("--is-frac", type=float, default=0.7, help="in-sample fraction (default 0.7)")
    p.add_argument("--seed", type=int, default=42, help="RNG seed for the statistical axes")
    p.add_argument("--baseline-n", type=int, default=50,
                   help="simulations for the `baseline` axis (default 50)")
    p.add_argument("--no-save", action="store_true", help="do not write analysis.json")
    # so `analyze <strategy-name>` can build a run like `hermes run` does
    p.add_argument("--symbol"), p.add_argument("--source")
    p.add_argument("--start"), p.add_argument("--end")
    p.add_argument("--cash", type=float)
    p.add_argument("-p", "--param", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--unconstrained", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("review", help="write a verdict on a run by driving Claude Code")
    p.add_argument("key", help="a run key, or a unique prefix of one")
    p.add_argument("--force", action="store_true", help="re-review even if one exists")
    _add_common(p)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser(
        "install-skills",
        help="copy the Hermes Claude Code skills into a project (or ~/.claude with --user)",
    )
    p.add_argument("--user", action="store_true", help="install into ~/.claude/skills")
    p.add_argument("--dest", default=".", help="project dir to install into (default: cwd)")
    p.set_defaults(func=cmd_install_skills)

    return parser


def cmd_install_skills(args: argparse.Namespace) -> int:
    from .skilltools import PORTABLE_SKILLS, install_skills

    target = install_skills(dest=args.dest, user=args.user)
    print(f"Installed {len(PORTABLE_SKILLS)} skills + hermes-reference/ into {target}")
    print("Open this project in Claude Code and type /ask-hermes to start.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit as e:                       # our own argument/lookup errors
        if isinstance(e.code, str):
            _warn(e.code)
            return EXIT_ERR
        return int(e.code or EXIT_OK)
    except KeyboardInterrupt:
        _warn("interrupted")
        return EXIT_ERR


if __name__ == "__main__":
    raise SystemExit(main())
