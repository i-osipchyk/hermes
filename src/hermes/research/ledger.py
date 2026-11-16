"""The run ledger: an input-keyed record of every backtest this project has run.

A backtest is a pure function of (Strategy + Parameters, data, config), so a hash of
those inputs identifies a run exactly. The ledger turns that into two questions an
autonomous researcher has to be able to answer:

* **"Have I already tried this?"** — :meth:`RunLedger.find`, which is what makes an
  agent's search over ideas resumable instead of amnesiac. A hit returns the stored
  result, so re-running a configuration costs nothing.
* **"What have I tried?"** — :meth:`RunLedger.entries`, the experiment log.

The key covers the strategy's **file hash**, so editing a strategy invalidates its
prior runs rather than silently serving a result the current code would not produce.

Layout::

    <root>/<key>/result.json          BacktestResult.to_dict()
                 meta.json            RunMeta — the inputs that produced it
                 llm_log.jsonl        per-call AI advisor log, when the run had one
                 universe_meta.json   universe-run extras, when applicable

Formerly ``hermes.webui.run_cache``; it is library surface, not UI surface (ADR-0010).
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .jsonsafe import decode_nonfinite, encode_nonfinite

DEFAULT_RUNS_DIR = Path(".hermes_cache/runs")

# Module-level default, kept because the Streamlit app and older code patch it.
RUNS_DIR = DEFAULT_RUNS_DIR


@dataclass
class RunMeta:
    """The inputs that produced a run — the ledger's row."""

    strategy: str
    strategy_hash: str   # SHA-256[:16] of strategy .py source
    source: str
    ticker: str | None
    universe: str | None
    start: str           # ISO-8601
    end: str
    params: dict
    sizer: str           # repr(sizer)
    unconstrained: bool
    created_at: str      # ISO-8601 UTC

    # Why this configuration was run — the hypothesis it tests, or what changed
    # since the last variant. Deliberately NOT part of the cache key: it is
    # intent, not an input, so two runs that differ only by note are one run.
    # Defaulted so rows written before this field still load.
    note: str = ""

    @property
    def label(self) -> str:
        scope = self.ticker or self.universe or "?"
        return f"{self.strategy} · {scope} · {self.start[:10]}–{self.end[:10]}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> RunMeta:
        # Tolerate rows written by older versions (and forward-compatibly, newer
        # ones): a ledger that refuses to read its own history is not a ledger.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A ledger row without its result payload — cheap to list."""

    key: str
    meta: RunMeta

    @property
    def label(self) -> str:
        return self.meta.label


@dataclass(frozen=True, slots=True)
class RunRecord:
    """A stored run: its key, its inputs, and its serialised result."""

    key: str
    meta: RunMeta
    result: dict                       # BacktestResult.to_dict()
    llm_log: Any | None = None         # LLMObservabilityLog, when the run had an advisor

    @property
    def metrics(self) -> dict:
        return self.result.get("metrics", {})

    @property
    def trades(self) -> list[dict]:
        return self.result.get("trades", [])

    @property
    def fail_open_calls(self) -> int:
        """Advisor calls that errored and approved anyway. Non-zero means this run's
        metrics describe a *less gated* strategy than the one configured."""
        return int(self.result.get("llm_summary", {}).get("fail_open_calls", 0) or 0)


_SIBLING = re.compile(r"""with_name\(\s*['"]([^'"]+)['"]\s*\)""")

# Packages whose code decides what a run produces. ``ai``/``research``/``webui`` do not
# change a simulation's numbers; the advisor's model is keyed separately.
_ENGINE_PACKAGES = ("backtest", "core", "data", "execution", "indicators", "strategy")


def _hash_with_siblings(path: Path, seen: set[Path]) -> list[bytes]:
    path = Path(path).resolve()
    if path in seen:
        return []
    seen.add(path)
    data = path.read_bytes()
    parts = [data]
    for name in sorted(set(_SIBLING.findall(data.decode("utf-8", errors="replace")))):
        sibling = path.with_name(name)
        if sibling.is_file():
            parts.append(name.encode())
            parts += _hash_with_siblings(sibling, seen)
    return parts


