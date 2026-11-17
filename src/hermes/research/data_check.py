"""The data check between the spec and ``quantified``.

Before a Quant spends a plan, a strategy and a review on an idea, ask whether the data the spec
names can be had — and whose problem it is when it cannot. Three answers, worst wins:

* ``ok`` — every (instrument, timeframe) is cached, probed, or at least offered by a source
  whose credentials are set (``unprobed`` says so: *unverified*, not confirmed);
* ``owner_action`` — a source that could serve it needs credentials that are not set. Only the
  owner can fix that, so the driver parks an approval rather than calling a Developer;
* ``needs_infra`` — no source offers the timeframe, or every reachable source was probed and
  does not have the instrument. That is a capability a Developer can build (or an instrument
  the owner can change).

Hermes computes, so the check is deterministic and offline by default; a probe (one week of
bars from one source) is opt-in and injected, which keeps it testable and keeps network out of
the tests.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..core import Timeframe

# Env vars a source needs before it can fetch. cTrader's token pair comes from SSM at fetch
# time and cannot be checked offline; the account id is what the source cannot do without.
CREDENTIALS: dict[str, tuple[str, ...]] = {
    "pepperstone": ("CTRADER_ACCOUNT_ID",),
    "tiingo": ("TIINGO_API_KEY",),
    "polygon": ("POLYGON_API_KEY",),
}
PROBE_START = datetime(2023, 1, 2, tzinfo=UTC)
PROBE_SPAN = timedelta(days=7)
_RANK = {"ok": 0, "needs_infra": 1, "owner_action": 2}      # owner_action first: cheapest to fix


@dataclass
class DataRow:
    instrument: str
    timeframe: str
    state: str          # cached | probed | unprobed | needs_credentials | unavailable | not_found
    source: str = ""
    first: str | None = None
    last: str | None = None
    bars: int | None = None
    detail: str = ""


@dataclass
class DataCheck:
    verdict: str = "ok"
    rows: list[DataRow] = field(default_factory=list)
    checked_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return "; ".join(f"{r.instrument} {r.timeframe}: {r.state}"
                         + (f" ({r.detail})" if r.detail else "") for r in self.rows)


def _cached(cache_root: Path, ticker: str, tf: Timeframe) -> tuple[str, str, str, int] | None:
    """(source, first, last, bars) of the first cache file for this ticker and timeframe."""
    for path in sorted(Path(cache_root).glob(f"*/{ticker}_{tf}.parquet")):
        import pandas as pd

        ts = pd.read_parquet(path, columns=["ts"])["ts"]
        if ts.empty:
            continue
        stamp = lambda v: datetime.fromtimestamp(int(v), tz=UTC).isoformat()  # noqa: E731
        return path.parent.name, stamp(ts.min()), stamp(ts.max()), int(len(ts))
    return None


def _row(ticker: str, tf: Timeframe, cache_root: Path, env: Mapping[str, str],
         sources: Mapping[str, Any], credentials: Mapping[str, tuple[str, ...]],
         probe: Callable[[str, str, Timeframe], int] | None) -> DataRow:
    hit = _cached(cache_root, ticker, tf)
    if hit:
        source, first, last, bars = hit
        return DataRow(ticker, str(tf), "cached", source, first, last, bars)
    offering = [n for n, s in sources.items() if tf in s.supported_timeframes()]
    if not offering:
        return DataRow(ticker, str(tf), "unavailable", detail=f"no source offers {tf} bars")
    missing = {n: [v for v in credentials.get(n, ()) if not env.get(v)] for n in offering}
    ready = [n for n in offering if not missing[n]]
    gated = {n: v for n, v in missing.items() if v}
    if not ready:
        need = "; ".join(f"{n} needs {', '.join(v)}" for n, v in gated.items())
        return DataRow(ticker, str(tf), "needs_credentials", detail=need)
    if probe is None:
        return DataRow(ticker, str(tf), "unprobed", ready[0],
                       detail=f"offered by {', '.join(ready)}; not probed, so unverified")
    errors = []
    for name in ready:
        try:
            return DataRow(ticker, str(tf), "probed", name, bars=int(probe(name, ticker, tf)))
        except Exception as e:  # noqa: BLE001 — any failure of a probe is a finding, not a crash
            errors.append(f"{name}: {e}")
    detail = "; ".join(errors)
    if gated:
        detail += "; untried (credentials not set): " + "; ".join(
            f"{n} needs {', '.join(v)}" for n, v in gated.items())
        return DataRow(ticker, str(tf), "needs_credentials", detail=detail)
    return DataRow(ticker, str(tf), "not_found", detail=detail)


def check_spec_data(spec: Any, *, cache_root: Path | str = ".hermes_cache/bars",
                    env: Mapping[str, str] | None = None,
                    sources: Mapping[str, Any] | None = None,
                    credentials: Mapping[str, tuple[str, ...]] | None = None,
                    probe: Callable[[str, str, Timeframe], int] | None = None) -> DataCheck:
    env = os.environ if env is None else env
    if sources is None:
        from .sources import build_source, source_names
        sources = {n: build_source(n) for n in source_names()}
    credentials = CREDENTIALS if credentials is None else credentials
    rows = [_row(ticker, Timeframe.parse(tf), Path(cache_root), env, sources, credentials, probe)
            for ticker in spec.instruments for tf in spec.timeframes]
    worst = "ok"
    for r in rows:
        v = {"needs_credentials": "owner_action", "unavailable": "needs_infra",
             "not_found": "needs_infra"}.get(r.state, "ok")
        if _RANK[v] > _RANK[worst]:
            worst = v
    return DataCheck(worst, rows, datetime.now(UTC).isoformat())


def data_check_path(ideas_dir: Path | str, idea_id: str) -> Path:
    return Path(ideas_dir) / idea_id / "data_check.json"


def read_data_check(ideas_dir: Path | str, idea_id: str) -> dict | None:
    path = data_check_path(ideas_dir, idea_id)
    return json.loads(path.read_text()) if path.exists() else None


def write_data_check(ideas_dir: Path | str, idea_id: str, check: DataCheck) -> Path:
    path = data_check_path(ideas_dir, idea_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(check.to_dict(), indent=2, allow_nan=False) + "\n")
    return path


def real_probe(source_name: str, ticker: str, tf: Timeframe) -> int:
    """One week of bars from one source: proves the instrument resolves and the timeframe is
    served. Costs a small fetch (and caches it); never reads near a holdout."""
    from ..core import Symbol
    from .sources import build_source

    src = build_source(source_name)
    inst = src.get_instrument(Symbol(ticker, source_name))
    bars = src.history(inst, tf, PROBE_START, PROBE_START + PROBE_SPAN)
    if not bars:
        raise LookupError(f"{source_name} returned no {tf} bars for {ticker} in "
                          f"{PROBE_START:%Y-%m-%d}..{(PROBE_START + PROBE_SPAN):%Y-%m-%d}")
    return len(bars)
