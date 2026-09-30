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

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

    @property
    def label(self) -> str:
        scope = self.ticker or self.universe or "?"
        return f"{self.strategy} · {scope} · {self.start[:10]}–{self.end[:10]}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> RunMeta:
        return cls(**d)


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


def strategy_hash(path: Path) -> str:
    """SHA-256[:16] of the strategy file bytes — changes when the file is edited."""
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
) -> str:
    """SHA-256[:16] of all inputs — any change produces a different key."""
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
        (d / "result.json").write_text(json.dumps(result, indent=2))
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
        result = json.loads(result_path.read_text())
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
    ) -> None:
        d = self._dir(key)
        d.mkdir(parents=True, exist_ok=True)
        (d / "universe_meta.json").write_text(json.dumps({
            "universe_size": universe_size,
            "summary_rows": summary_rows,
            "per_symbol_counts": per_symbol_counts,
        }, indent=2))

    def load_universe_meta(self, key: str) -> dict | None:
        path = self._dir(key) / "universe_meta.json"
        return json.loads(path.read_text()) if path.exists() else None


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
                       per_symbol_counts: dict[str, int]) -> None:
    _default().save_universe_meta(key, universe_size, summary_rows, per_symbol_counts)


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
