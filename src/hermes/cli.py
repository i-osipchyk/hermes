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
        note=getattr(args, "note", "") or "",
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
    payload_note = cached.meta.note if cached is not None else (args.note or "")
    payload = {
        "run_key": key, "strategy": entry.name, "symbol": bt.symbol.ticker,
        "source": bt.source.name, "start": bt.start.isoformat(), "end": bt.end.isoformat(),
        "from_cache": from_cache, "metrics": m, "note": payload_note,
    }
    lines = [
        f"{entry.name} · {bt.symbol.ticker} · {bt.source.name}"
        + ("   (from ledger)" if from_cache else ""),
        f"run  {key}",
    ]
    if payload_note:
        lines.append(f"why  {payload_note}")
    lines.extend(_metric_lines(m))
    gate = result_dict.get("llm_summary")
    if gate:
        payload["llm_summary"] = gate
        fo = int(gate.get("fail_open_calls") or 0)
        lines.append(f"  advisor     {gate.get('total_calls', 0)} calls"
                     + (f"   ⚠️  {fo} FAILED OPEN — run is less gated than configured"
                        if fo else "   gate clean"))
    from .research.analysis import run_coverage

    payload["coverage"] = run_coverage(result_dict)
    if not m.get("num_trades"):
        from .research.analysis import diagnose_zero_trades

        why = diagnose_zero_trades(result_dict)
        payload["zero_trade_diagnosis"] = why
        lines.append("")
        lines.append(f"  ⚠️  zero trades — {why}")
    _emit(payload, args.json, lines)
    return EXIT_OK if m.get("num_trades") else EXIT_EMPTY


# ---------------------------------------------------------------------------
# runs / show
# ---------------------------------------------------------------------------

def cmd_runs(args: argparse.Namespace) -> int:
    from .research.ledger import RunLedger

    entries = RunLedger(args.runs_dir).entries(
        strategy=args.strategy, ticker=args.symbol, limit=args.limit,
        note_contains=args.note_contains,
    )
    payload = {"runs": [{"key": e.key, **e.meta.to_dict()} for e in entries]}
    lines = [f"{len(entries)} run(s) in the ledger"] if entries else ["ledger is empty"]
    for e in entries:
        lines.append(f"  {e.key}  {e.meta.created_at[:19]}  {e.meta.label}")
        if e.meta.note:
            lines.append(f"                      ↳ {e.meta.note}")
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
    lines = [rec.meta.label, f"run  {rec.key}"]
    if rec.meta.note:
        lines.append(f"why  {rec.meta.note}")
    lines.extend(_metric_lines(rec.metrics))
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

    from .research.analysis import RESTORABLE_AXES

    # A stored run only carries its serialised dict; axes needing real Trade objects
    # or a benchmark series must re-run the backtest.
    needs_rerun = set(axes) - RESTORABLE_AXES
    result = None
    if rec is not None and not needs_rerun:
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
# ideas — step 1 of the framework
# ---------------------------------------------------------------------------

def cmd_ideas(args: argparse.Namespace) -> int:
    from .research.ideas import SOURCES, IdeaBook

    book = IdeaBook(args.ideas_file)
    action = args.ideas_action

    if action == "add":
        if args.source not in SOURCES:
            raise SystemExit(f"hermes: unknown --source {args.source!r}. "
                             f"Known: {', '.join(SOURCES)}")
        idea = book.add(args.title, hypothesis=args.hypothesis or "",
                        source=args.source, source_detail=args.detail or "",
                        tags=args.tag or [])
        _emit({"idea": idea.to_dict()}, args.json,
              [f"{idea.id}  {idea.label}",
               "  next: quantify it into unambiguous rules, then "
               f"`hermes ideas set {idea.id} --status quantified`"])
        return EXIT_OK

    if action == "set":
        changes = {k: v for k, v in {
            "status": args.status, "hypothesis": args.hypothesis,
            "strategy": args.strategy, "verdict": args.verdict,
            "run_keys": args.run_key or None, "tags": args.tag or None,
        }.items() if v}
        if not changes:
            raise SystemExit("hermes: nothing to change — pass --status/--hypothesis/"
                             "--strategy/--verdict/--run-key/--tag")
        try:
            idea = book.update(args.id, **changes)
        except ValueError as e:
            raise SystemExit(f"hermes: {e}") from e
        except LookupError as e:
            raise SystemExit(f"hermes: {e}") from e
        _emit({"idea": idea.to_dict()}, args.json, [f"{idea.id}  {idea.label}"])
        return EXIT_OK

    if action == "rm":
        try:
            idea = book.remove(args.id)
        except LookupError as e:
            raise SystemExit(f"hermes: {e}") from e
        _emit({"removed": idea.to_dict()}, args.json, [f"removed {idea.id}  {idea.title}"])
        return EXIT_OK

    # default: list
    ideas = book.list(status=args.status, source=args.source_filter,
                      tag=args.tag_filter, open_only=args.open)
    pipeline = book.pipeline()
    payload = {
        "ideas": [i.to_dict() for i in ideas],
        "pipeline": pipeline,
        "hit_rate": book.hit_rate() if args.hit_rate else None,
    }
    lines = ["  ".join(f"{s}={n}" for s, n in pipeline.items() if n) or "backlog is empty"]
    for i in ideas:
        lines.append(f"  {i.id}  {i.status:<10} {i.title}"
                     + (f"   [{i.source}]" if i.source != "other" else ""))
        if i.hypothesis:
            lines.append(f"              ↳ {i.hypothesis}")
        if i.run_keys:
            lines.append(f"              runs: {', '.join(i.run_keys)}")
    if args.hit_rate:
        lines.append("")
        lines.append("  provenance      decided  validated  rate")
        for src, r in payload["hit_rate"].items():
            if r["total"]:
                rate = "—" if r["rate"] is None else f"{r['rate']:.0%}"
                lines.append(f"  {src:<14} {r['decided']:>7}  {r['validated']:>9}  {rate:>4}")
    _emit(payload, args.json, lines)
    return EXIT_OK if ideas or any(pipeline.values()) else EXIT_EMPTY


# ---------------------------------------------------------------------------
# correlate — step 4 of the framework
# ---------------------------------------------------------------------------

