"""A project in ``tmp_path`` and scripted roles that walk an idea through the pipeline,
writing through the real guards (permissions, plan lock, transitions)."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta

from hermes.research.artifacts import Lessons, Validation
from hermes.research.data_check import DataCheck, DataRow
from hermes.research.driver import Driver, DriverConfig, ScriptedRunner, role_env
from hermes.research.ledger import RunLedger, RunMeta
from tests.artifact_helpers import good_plan, good_spec

T0 = datetime(2023, 1, 2, tzinfo=UTC)


def save_run(ledger: RunLedger, key: str, idea: str, variant: str | None, seed: int,
             kind: str = "trial") -> None:
    rng, eq, equity = random.Random(seed), 10_000.0, []
    for i in range(120):
        eq *= 1 + rng.gauss(0.0004, 0.01)
        equity.append(((T0 + timedelta(days=i, hours=20)).isoformat(), eq))
    meta = RunMeta(strategy="s1", strategy_hash="h", source="memory", ticker="T", universe=None,
                   start=T0.isoformat(), end=(T0 + timedelta(days=120)).isoformat(), params={},
                   sizer="", unconstrained=False, created_at=(T0 + timedelta(minutes=seed)).isoformat(),
                   idea=idea, kind=kind, variant=variant)
    trades = [{"side": "sell", "entry_time": (T0 + timedelta(days=seed % 50 + 7 * j)).isoformat()}
              for j in range(10)]
    ledger.save(key, {"equity_curve": equity, "trades": trades, "metrics": {"sharpe": 0.8}}, meta)


class Project:
    def __init__(self, tmp_path, *, runner=None, **driver_kw):
        self.tmp = tmp_path
        self.cfg = DriverConfig(
            ideas_file=tmp_path / "ideas.json", ideas_dir=tmp_path / "ideas",
            approvals_file=tmp_path / "approvals.json", thesis_file=tmp_path / "thesis.md",
            runs_dir=tmp_path / "runs", strategies_dir=tmp_path / "strategies",
            repo=tmp_path / "repo")
        (tmp_path / "strategies").mkdir(parents=True, exist_ok=True)
        self.runner = runner or ScriptedRunner(self.happy_scripts())
        self.data_verdict = "ok"
        self.driver = Driver(self.cfg, self.runner, unlocker=self.unlock, filler=self.fill,
                             data_checker=self.data_check, **driver_kw)
        self.book, self.store = self.driver.book, self.driver.store
        self.ledger, self.approvals = self.driver.ledger, self.driver.approvals
        self.unlocked: list[str] = []
        self.recommendation = "ready_for_unlock"

    def idea(self, **kw) -> str:
        return self.book.add("gold london break", source="discretionary", **kw).id

    # --- the scripted roles ----------------------------------------------------------

    def happy_scripts(self) -> dict:
        return {
            ("researcher", "write_spec"): lambda i, p: self.store.write(i, "spec", good_spec()),
            ("quant", "write_plan"): lambda i, p: self.store.write(i, "plan", good_plan()),
            ("validator", "review_plan"):
                lambda i, p: self.store.approve_plan(i, reviewer="v"),
            ("quant", "write_strategy"): self._write_strategy,
            ("quant", "run_variants"): self._run_variants,
            ("validator", "validate"): lambda i, p: self.store.write(i, "validation", Validation(
                verdict=self.recommendation, rationale="the gate reads as recommended",
                failure={"kind": "no_edge", "assumption": "momentum persists"}
                if self.recommendation == "reject" else None,
                override_reason="")),
            ("researcher", "reflect"): lambda i, p: self.store.write(
                i, "lessons", Lessons(outcome="done")),
        }

    def _write_strategy(self, idea, prompt):
        (self.tmp / "strategies" / "s1.py").write_text("# strategy\n")
        self.book.update(idea, strategy="s1")

    def _run_variants(self, idea, prompt):
        plan = self.store.read(idea, "plan")
        for n, v in enumerate(plan.variants):
            save_run(self.ledger, f"{idea}-{v['id']}", idea, v["id"], 100 + 37 * n)

    # --- the driver's seams ----------------------------------------------------------

    def data_check(self, spec):
        state = {"ok": "cached", "owner_action": "needs_credentials",
                 "needs_infra": "unavailable"}[self.data_verdict]
        return DataCheck(self.data_verdict, [DataRow("XAUUSD", "1h", state, detail="test")])

    def fill(self, idea, driver):
        return Validation(recommendation=self.recommendation, recommendation_reason="test",
                          candidate_run=f"{idea}-v1", tier="credible")

    def unlock(self, idea, run_key):
        self.unlocked.append(run_key)
        path = self.cfg.ideas_dir / idea / "holdout.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"state": "done", "passed": True}))
        with role_env("driver"):
            self.book.update(idea, status="validated", verdict="held out")

    def history(self, idea):
        return [(h["from"], h["to"], h["by"]) for h in self.book.get(idea).history]

    def run_log(self, idea):
        path = self.cfg.ideas_dir / idea / "run.jsonl"
        return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []
