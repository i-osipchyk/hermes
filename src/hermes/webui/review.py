"""Drive a Claude Code review of a backtest result (ADR-0008).

Instead of a billed Claude API call, we run Claude Code headlessly (``claude -p``) in
the repo so the ``hermes-analyze-results`` skill loads and the user's subscription auth
is used. The review is a background job: it writes ``review.md`` to the run's cache dir,
and the page renders that file on refresh. Streamlit-free so it stays unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REVIEWS_DIR = Path(".hermes_cache/reviews")
_LAST_RID_FILE = REVIEWS_DIR / "last.rid"


def run_id(result_dict: dict) -> str:
    """Content-addressed id — identical results reuse the same review."""
    blob = json.dumps(result_dict, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _dir(rid: str) -> Path:
    return REVIEWS_DIR / rid


def save_last_rid(rid: str) -> None:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    _LAST_RID_FILE.write_text(rid)


def load_last_rid() -> str | None:
    return _LAST_RID_FILE.read_text().strip() if _LAST_RID_FILE.exists() else None


def write_result(result_dict: dict, rid: str) -> Path:
    d = _dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "result.json"
    path.write_text(json.dumps(result_dict, indent=2))
    return path


@dataclass(slots=True)
class _RestoredResult:
    """Lightweight stand-in for BacktestResult rebuilt from cached JSON."""
    metrics: object
    equity_curve: list
    _dict: dict

    def to_dict(self) -> dict:
        return self._dict


def load_result(rid: str) -> _RestoredResult | None:
    """Reconstruct a display-ready result object from a cached result.json."""
    path = _dir(rid) / "result.json"
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    from hermes.backtest.result import Metrics  # local import to avoid circular
    metrics = Metrics(**d["metrics"])
    equity_curve = [
        (datetime.fromisoformat(ts).replace(tzinfo=UTC), eq)
        for ts, eq in d.get("equity_curve", [])
    ]
    return _RestoredResult(metrics=metrics, equity_curve=equity_curve, _dict=d)


def review_path(rid: str) -> Path:
    return _dir(rid) / "review.md"


def read_review(rid: str) -> str | None:
    p = review_path(rid)
    return p.read_text() if p.exists() else None


@dataclass(frozen=True, slots=True)
class ReviewStatus:
    state: str          # "idle" | "running" | "done" | "failed"
    detail: str = ""


def status(rid: str) -> ReviewStatus:
    d = _dir(rid)
    if review_path(rid).exists():
        return ReviewStatus("done")
    exit_file = d / ".exit"
    if exit_file.exists():
        log = (d / "claude.log").read_text()[-800:] if (d / "claude.log").exists() else ""
        return ReviewStatus("failed", log)
    if (d / ".started").exists():
        return ReviewStatus("running")
    return ReviewStatus("idle")


def claude_available() -> bool:
    return shutil.which("claude") is not None


def _prompt(rid: str) -> str:
    d = _dir(rid)
    return (
        "You are reviewing a Hermes backtest. Read the rubric in "
        ".claude/skills/hermes-analyze-results/SKILL.md and the run in "
        f"{d / 'result.json'} (metrics, equity curve, trade blotter). Apply the rubric "
        "qualitatively over the provided data — you have no Bash, so where an axis needs a "
        "re-run (e.g. cost sensitivity, robustness) state what you would check rather than "
        "running it. Write your verdict as concise markdown (a headline verdict, the biggest "
        f"risk, and the single best next change) to {review_path(rid)} using the Write tool."
    )


def _command(rid: str) -> list[str]:
    return [
        "claude", "-p", _prompt(rid),
        "--allowedTools", "Read", "Glob", "Write",
        "--permission-mode", "acceptEdits",
    ]


def launch(rid: str, repo_dir: Path | None = None) -> None:
    """Fire the review as a detached background job. Status is read from the files it
    leaves behind (``review.md`` on success, ``.exit`` on completion)."""
    d = _dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".started").touch()
    for stale in (".exit",):
        (d / stale).unlink(missing_ok=True)
    # Wrap so the exit code is recorded even though the process is detached.
    quoted = " ".join(_shq(a) for a in _command(rid))
    wrapper = f"{quoted} > {_shq(str(d / 'claude.log'))} 2>&1; echo $? > {_shq(str(d / '.exit'))}"
    subprocess.Popen(
        wrapper, shell=True, cwd=str(repo_dir or Path.cwd()), start_new_session=True
    )


def manual_instructions(rid: str) -> str:
    """Copy-paste fallback: what to run in an interactive Claude Code session so the page
    picks up the same review.md."""
    return (
        "If the automatic review didn't run, paste this into a Claude Code session opened "
        "in the repo:\n\n"
        f"> Review the Hermes backtest at `{_dir(rid) / 'result.json'}` using "
        "`hermes-analyze-results`, and write the verdict as markdown to "
        f"`{review_path(rid)}`.\n\n"
        "The page will display it on the next refresh."
    )


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
