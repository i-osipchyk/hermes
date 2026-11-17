"""The ``hermes`` command-line entry point — the research loop as commands.

This exists so an agent (or a person) can drive Hermes without writing a script per
question, and get back something parseable. Every command takes ``--json`` and prints a
single JSON document to stdout; without it you get a compact human reading of the same
data. Diagnostics go to stderr, so ``hermes ... --json`` is always safe to pipe.

The loop it supports:

    hermes strategies                 what can I run?
    hermes run <name>                 run it (ledger hit = free) and record it
    hermes run <name> --universe <u>  the same over many symbols, one shared capital pool
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
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .research.holdout import HoldoutSource, HoldoutViolation, base_timeframe
from .research.jsonsafe import decode_nonfinite, encode_nonfinite

if TYPE_CHECKING:
    from .research.ledger import RunLedger, RunRecord
    from .research.universe_runs import UniverseRun

EXIT_OK, EXIT_ERR, EXIT_EMPTY = 0, 1, 2


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

def _emit(payload: dict, as_json: bool, lines: list[str]) -> None:
    """One JSON document on stdout, or the human lines. Never both."""
    if as_json:
        json.dump(encode_nonfinite(payload), sys.stdout, indent=2, default=str, allow_nan=False)
        sys.stdout.write("\n")
    else:
        for ln in lines:
            print(ln)


def _analysis_json(analysis: Any) -> str:
    return json.dumps(encode_nonfinite(analysis.to_dict()), indent=2, default=str, allow_nan=False)


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
             "ai_generated": e.is_ai_generated,
             "universe": e.build_universe_backtest is not None,
             "single_symbol": e.build_backtest is not None}
            for e in entries
        ],
        "broken": [
            {"name": b.name, "path": str(b.path), "error": b.error} for b in broken
        ],
    }
    lines = [f"{len(entries)} runnable in {args.dir}/"]
    for e in entries:
        lines.append(f"  {e.name}" + ("  🤖" if e.is_ai_generated else "")
                     + ("  [universe]" if e.build_universe_backtest is not None else ""))
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
    from .research.discovery import configured_backtest, default_config, stable_end
    from .research.ledger import advisor_fingerprint, cache_key, source_epoch, strategy_hash

    entry = _entry(args.strategy, args.dir)
    defaults = default_config(entry)

    ticker = args.symbol or defaults.symbol.ticker
    source_name = args.source or defaults.source.name
    start = _parse_date(args.start) if args.start else defaults.start
    cash = args.cash if args.cash is not None else defaults.starting_cash
    params = _parse_params(args.param)

    # Research reads go through the holdout guard (ADR-0012); a run that names no end
    # stops where the holdout starts (this also keeps a default "now" out of the key).
    bt = configured_backtest(
        entry, ticker=ticker, source_name=source_name, start=start,
        end=_parse_date(args.end) if args.end else stable_end(defaults.end),
        starting_cash=cash, params=params, unconstrained=args.unconstrained,
    )
    bt = replace(bt, source=HoldoutSource(bt.source))
    if not args.end:
        held = bt.source.holdout_start(bt.symbol, base_timeframe(bt))
        if held is not None and bt.end > held:
            bt = replace(bt, end=held)
    end = bt.end
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
        starting_cash=cash,
        advisor=advisor_fingerprint(bt.advisor),
        data_epoch=source_epoch(bt.source, ticker),
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
        idea=getattr(args, "idea", None),
        kind="diagnostic" if getattr(args, "diagnostic", False) else "trial",
    )


def _is_universe_request(args: argparse.Namespace) -> bool:
    if getattr(args, "universe", None):
        if getattr(args, "symbol", None):
            raise SystemExit("hermes: --symbol and --universe are exclusive")
        return True
    return bool(_entry(args.strategy, args.dir).is_universe)


def _build_universe(args: argparse.Namespace) -> UniverseRun:
    """A configured UniverseRun from the CLI flags."""
    from .research.universe_runs import universe_run

    entry = _entry(args.strategy, args.dir)
    try:
        return universe_run(
            entry, universe=getattr(args, "universe", None), source_name=args.source,
            start=_parse_date(args.start) if args.start else None,
            end=_parse_date(args.end) if args.end else None,
            starting_cash=args.cash, params=_parse_params(args.param),
            unconstrained=args.unconstrained,
        )
    except ValueError as e:
        raise SystemExit(f"hermes: {e}") from e


def _progress(label: str) -> Callable[[int, int, object], None]:
    """A PortfolioBacktest progress callback that reports every 10% on stderr."""
    last = [-1]

    def cb(done: int, total: int, ts: object) -> None:
        pct = int(done * 10 / total) if total else 10
        if pct > last[0]:
            last[0] = pct
            _warn(f"  {label}: {pct * 10}%  ({str(ts)[:10]})")
    return cb


def _universe_lines(uni: dict[str, Any] | None, top: int = 10) -> list[str]:
    if not uni:
        return []
    rows = sorted(uni.get("summary_rows", []), key=lambda r: r.get("net_pnl") or 0,
                  reverse=True)
    traded = [r for r in rows if r.get("trades")]
    out = ["", f"  universe    {uni.get('universe_size', len(rows))} symbols, "
               f"{len(traded)} traded"]
    coverage = uni.get("coverage")
    if coverage is None:
        out.append("  coverage    not recorded (run predates coverage tracking)")
    else:
        skipped = uni.get("skipped", [])
        line = (f"  coverage    {coverage:.0%} "
                f"({uni['legs_requested'] - len(skipped)}/{uni['legs_requested']} legs had data)")
        if skipped:
            shown = ", ".join(skipped[:10]) + (f" … +{len(skipped) - 10}" if len(skipped) > 10
                                              else "")
            line += f" — skipped: {shown}"
        out.append(line)
    if traded:
        out.append(f"  {'symbol':<16} {'trades':>6} {'win %':>7} {'net P&L':>12}")
        shown = traded if len(traded) <= 2 * top else traded[:top] + [None] + traded[-top:]
        for r in shown:
            if r is None:
                out.append(f"  {'…':<16}")
                continue
            win = f"{r['win_%']:.1f}" if r.get("win_%") is not None else "—"
            out.append(f"  {r['symbol']:<16} {r['trades']:>6} {win:>7} "
                       f"{_fmt(r.get('net_pnl')):>12}")
    return out


def cmd_run_universe(args: argparse.Namespace) -> int:
    from .research.ledger import RunLedger

    ur = _build_universe(args)
    key = ur.key
    ledger = RunLedger(args.runs_dir)

    cached = None if args.no_cache else ledger.load(key)
    uni = ledger.load_universe_meta(key) if cached is not None else None
    if cached is not None and uni is not None:
        result_dict, from_cache = cached.result, True
    else:
        if ur.has_advisor and not args.allow_advisor:
            raise SystemExit(
                f"hermes: '{ur.entry.name}' has an AI Advisor — a universe run makes a billed "
                f"call per signal on every leg ({ur.size_hint or '?'} symbols). "
                f"Pass --allow-advisor to spend it."
            )
        label = f"{ur.entry.name} · {ur.universe}"
        _warn(f"running {label} ({ur.size_hint or '?'} symbols) · "
              f"{ur.start.date()}→{ur.end.date()} …")
        universe_result = ur.run(progress_callback=_progress(label))
        ur = _build_universe(args)      # re-key on the data epoch the run left behind
        key = ur.key
        pr = universe_result.portfolio_result
        from_cache = False
        uni = {
            "universe_size": universe_result.universe_size,
            "summary_rows": pr.summary_rows(),
            "per_symbol_counts": {k: len(v) for k, v in pr.per_symbol.items()},
            "legs_requested": pr.legs_requested,
            "skipped": list(pr.skipped),
            "coverage": pr.coverage,
        }
        if args.no_save:
            result_dict = pr.result.to_dict()
        else:
            result_dict = ur.save(ledger, universe_result, note=args.note or "")

    m = result_dict.get("metrics", {})
    note = cached.meta.note if cached is not None else (args.note or "")
    payload = {
        "run_key": key, "strategy": ur.entry.name, "universe": ur.universe,
        "source": ur.source, "start": ur.start.isoformat(), "end": ur.end.isoformat(),
        "from_cache": from_cache, "metrics": m, "note": note,
        "universe_size": uni.get("universe_size"),
        "legs_requested": uni.get("legs_requested"),
        "skipped": uni.get("skipped", []),
        "coverage": uni.get("coverage"),
        "per_symbol": uni.get("summary_rows", []),
    }
    lines = [
        f"{ur.entry.name} · universe {ur.universe} · {ur.source}"
        + ("   (from ledger)" if from_cache else ""),
        f"run  {key}",
    ]
    if note:
        lines.append(f"why  {note}")
    lines.extend(_metric_lines(m))
    lines.extend(_universe_lines(uni))
    if not m.get("num_trades"):
        lines.append("")
        lines.append("  ⚠️  zero trades across the whole universe")
    _emit(payload, args.json, lines)
    return EXIT_OK if m.get("num_trades") else EXIT_EMPTY


def cmd_run(args: argparse.Namespace) -> int:
    from .research.ledger import RunLedger

    if _is_universe_request(args):
        if args.idea or args.diagnostic:
            raise SystemExit("hermes: --idea/--diagnostic are not supported on universe runs yet")
        return cmd_run_universe(args)
    entry, bt, key = _build(args)
    ledger = RunLedger(args.runs_dir)

    cached = None if args.no_cache else ledger.load(key)
    if cached is not None:
        result_dict, from_cache = cached.result, True
        # One configuration is one ledger row. If it was first run as a diagnostic (or for
        # no idea) and is now claimed as a trial, count it: undercounting trials flatters
        # the deflated Sharpe, overcounting only makes it stricter.
        wants_trial = bool(args.idea) and not args.diagnostic
        if wants_trial and (cached.meta.kind == "diagnostic" or cached.meta.idea is None):
            ledger.attribute(key, args.idea, "trial")
    else:
        _warn(f"running {entry.name} · {bt.symbol.ticker} · "
              f"{bt.start.date()}→{bt.end.date()} …")
        result = bt.run()
        result_dict, from_cache = result.to_dict(), False
        # Fetching may have taught the source a split/dividend: key the run on the data
        # it actually read, or the next identical request would miss the ledger once.
        key = _build(args)[2]
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
    uni = ledger.load_universe_meta(rec.key)
    if uni is not None:
        payload["universe_size"] = uni.get("universe_size")
        payload["coverage"] = uni.get("coverage")
        payload["skipped"] = uni.get("skipped", [])
        payload["per_symbol"] = uni.get("summary_rows", [])
        lines.extend(_universe_lines(uni))
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

    try:
        stored = ledger.load(ledger.resolve(args.target))
    except Exception:  # noqa: BLE001 — not a key; maybe a strategy name
        stored = None
    if stored is not None and stored.meta.universe:
        return _analyze_universe(args, ledger, axes, stored)
    if stored is None:
        args.strategy = args.target
        if _is_universe_request(args):
            return _analyze_universe(args, ledger, axes, None)


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
        entry, bt = _rebuild(args, rec)
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
        deflation=_deflation_inputs(args, ledger, rec) if "deflation" in axes else None,
        null_n=args.null_n,
        holdout_carved=_holdout_carved(bt),
    )

    if run_key and not args.no_save:
        out = ledger.path / run_key
        if out.exists():
            (out / "analysis.json").write_text(_analysis_json(analysis))
            _warn(f"wrote {out / 'analysis.json'}")

    lines = [f"{strategy_name} · run {run_key}", ""]
    for a in analysis.axes:
        lines.append(f"  {'✓' if a.ran else '·'} {a.axis:<12} {a.note}")
    _emit(analysis.to_dict(), args.json, lines)
    return EXIT_OK if analysis.ran else EXIT_EMPTY


def _rebuild(args: argparse.Namespace, rec: RunRecord, *, guarded: bool = True):
    """The strategy entry and a fresh Backtest reproducing a stored run's window and
    parameters. Guarded by default: a stored row may predate the holdout guard."""
    from .research.discovery import configured_backtest, default_config

    entry = _entry(rec.meta.strategy, args.dir)
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
    return entry, (replace(bt, source=HoldoutSource(bt.source)) if guarded else bt)


def _holdout_carved(bt) -> bool | None:
    """Whether the instrument's history yields a holdout (None when the source is unguarded)."""
    if bt is None or not isinstance(bt.source, HoldoutSource):
        return None
    return bt.source.holdout_start(bt.symbol, base_timeframe(bt)) is not None


