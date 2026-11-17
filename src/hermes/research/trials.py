"""Trials and trial clusters (ADR-0013, phase 2 step 2).

A **trial** is a ledger run that is a candidate for selection for an idea; a diagnostic
explains and is not counted. Near-identical trials are one **trial cluster**: nine
variants of one bet are one draw, not nine, when the Sharpe is deflated.
"""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .ideas import IdeaBook
from .ledger import RunLedger

CLUSTER_CORR = 0.9       # daily-return correlation at or above this -> same bet
CLUSTER_OVERLAP = 0.8    # shared (entry day, side) share at or above this -> same bet
MIN_OVERLAP_DAYS = 20    # fewer common days than this and correlation is not trusted
MIN_TRADES_FOR_SHARPE = 10   # the trade-level inference floor (audit F5)


@dataclass
class TrialCluster:
    keys: list[str] = field(default_factory=list)    # ledger run keys, earliest first

    @property
    def representative(self) -> str:
        return self.keys[0]


@dataclass(frozen=True)
class _Fingerprint:
    returns: dict[date, float]
    entries: frozenset[tuple[date, str]]


def _fingerprint(result: dict[str, Any]) -> _Fingerprint:
    last_per_day: dict[date, float] = {}
    for ts, equity in result.get("equity_curve") or []:
        last_per_day[datetime.fromisoformat(ts).date()] = float(equity)   # curve is ascending
    days = sorted(last_per_day)
    returns = {
        d1: last_per_day[d1] / last_per_day[d0] - 1.0
        for d0, d1 in zip(days, days[1:], strict=False)
        if last_per_day[d0]
    }
    entries = frozenset(
        (datetime.fromisoformat(t["entry_time"]).date(), t["side"])
        for t in result.get("trades") or [] if t.get("entry_time")
    )
    return _Fingerprint(returns, entries)


def _correlation(a: dict[date, float], b: dict[date, float]) -> float | None:
    common = sorted(a.keys() & b.keys())
    if len(common) < MIN_OVERLAP_DAYS:
        return None
    xs, ys = [a[d] for d in common], [b[d] for d in common]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)


def _overlap(a: frozenset, b: frozenset) -> float:
    # Against the larger set: a thin filter of a strategy is still the same bet, but a
    # mere subset claim must not merge clusters on a handful of coincidences.
    biggest = max(len(a), len(b))
    return len(a & b) / biggest if biggest else 0.0


def _same_bet(a: _Fingerprint, b: _Fingerprint) -> bool:
    corr = _correlation(a.returns, b.returns)
    if corr is not None and corr >= CLUSTER_CORR:
        return True
    return _overlap(a.entries, b.entries) >= CLUSTER_OVERLAP


@dataclass(frozen=True)
class EffectiveTrials:
    """How many independent things were tried to find an idea's result, and where each
    count came from, so the Validator can audit the deflation."""

    own: int
    inherited: dict[str, int]      # informing idea id -> its trial clusters
    batch_floor: int = 0

    @property
    def n_eff(self) -> int:
        return self.own + sum(self.inherited.values()) + self.batch_floor


BATCH_FLOOR = 0     # grows with the ideas processed in a pipeline run; none until that exists


def _trade_sharpe(result: dict[str, Any]) -> float | None:
    """Per-trade Sharpe (mean / population std of ``net_pnl / equity at entry``) of a stored
    run — the same unit the measured run's PSR/DSR use, so the two are comparable."""
    curve = [(datetime.fromisoformat(ts), float(eq)) for ts, eq in result.get("equity_curve") or []]
    times = [ts for ts, _ in curve]
    rets: list[float] = []
    for t in result.get("trades") or []:
        if not t.get("entry_time") or t.get("net_pnl") is None or not curve:
            continue
        # at-or-before the entry, clamped to the first point — as validation._equity_at does
        i = max(0, bisect.bisect_right(times, datetime.fromisoformat(t["entry_time"])) - 1)
        if curve[i][1] > 0:
            rets.append(t["net_pnl"] / curve[i][1])
    if len(rets) < MIN_TRADES_FOR_SHARPE:
        return None
    std = statistics.pstdev(rets)
    return statistics.fmean(rets) / std if std > 0 else None


class TrialRegistry:
    def __init__(self, ledger: RunLedger, ideas: IdeaBook | None = None):
        self.ledger = ledger
        self.ideas = ideas

    def effective_trials(self, idea: str) -> EffectiveTrials:
        """Own clusters + the clusters of every idea in its ``informed_by`` (undiscounted,
        not transitive) + the batch floor. An idea that nothing informed counts only itself."""
        informed_by = self.ideas.get(idea).informed_by if self.ideas else []
        return EffectiveTrials(
            own=len(self.clusters(idea)),
            inherited={i: len(self.clusters(i)) for i in informed_by},
            batch_floor=BATCH_FLOOR,
        )

    def clusters(self, idea: str) -> list[TrialCluster]:
        """The idea's trials grouped greedily, in ``created_at`` order, each compared with a
        cluster's representative (its first trial)."""
        trials = sorted(
            (e for e in self.ledger.entries() if e.meta.idea == idea and e.meta.kind == "trial"),
            key=lambda e: e.meta.created_at,
        )
        clusters: list[TrialCluster] = []
        reps: list[_Fingerprint] = []
        for entry in trials:
            record = self.ledger.load(entry.key)
            if record is None:
                continue
            fp = _fingerprint(record.result)
            for cluster, rep in zip(clusters, reps, strict=True):
                if _same_bet(fp, rep):
                    cluster.keys.append(entry.key)
                    break
            else:
                clusters.append(TrialCluster([entry.key]))
                reps.append(fp)
        return clusters

    def representative_sharpes(self, idea: str) -> list[float]:
        """Per-trade Sharpes of the cluster representatives — the idea's own, then those of
        every idea in ``informed_by`` — whose dispersion sets the DSR threshold. A cluster
        whose representative has too few trades to measure is left out."""
        informed_by = self.ideas.get(idea).informed_by if self.ideas else []
        out: list[float] = []
        for name in (idea, *informed_by):
            for cluster in self.clusters(name):
                record = self.ledger.load(cluster.representative)
                sharpe = _trade_sharpe(record.result) if record else None
                if sharpe is not None:
                    out.append(sharpe)
        return out