def strategy_hash(path: Path) -> str:
    """SHA-256[:16] of the strategy file bytes — changes when the file is edited.

    A variant that loads its base as a sibling (``Path(__file__).with_name("base.py")``,
    see CLAUDE.md) also covers that file, transitively: editing the base changes every
    variant's hash. A file with no sibling keeps its plain content hash.
    """
    parts = _hash_with_siblings(Path(path), set())
    if len(parts) == 1:
        return hashlib.sha256(parts[0]).hexdigest()[:16]
    h = hashlib.sha256()
    for part in parts:
        h.update(hashlib.sha256(part).digest())
    return h.hexdigest()[:16]


def _hash_engine(base: Path) -> str:
    h = hashlib.sha256()
    for pkg in _ENGINE_PACKAGES:
        for f in sorted((base / pkg).rglob("*.py")):
            h.update(str(f.relative_to(base)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


@functools.lru_cache(maxsize=1)
def _installed_engine_fingerprint() -> str:
    return _hash_engine(Path(__file__).resolve().parent.parent)


def engine_fingerprint(root: Path | None = None) -> str:
    """SHA-256[:16] of the simulation code (backtest, execution, data, …).

    Part of every :func:`cache_key`, so fixing the engine invalidates the runs it
    produced instead of serving them as valid. ``root`` is for tests; the installed
    package is hashed once per process.
    """
    return _installed_engine_fingerprint() if root is None else _hash_engine(Path(root))


def advisor_fingerprint(advisor: Any | None) -> str | None:
    """Identity of an AI Advisor for the cache key: its model and system prompt."""
    if advisor is None:
        return None
    model = getattr(getattr(advisor, "provider", None), "model_id", None) or type(advisor).__name__
    prompt = str(getattr(advisor, "system_prompt", ""))
    return f"{model}:{hashlib.sha256(prompt.encode()).hexdigest()[:8]}"


def source_epoch(source: Any, ticker: str) -> str | None:
    """A source's data epoch for ``ticker``; ``None`` for sources that do not define one
    (duck-typed sources in user strategy files need not subclass ``DataSource``)."""
    fn = getattr(source, "data_epoch", None)
    return fn(ticker) if callable(fn) else None


def file_hash(path: Path | str) -> str:
    """SHA-256[:16] of a file's bytes (e.g. a constituent calendar CSV)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def cache_key(
    strategy: str,
    strategy_hash: str,
    source: str,
    ticker: str | None,
    universe: str | None,
    start: str,
    end: str,
    params: dict,
    sizer: str,
    unconstrained: bool,
    leverage: float | None = None,
    starting_cash: float | None = None,
    advisor: str | None = None,
    universe_hash: str | None = None,
    data_epoch: str | None = None,
) -> str:
    """SHA-256[:16] of all inputs — any change produces a different key.

    Includes the engine code (:func:`engine_fingerprint`), so a fix to the simulation
    retires the runs it invalidates.
    """
    inputs = {
        "strategy": strategy,
        "strategy_hash": strategy_hash,
        "source": source,
        "ticker": ticker,
        "universe": universe,
        "start": start,
        "end": end,
        "params": params,
        "sizer": sizer,
        "unconstrained": unconstrained,
        "leverage": leverage,
        "starting_cash": starting_cash,
        "advisor": advisor,
        "universe_hash": universe_hash,
        "data_epoch": data_epoch,
        "engine": engine_fingerprint(),
    }
    blob = json.dumps(inputs, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


class AmbiguousKey(LookupError):
    """A key prefix matched more than one run."""


@dataclass
class RunLedger:
    """Read/write access to the stored runs under ``root``.

    ``root`` defaults to the module-level :data:`RUNS_DIR` at call time (not at
    import), so tests and alternate projects can repoint it.
    """

    root: Path | None = None
    _resolved: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._resolved = Path(self.root) if self.root is not None else Path(RUNS_DIR)

    @property
    def path(self) -> Path:
        return self._resolved

    def _dir(self, key: str) -> Path:
        return self._resolved / key

    # --- writing ------------------------------------------------------------

    def save(self, key: str, result: dict, meta: RunMeta, llm_log: Any | None = None) -> str:
        d = self._dir(key)
        d.mkdir(parents=True, exist_ok=True)
        (d / "result.json").write_text(json.dumps(encode_nonfinite(result), indent=2, allow_nan=False))
        (d / "meta.json").write_text(json.dumps(meta.to_dict(), indent=2))
        if llm_log is not None and getattr(llm_log, "total_calls", 0) > 0:
            llm_log.save(d / "llm_log.jsonl")
        return key

    def delete(self, key: str) -> None:
        d = self._dir(key)
        if d.exists():
            shutil.rmtree(d)

    # --- reading ------------------------------------------------------------

    def load(self, key: str) -> RunRecord | None:
        """The stored run for an exact key, or None."""
        from ..ai.observability import LLMObservabilityLog

        d = self._dir(key)
        result_path, meta_path = d / "result.json", d / "meta.json"
        if not result_path.exists() or not meta_path.exists():
            return None
        result = decode_nonfinite(json.loads(result_path.read_text()))
        meta = RunMeta.from_dict(json.loads(meta_path.read_text()))
        log = None
        log_path = d / "llm_log.jsonl"
        if log_path.exists():
            try:
                log = LLMObservabilityLog.load(log_path)
            except Exception:  # noqa: BLE001 — a corrupt log must not hide the result
                log = None
        return RunRecord(key=key, meta=meta, result=result, llm_log=log)

    def entries(
        self,
        *,
        strategy: str | None = None,
        ticker: str | None = None,
        limit: int | None = None,
        note_contains: str | None = None,
    ) -> list[LedgerEntry]:
        """Stored runs as (key, meta), newest first. Does not read result payloads."""
        if not self._resolved.exists():
            return []
        out: list[LedgerEntry] = []
        for d in self._resolved.iterdir():
            if not d.is_dir():
                continue
            meta_path = d / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = RunMeta.from_dict(json.loads(meta_path.read_text()))
            except Exception:  # noqa: BLE001 — skip unreadable rows, don't fail the listing
                continue
            if strategy and meta.strategy != strategy:
                continue
            if ticker and meta.ticker != ticker:
                continue
            if note_contains and note_contains.lower() not in (meta.note or "").lower():
                continue
            out.append(LedgerEntry(key=d.name, meta=meta))
        out.sort(key=lambda e: e.meta.created_at, reverse=True)
        return out[:limit] if limit else out

    def resolve(self, key_or_prefix: str) -> str:
        """Expand a unique key prefix to a full key (so a CLI can take 4 characters).

        Raises :class:`KeyError` if nothing matches, :class:`AmbiguousKey` if several do.
        """
        if (self._dir(key_or_prefix) / "meta.json").exists():
            return key_or_prefix
        hits = [e.key for e in self.entries() if e.key.startswith(key_or_prefix)]
        if not hits:
            raise KeyError(f"no run matching {key_or_prefix!r}")
        if len(hits) > 1:
            raise AmbiguousKey(f"{key_or_prefix!r} matches {len(hits)} runs: {', '.join(hits)}")
        return hits[0]

    def find(self, **inputs: Any) -> RunRecord | None:
        """"Have I already run this?" — the stored run for these inputs, or None.

        Takes the same keyword arguments as :func:`cache_key`.
        """
        return self.load(cache_key(**inputs))

    # --- universe-run extras -------------------------------------------------

    def save_universe_meta(
        self,
        key: str,
        universe_size: int,
        summary_rows: list[dict],
        per_symbol_counts: dict[str, int],
        skipped: list[str] | None = None,
        legs_requested: int | None = None,
    ) -> None:
        d = self._dir(key)
        d.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {
            "universe_size": universe_size,
            "summary_rows": summary_rows,
            "per_symbol_counts": per_symbol_counts,
        }
        if legs_requested is not None:
            skipped = list(skipped or [])
            meta["legs_requested"] = legs_requested
            meta["skipped"] = skipped
            meta["coverage"] = ((legs_requested - len(skipped)) / legs_requested
                                if legs_requested else None)
        (d / "universe_meta.json").write_text(
            json.dumps(encode_nonfinite(meta), indent=2, allow_nan=False))

    def load_universe_meta(self, key: str) -> dict | None:
        path = self._dir(key) / "universe_meta.json"
        return decode_nonfinite(json.loads(path.read_text())) if path.exists() else None


# ---------------------------------------------------------------------------
# Module-level API over a default ledger. Kept so the Streamlit app and any
# existing callers keep working; new code should take a RunLedger.
# ---------------------------------------------------------------------------

def _default() -> RunLedger:
    return RunLedger()


def save_run(key: str, result_dict: dict, meta: RunMeta, llm_log: Any | None = None) -> None:
    _default().save(key, result_dict, meta, llm_log)


def load_run(key: str) -> tuple[dict, RunMeta] | None:
    rec = _default().load(key)
    if rec is None:
        return None
    result = dict(rec.result)
    if rec.llm_log is not None:
        result["_llm_log"] = rec.llm_log
    return result, rec.meta


def list_runs() -> list[tuple[str, RunMeta]]:
    return [(e.key, e.meta) for e in _default().entries()]


def delete_run(key: str) -> None:
    _default().delete(key)


def save_universe_meta(key: str, universe_size: int, summary_rows: list[dict],
                       per_symbol_counts: dict[str, int], skipped: list[str] | None = None,
                       legs_requested: int | None = None) -> None:
    _default().save_universe_meta(key, universe_size, summary_rows, per_symbol_counts,
                                  skipped, legs_requested)


def load_universe_meta(key: str) -> dict | None:
    return _default().load_universe_meta(key)


# ---------------------------------------------------------------------------
# Restoring a saved run into something result-shaped (for display / re-analysis)
# ---------------------------------------------------------------------------

@dataclass
class RestoredResult:
    """A stand-in for ``BacktestResult`` rebuilt from a stored ``result.json``.

    Carries the fields readers actually use — metrics, equity curve, the raw dict —
    without re-running the backtest. It is **not** a BacktestResult: ``trades`` are
    dicts, not ``Trade`` objects, so anything needing real Trades must re-run.
    """

    metrics: Any
    equity_curve: list[tuple[datetime, float]]
    _dict: dict
    stat_validation: Any = None

    def to_dict(self) -> dict:
        return self._dict


def restore_result(result_dict: dict) -> RestoredResult:
    from ..backtest.result import Metrics

    return RestoredResult(
        metrics=Metrics(**result_dict["metrics"]),
        equity_curve=[
            (datetime.fromisoformat(ts).replace(tzinfo=UTC), eq)
            for ts, eq in result_dict.get("equity_curve", [])
        ],
        _dict=result_dict,
        stat_validation=result_dict.get("stat_validation"),
    )


@dataclass
class _CachedPortfolioResult:
    """Lightweight stand-in for PortfolioResult restored from the ledger."""

    result: object
    _summary_rows: list
    per_symbol: dict                # ticker -> list of `n` Nones (count only)

    def summary_rows(self) -> list:
        return self._summary_rows


@dataclass
class CachedUniverseResult:
    """Stand-in for UniverseResult when restoring from the ledger."""

    portfolio_result: _CachedPortfolioResult
    universe_size: int

    def summary_rows(self) -> list:
        return self.portfolio_result.summary_rows()


def restore_universe_result(result_dict: dict, uni_meta: dict) -> CachedUniverseResult:
    """Rebuild a CachedUniverseResult from stored dicts for display."""
    r = restore_result(result_dict)
    per_symbol = {k: [None] * v for k, v in uni_meta["per_symbol_counts"].items()}
    return CachedUniverseResult(
        portfolio_result=_CachedPortfolioResult(
            result=r,
            _summary_rows=uni_meta["summary_rows"],
            per_symbol=per_symbol,
        ),
        universe_size=uni_meta["universe_size"],
    )