def _deflation_inputs(args: argparse.Namespace, ledger: RunLedger, rec: RunRecord | None):
    """The trial count and trial Sharpes behind a run, or None when it belongs to no idea."""
    from .research.analysis import DeflationInputs
    from .research.ideas import IdeaBook
    from .research.trials import TrialRegistry

    if rec is None or not rec.meta.idea:
        return None
    registry = TrialRegistry(ledger, IdeaBook(args.ideas_file))
    try:
        return DeflationInputs(registry.effective_trials(rec.meta.idea),
                               registry.representative_sharpes(rec.meta.idea))
    except LookupError as e:
        raise SystemExit(f"hermes: run is attributed to idea {rec.meta.idea!r}: {e}") from e


# Axes that perturb and re-run one Backtest. A universe run is a PortfolioBacktest over
# many legs, and nothing re-runs that with perturbed inputs yet.
_UNIVERSE_UNMEASURED = ("costs", "params", "oos", "null", "walkforward", "baseline")


def _analyze_universe(args: argparse.Namespace, ledger: RunLedger, axes: tuple[str, ...],
                      rec: RunRecord | None) -> int:
    """The rubric over a universe run: what can be measured, and what can't (with why)."""
    from .research.analysis import ALL_AXES, RESTORABLE_AXES, AxisResult, analyze
    from .research.ledger import restore_result
    from .research.universe_runs import universe_run

    if rec is not None:
        entry = _entry(rec.meta.strategy, args.dir)
        try:
            ur = universe_run(
                entry, universe=rec.meta.universe, source_name=rec.meta.source,
                start=_parse_date(rec.meta.start), end=_parse_date(rec.meta.end),
                params=rec.meta.params, unconstrained=rec.meta.unconstrained,
            )
        except ValueError as e:
            raise SystemExit(f"hermes: {e}") from e
        run_key = rec.key
    else:
        ur = _build_universe(args)
        run_key = ur.key
        rec = ledger.load(run_key)

    measurable = tuple(a for a in axes if a not in _UNIVERSE_UNMEASURED)
    result = None
    if rec is not None and set(measurable) <= RESTORABLE_AXES:
        result = restore_result(rec.result)
    elif measurable:
        if ur.has_advisor and not getattr(args, "allow_advisor", False):
            raise SystemExit(
                f"hermes: re-running '{ur.entry.name}' over its universe makes billed AI "
                f"Advisor calls. Pass --allow-advisor, or ask only for --axes trades,gate."
            )
        _warn(f"analysing {ur.entry.name} · {ur.universe} ({', '.join(measurable)}) — "
              f"re-running the universe …")
        result = ur.run(progress_callback=_progress(ur.entry.name)).portfolio_result.result

    uni_meta = ledger.load_universe_meta(run_key) or {}
    analysis = analyze(None, result, axes=measurable, run_key=run_key,
                       strategy=ur.entry.name, seed=args.seed,
                       coverage=uni_meta.get("coverage"))
    for axis in axes:
        if axis in _UNIVERSE_UNMEASURED:
            why = (f"not measured — {axis} re-runs a perturbed backtest, which is single-symbol "
                   f"only; universe runs have no re-run path yet")
            analysis.axes.append(AxisResult(axis, False, why, error=why))
    order = {a: i for i, a in enumerate(ALL_AXES)}
    analysis.axes.sort(key=lambda a: order.get(a.axis, 99))

    if not args.no_save and (ledger.path / run_key).exists():
        out = ledger.path / run_key / "analysis.json"
        out.write_text(_analysis_json(analysis))
        _warn(f"wrote {out}")

    lines = [f"{ur.entry.name} · universe {ur.universe} · run {run_key}", ""]
    for a in analysis.axes:
        lines.append(f"  {'✓' if a.ran else '·'} {a.axis:<12} {a.note}")
    _emit(analysis.to_dict(), args.json, lines)
    return EXIT_OK if analysis.ran else EXIT_EMPTY


