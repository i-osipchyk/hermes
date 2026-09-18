"""Input-keyed run cache for Hermes backtest results.

Cache key = SHA-256[:16] of (strategy_name, strategy_file_hash, source,
ticker/universe, start, end, params, sizer_repr, unconstrained).

Storage: .hermes_cache/runs/<key>/result.json + meta.json
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

RUNS_DIR = Path(".hermes_cache/runs")


@dataclass
class RunMeta:
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


def strategy_hash(path: Path) -> str:
    """SHA-256[:16] of the strategy file bytes — changes when file is edited."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


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
    }
    blob = json.dumps(inputs, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _run_dir(key: str) -> Path:
    return RUNS_DIR / key


def save_run(key: str, result_dict: dict, meta: RunMeta) -> None:
    d = _run_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps(result_dict, indent=2))
    (d / "meta.json").write_text(json.dumps(meta.to_dict(), indent=2))


def load_run(key: str) -> tuple[dict, RunMeta] | None:
    d = _run_dir(key)
    result_path = d / "result.json"
    meta_path = d / "meta.json"
    if not result_path.exists() or not meta_path.exists():
        return None
    result_dict = json.loads(result_path.read_text())
    meta = RunMeta.from_dict(json.loads(meta_path.read_text()))
    return result_dict, meta


def list_runs() -> list[tuple[str, RunMeta]]:
    """Return all saved runs as (key, meta), newest first."""
    if not RUNS_DIR.exists():
        return []
    runs = []
    for d in RUNS_DIR.iterdir():
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = RunMeta.from_dict(json.loads(meta_path.read_text()))
            runs.append((d.name, meta))
        except Exception:
            continue
    runs.sort(key=lambda x: x[1].created_at, reverse=True)
    return runs


def delete_run(key: str) -> None:
    import shutil
    d = _run_dir(key)
    if d.exists():
        shutil.rmtree(d)


# ---------------------------------------------------------------------------
# Universe run extras
# ---------------------------------------------------------------------------

def save_universe_meta(key: str, universe_size: int, summary_rows: list[dict],
                       per_symbol_counts: dict[str, int]) -> None:
    """Save universe-specific display data alongside the run."""
    d = _run_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "universe_size": universe_size,
        "summary_rows": summary_rows,
        "per_symbol_counts": per_symbol_counts,
    }
    (d / "universe_meta.json").write_text(json.dumps(payload, indent=2))


def load_universe_meta(key: str) -> dict | None:
    path = _run_dir(key) / "universe_meta.json"
    return json.loads(path.read_text()) if path.exists() else None


@dataclass
class _CachedPortfolioResult:
    """Lightweight stand-in for PortfolioResult restored from cache."""
    result: object                  # restored BacktestResult-like object
    _summary_rows: list
    per_symbol: dict                # ticker -> list of `n` Nones (count only)

    def summary_rows(self) -> list:
        return self._summary_rows


@dataclass
class CachedUniverseResult:
    """Stand-in for UniverseResult when restoring from cache."""
    portfolio_result: _CachedPortfolioResult
    universe_size: int

    def summary_rows(self) -> list:
        return self.portfolio_result.summary_rows()


def restore_universe_result(result_dict: dict, uni_meta: dict) -> CachedUniverseResult:
    """Rebuild a CachedUniverseResult from saved dicts for display."""
    from datetime import UTC, datetime
    from hermes.backtest.result import Metrics

    metrics = Metrics(**result_dict["metrics"])
    equity_curve = [
        (datetime.fromisoformat(ts).replace(tzinfo=UTC), eq)
        for ts, eq in result_dict.get("equity_curve", [])
    ]

    class _R:
        pass

    r = _R()
    r.metrics = metrics
    r.equity_curve = equity_curve
    # Do NOT set r.trades — _split_result uses getattr(result, "trades", []) and
    # expects Trade objects; to_dict() returns the full dict including trades for display.
    r.stat_validation = result_dict.get("stat_validation")

    def to_dict():
        return result_dict

    r.to_dict = to_dict

    per_symbol = {k: [None] * v for k, v in uni_meta["per_symbol_counts"].items()}
    pr = _CachedPortfolioResult(
        result=r,
        _summary_rows=uni_meta["summary_rows"],
        per_symbol=per_symbol,
    )
    return CachedUniverseResult(portfolio_result=pr, universe_size=uni_meta["universe_size"])