def cmd_correlate(args: argparse.Namespace) -> int:
    from .backtest import correlate_curves
    from .research.ledger import RunLedger

    ledger = RunLedger(args.runs_dir)
    keys = [_resolve(ledger, k) for k in args.keys] if args.keys else None
    if keys is None:
        # No keys given: the newest run per strategy — "what does my book look like?"
        seen: dict[str, str] = {}
        for e in ledger.entries():
            seen.setdefault(e.meta.strategy, e.key)
        keys = list(seen.values())
    if len(keys) < 2:
        raise SystemExit(
            "hermes: correlation needs at least 2 runs. `hermes runs` lists them; "
            "pass keys explicitly, or record more runs first."
        )

    curves, labels = {}, {}
    for key in keys:
        rec = ledger.load(key)
        if rec is None:
            raise SystemExit(f"hermes: run {key!r} has no stored result")
        label = f"{rec.meta.strategy}:{rec.meta.ticker or rec.meta.universe or '?'}"
        while label in curves:                      # keep labels unique
            label += "'"
        labels[label] = key
        curves[label] = [
            (datetime.fromisoformat(ts), float(eq))
            for ts, eq in rec.result.get("equity_curve", [])
        ]

    m = correlate_curves(curves, frequency=args.frequency)
    payload = {**m.to_dict(), "runs": labels}

    width = max(len(x) for x in m.labels) + 2
    lines = [f"return correlation ({args.frequency}), {len(m.labels)} streams", ""]
    lines.append(" " * width + "  ".join(f"{i:>5}" for i in range(len(m.labels))))
    for i, lab in enumerate(m.labels):
        cells = []
        for v in m.matrix[i]:
            cells.append("    —" if v is None else f"{v:5.2f}")
        lines.append(f"{i} {lab:<{width - 2}}" + "  ".join(cells))
    lines.append("")
    if m.diversification_ratio is not None:
        lines.append(f"diversification ratio  {m.diversification_ratio:.2f}  "
                     f"(1.00 = one bet; lower is better)")
    for a, b, c in m.redundant:
        lines.append(f"  ⚠️  {a} and {b} are {c:.0%} correlated — effectively one bet")
    for a, b, c in m.diversifying:
        lines.append(f"  ✓  {a} and {b} at {c:+.2f} — genuinely diversifying")
    for a, b, n in m.unmeasurable:
        lines.append(f"  ·  {a} vs {b}: only {n} shared periods — not measurable")
    _emit(payload, args.json, lines)
    return EXIT_OK


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
    p.add_argument("--note", help="why this run — the hypothesis it tests, or what "
                                   "changed since the last variant. Recorded in the ledger.")
    p.add_argument("--no-cache", action="store_true", help="re-run even if the ledger has it")
    p.add_argument("--no-save", action="store_true", help="do not write to the ledger")
    _add_common(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("runs", help="list recorded runs, newest first")
    p.add_argument("--strategy", help="only this strategy")
    p.add_argument("--symbol", help="only this ticker")
    p.add_argument("--note-contains", dest="note_contains",
                   help="only runs whose note matches this text")
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
    p.add_argument("--axes", help="comma-separated subset (default: the cheap seven)")
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

    p = sub.add_parser("ideas", help="the idea backlog — step 1 of the research framework")
    isub = p.add_subparsers(dest="ideas_action")
    for name, helptext in [("list", "list the backlog (default)"), ("add", "capture an idea"),
                           ("set", "update an idea"), ("rm", "remove an idea")]:
        q = isub.add_parser(name, help=helptext)
        q.add_argument("--ideas-file", default="research/ideas.json",
                       help="backlog file (default: research/ideas.json)")
        q.add_argument("--json", action="store_true")
        if name == "add":
            q.add_argument("title")
            q.add_argument("--hypothesis", help="the claim, in Hermes vocabulary")
            q.add_argument("--source", default="other",
                           help="book | discretionary | trader | course | data | other")
            q.add_argument("--detail", help="which book / trader / course / session")
            q.add_argument("--tag", action="append", default=[])
        elif name == "set":
            q.add_argument("id", help="idea id, or a unique prefix")
            q.add_argument("--status",
                           help="raw | quantified | testing | validated | rejected | parked")
            q.add_argument("--hypothesis")
            q.add_argument("--strategy", help="strategies/<name>.py this became")
            q.add_argument("--verdict", help="why it was validated or rejected")
            q.add_argument("--run-key", action="append", default=[],
                           help="link a ledger run (repeatable)")
            q.add_argument("--tag", action="append", default=[])
        elif name == "rm":
            q.add_argument("id", help="idea id, or a unique prefix")
        else:
            q.add_argument("--status", help="only this status")
            q.add_argument("--source", dest="source_filter", help="only this provenance")
            q.add_argument("--tag", dest="tag_filter", help="only this tag")
            q.add_argument("--open", action="store_true", help="hide validated/rejected")
            q.add_argument("--hit-rate", action="store_true",
                           help="validated-vs-rejected per provenance")
        q.set_defaults(func=cmd_ideas, ideas_action=name)
    # `hermes ideas [filters]` with no sub-action lists, so the same options have to
    # exist on the parent parser — a subparser only sees args that follow its name.
    p.add_argument("--ideas-file", default="research/ideas.json",
                   help="backlog file (default: research/ideas.json)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--status", help="only this status")
    p.add_argument("--source", dest="source_filter", help="only this provenance")
    p.add_argument("--tag", dest="tag_filter", help="only this tag")
    p.add_argument("--open", action="store_true", help="hide validated/rejected")
    p.add_argument("--hit-rate", action="store_true",
                   help="validated-vs-rejected per provenance")
    p.set_defaults(func=cmd_ideas, ideas_action="list")

    p = sub.add_parser("correlate",
                       help="return correlation between runs — step 4, portfolio building")
    p.add_argument("keys", nargs="*",
                   help="run keys (default: the newest run of each strategy)")
    p.add_argument("--frequency", default="D",
                   help="pandas offset for resampling: D, W, ME (default D)")
    _add_common(p)
    p.set_defaults(func=cmd_correlate)

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