# ---------------------------------------------------------------------------
# ideas — step 1 of the framework
# ---------------------------------------------------------------------------

def cmd_ideas(args: argparse.Namespace) -> int:
    from .research.ideas import SOURCES, IdeaBook

    book = IdeaBook(args.ideas_file, holdout_dir=getattr(args, "holdout_dir", None)
                    or "research/ideas")
    action = args.ideas_action

    if action == "add":
        if args.source not in SOURCES:
            raise SystemExit(f"hermes: unknown --source {args.source!r}. "
                             f"Known: {', '.join(SOURCES)}")
        try:
            idea = book.add(args.title, hypothesis=args.hypothesis or "",
                            source=args.source, source_detail=args.detail or "",
                            tags=args.tag or [], parent=args.parent)
        except (ValueError, LookupError) as e:       # DuplicateIdea is a ValueError
            raise SystemExit(f"hermes: {e}") from e
        text = " ".join([idea.title, idea.hypothesis, *idea.tags])
        similar = [{"id": i.id, "title": i.title, "status": i.status, "score": score}
                   for i, score in book.similar(text, exclude=idea.id) if i.id in idea.similar_to]
        _emit({"idea": idea.to_dict(), "similar": similar}, args.json,
              [f"{idea.id}  {idea.label}",
               *[f"  similar {m['score']:.2f}  {m['id']}  [{m['status']}] {m['title']}"
                 for m in similar],
               "  next: quantify it into unambiguous rules, then "
               f"`hermes ideas set {idea.id} --status quantified`"])
        return EXIT_OK

    if action == "set":
        changes = {k: v for k, v in {
            "status": args.status, "hypothesis": args.hypothesis,
            "strategy": args.strategy, "verdict": args.verdict,
            "run_keys": args.run_key or None, "tags": args.tag or None,
            "informed_by": args.informed_by or None, "links": args.link or None,
            "failure": ({"kind": args.failure_kind, "assumption": args.failure_assumption or "",
                         "detail": args.failure_detail or ""} if args.failure_kind else None),
        }.items() if v}
        if not changes:
            raise SystemExit("hermes: nothing to change — pass --status/--hypothesis/"
                             "--strategy/--verdict/--run-key/--tag/--link")
        try:
            idea = book.update(args.id, reason=args.reason or "", **changes)
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
# trials — how many independent things were tried for an idea (ADR-0013)
# ---------------------------------------------------------------------------

def cmd_trials(args: argparse.Namespace) -> int:
    from .research.ideas import IdeaBook
    from .research.ledger import RunLedger
    from .research.trials import TrialRegistry

    book = IdeaBook(args.ideas_file, holdout_dir=args.out_dir)
    try:
        idea = book.get(args.idea)
    except LookupError as e:                      # KeyError is a LookupError
        raise SystemExit(f"hermes: {e}") from e
    registry = TrialRegistry(RunLedger(args.runs_dir), book)
    eff = registry.effective_trials(idea.id)
    payload = {
        "idea": idea.id, "own": eff.own, "inherited": eff.inherited,
        "batch_floor": eff.batch_floor, "n_eff": eff.n_eff,
        "clusters": [{"representative": c.representative, "keys": c.keys}
                     for c in registry.clusters(idea.id)],
    }
    out = Path(args.out_dir) / idea.id / "trials.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    lines = [f"{idea.id}  {idea.title}",
             f"  effective trials  {eff.n_eff}  "
             f"(own {eff.own}"
             + "".join(f" + {n} from {i}" for i, n in eff.inherited.items())
             + (f" + floor {eff.batch_floor}" if eff.batch_floor else "") + ")"]
    for i, c in enumerate(payload["clusters"], 1):
        lines.append(f"  cluster {i}: {len(c['keys'])} run(s), representative "
                     f"{c['representative']}")
    _emit(payload, args.json, lines)
    return EXIT_OK if eff.n_eff else EXIT_EMPTY


# ---------------------------------------------------------------------------
# holdout — earning and spending the single look (ADR-0012)
# ---------------------------------------------------------------------------

def _candidate(args: argparse.Namespace):
    """The idea and the stored trial run named on the command line, validated as a pair."""
    from .research.ideas import IdeaBook
    from .research.ledger import RunLedger

    ledger = RunLedger(args.runs_dir)
    try:
        rec = ledger.load(ledger.resolve(args.run_key))
    except LookupError as e:
        raise SystemExit(f"hermes: {e}") from e
    if rec is None:
        raise SystemExit(f"hermes: no run {args.run_key!r} in the ledger")
    book = IdeaBook(args.ideas_file, holdout_dir=args.out_dir)
    try:
        idea = book.get(args.idea)
    except LookupError as e:
        raise SystemExit(f"hermes: {e}") from e
    if rec.meta.idea != idea.id:
        raise SystemExit(f"hermes: run {rec.key} is not a trial of idea {idea.id} "
                         f"(it belongs to {rec.meta.idea!r})")
    return ledger, book, idea, rec


def _measure_gate(args: argparse.Namespace, ledger, book, idea, bt):
    """Re-run the candidate (guarded) and measure the in-sample gate on it."""
    from .research.analysis import DeflationInputs
    from .research.trials import TrialRegistry
    from .research.unlock import evaluate_gate

    _warn(f"measuring the gate for {idea.id} …")
    result = bt.run()
    registry = TrialRegistry(ledger, book)
    deflation = DeflationInputs(registry.effective_trials(idea.id),
                                registry.representative_sharpes(idea.id))
    return result, evaluate_gate(bt, result, deflation, null_n=args.null_n, seed=args.seed)


def cmd_holdout_check(args: argparse.Namespace) -> int:
    ledger, book, idea, rec = _candidate(args)
    _entry_, bt = _rebuild(args, rec)
    _result, gate = _measure_gate(args, ledger, book, idea, bt)
    payload = {"idea": idea.id, "run_key": rec.key, **gate.to_dict()}
    lines = [f"{idea.id}  {idea.title}", f"  in-sample gate: {'PASS' if gate.passed else 'FAIL'}"]
    for name, c in gate.checks.items():
        lines.append(f"  {'✓' if c['ok'] else '✗'} {name:<5} {c['value']}  (need {c['need']})")
    _emit(payload, args.json, lines)
    return EXIT_OK if gate.passed else EXIT_EMPTY


def cmd_holdout_unlock(args: argparse.Namespace) -> int:
    from .research.ledger import strategy_hash

    ledger, book, idea, rec = _candidate(args)
    if (Path(args.out_dir) / idea.id / "holdout.json").exists():
        raise SystemExit(f"hermes: idea {idea.id} has already looked at the holdout — one look "
                         f"per idea, even if the strategy has changed since")
    if "validated" not in book.legal_targets(idea):
        raise SystemExit(f"hermes: idea {idea.id} is {idea.status}; the holdout is only "
                         f"unlocked for an idea that is `testing`. Move it there first "
                         f"(`hermes ideas set {idea.id} --status testing`)")
    entry, bt = _rebuild(args, rec)
    if strategy_hash(entry.path) != rec.meta.strategy_hash:
        raise SystemExit(f"hermes: {entry.name} has changed since run {rec.key}; the holdout "
                         f"is only spent on the frozen strategy that was measured. Re-run it "
                         f"(and re-check) first")
    if bt.source.holdout_start(bt.symbol, base_timeframe(bt)) is None:
        raise SystemExit(f"hermes: no holdout was carved for {bt.symbol.ticker} (history too "
                         f"short), so there is nothing to unlock; the idea stays exploratory")
    _result, gate = _measure_gate(args, ledger, book, idea, bt)
    if not gate.passed:
        failed = "; ".join(f"{n}: {c['value']} (need {c['need']})"
                           for n, c in gate.checks.items() if not c["ok"])
        raise SystemExit(f"hermes: the in-sample gate failed — {failed}. The holdout is not "
                         f"spent on an idea that is unconvincing in-sample")

    # --- spend the look. Mark it burned BEFORE reading: a crash mid-run still counts. ---
    from .research.unlock import holdout_verdict

    tf = base_timeframe(bt)
    held = bt.source.holdout_start(bt.symbol, tf)
    prior = len(bt.source.looks(bt.symbol, tf))
    out = Path(args.out_dir) / idea.id / "holdout.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    record = {
        "idea": idea.id, "run_key": rec.key, "strategy": entry.name,
        "strategy_hash": rec.meta.strategy_hash, "params": rec.meta.params,
        "symbol": bt.symbol.ticker, "source": bt.source.name, "timeframe": str(tf),
        "window": {"start": held.isoformat(),
                   "end": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()},
        "unlocked_at": now.isoformat(), "state": "started", "passed": None,
        "in_sample_sharpe": rec.result.get("metrics", {}).get("sharpe"),
    }
    out.write_text(json.dumps(encode_nonfinite(record), indent=2, allow_nan=False) + "\n")
    bt.source.record_look(bt.symbol, tf, {"idea": idea.id, "run_key": rec.key,
                                          "at": now.isoformat()})

    _warn(f"reading the holdout {record['window']['start'][:10]} → "
          f"{record['window']['end'][:10]} (one look) …")
    unguarded = replace(bt, source=bt.source.inner, start=held,
                        end=datetime.fromisoformat(record["window"]["end"]))
    verdict = holdout_verdict(record["in_sample_sharpe"], unguarded.run(), prior)
    record.update(state="done", passed=verdict["passed"], **{
        k: verdict[k] for k in ("checks", "holdout", "required_psr", "prior_looks")})
    out.write_text(json.dumps(encode_nonfinite(record), indent=2, allow_nan=False) + "\n")

    if verdict["passed"]:
        book.update(idea.id, status="validated",
                    verdict=f"passed the holdout ({record['window']['start'][:10]} → "
                            f"{record['window']['end'][:10]}): PSR {verdict['holdout']['psr']:.2f}",
                    reason="holdout unlock")
    else:
        failed = ", ".join(n for n, c in verdict["checks"].items() if not c["ok"])
        book.update(idea.id, status="rejected", verdict=f"holdout_failed: {failed}",
                    reason="holdout unlock",
                    failure={"kind": "holdout_failed", "detail": failed,
                             "assumption": "the in-sample edge persists out of sample"})
    lines = [f"{idea.id}  holdout {'PASSED' if verdict['passed'] else 'FAILED'}"]
    for name, c in verdict["checks"].items():
        lines.append(f"  {'✓' if c['ok'] else '✗'} {name:<6} {c['value']}  (need {c['need']})")
    _emit(record, args.json, lines)
    return EXIT_OK if verdict["passed"] else EXIT_EMPTY


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
        rv.attach_analysis(rid, decode_nonfinite(json.loads(analysis_path.read_text())))
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
    p.add_argument("--universe", help="run over a universe: a tickers/<name>.json list or a "
                   "calendar universe (sp500). A build_universe_backtest file needs none")
    p.add_argument("--allow-advisor", action="store_true",
                   help="let a universe run make billed AI Advisor calls on every leg")
    p.add_argument("--note", help="why this run — the hypothesis it tests, or what "
                                   "changed since the last variant. Recorded in the ledger.")
    p.add_argument("--idea", help="the idea this run is evidence for (counted by `hermes trials`)")
    p.add_argument("--diagnostic", action="store_true",
                   help="this run explains, it does not select: not counted as a trial")
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
    p.add_argument("--ideas-file", default="research/ideas.json",
                   help="backlog file, for the deflation axis (default: research/ideas.json)")
    p.add_argument("--null-n", type=int, default=200,
                   help="time-shifted replays for the null axis (default 200)")
    p.add_argument("--baseline-n", type=int, default=50,
                   help="simulations for the `baseline` axis (default 50)")
    p.add_argument("--no-save", action="store_true", help="do not write analysis.json")
    # so `analyze <strategy-name>` can build a run like `hermes run` does
    p.add_argument("--symbol"), p.add_argument("--source")
    p.add_argument("--start"), p.add_argument("--end")
    p.add_argument("--cash", type=float)
    p.add_argument("-p", "--param", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--unconstrained", action="store_true")
    p.add_argument("--universe", help="analyse a universe run (see `hermes run --universe`)")
    p.add_argument("--allow-advisor", action="store_true",
                   help="allow re-running an AI-gated universe (billed calls)")
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
            q.add_argument("--parent", metavar="IDEA_ID",
                           help="the idea this one is spun out of; needed to re-propose "
                                "something close to a rejected idea")
        elif name == "set":
            q.add_argument("id", help="idea id, or a unique prefix")
            q.add_argument("--status",
                           help="one of the idea statuses; only the edges the pipeline allows "
                                "(validated needs a passed `hermes holdout unlock`)")
            q.add_argument("--reason", help="why (recorded in the idea's history)")
            q.add_argument("--link", action="append", default=[], metavar="URL",
                           help="a source link (repeatable); literature_done needs one")
            q.add_argument("--failure-kind", help="why it failed (rejected/needs_infra): "
                           "no_edge | cost_killed | regime_dependent | overfit | data_limit | "
                           "holdout_failed | duplicate | infra | other")
            q.add_argument("--failure-assumption", help="the assumption that did not hold")
            q.add_argument("--failure-detail", help="detail; for needs_infra, the missing capability")
            q.add_argument("--hypothesis")
            q.add_argument("--strategy", help="strategies/<name>.py this became")
            q.add_argument("--verdict", help="why it was validated or rejected")
            q.add_argument("--run-key", action="append", default=[],
                           help="link a ledger run (repeatable)")
            q.add_argument("--informed-by", action="append", default=[], metavar="IDEA_ID",
                           help="an idea whose results this one drew on; its trial "
                                "clusters count against this idea (repeatable)")
            q.add_argument("--tag", action="append", default=[])
            q.add_argument("--holdout-dir", default="research/ideas",
                           help="where <idea>/holdout.json lives (default: research/ideas)")
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

    p = sub.add_parser("trials", help="an idea's trial clusters and effective trials "
                                      "(the count a Sharpe is deflated by)")
    p.add_argument("idea", help="idea id (the one passed to `hermes run --idea`)")
    p.add_argument("--ideas-file", default="research/ideas.json",
                   help="backlog file (default: research/ideas.json)")
    p.add_argument("--out-dir", default="research/ideas",
                   help="where <idea>/trials.json is written (default: research/ideas)")
    _add_common(p)
    p.set_defaults(func=cmd_trials)

    p = sub.add_parser("holdout", help="the holdout: check the in-sample gate, then spend the "
                                       "single look")
    hsub = p.add_subparsers(dest="holdout_action", required=True)
    for name, helptext, func in [
        ("check", "measure the in-sample gate (burns nothing)", cmd_holdout_check),
        ("unlock", "spend the single look at the holdout", cmd_holdout_unlock),
    ]:
        q = hsub.add_parser(name, help=helptext)
        q.add_argument("idea", help="idea id")
        q.add_argument("--run-key", required=True, help="the trial run that is the candidate")
        q.add_argument("--ideas-file", default="research/ideas.json")
        q.add_argument("--null-n", type=int, default=200,
                       help="time-shifted replays for the null check (default 200)")
        q.add_argument("--seed", type=int, default=42)
        q.add_argument("--out-dir", default="research/ideas",
                       help="where <idea>/holdout.json is written (default: research/ideas)")
        _add_common(q)
        q.set_defaults(func=func)

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


def _load_env() -> None:
    """Read ``.env`` (cTrader/Tiingo/Polygon credentials) so no wrapper is needed. Real
    environment variables win over the file; a missing python-dotenv is not an error."""
    try:
        import dotenv
    except ImportError:
        return
    dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True))


def main(argv: list[str] | None = None) -> int:
    _load_env()
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
    except HoldoutViolation as e:
        _warn(f"hermes: {e}")
        return EXIT_ERR


if __name__ == "__main__":
    raise SystemExit(main())
