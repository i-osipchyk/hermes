"""Drive a Claude Code review of a backtest result (ADR-0008, ADR-0010).

Instead of a billed Claude API call, we run Claude Code headlessly (``claude -p``) in
the repo so the ``hermes-analyze-results`` skill loads and the user's subscription auth
is used. The review is a background job: it writes ``review.md`` next to the run, and
the caller (the UI, or ``hermes review``) renders that file once it appears.

**The reviewer interprets evidence; it does not compute it.** Every quantitative axis of
the rubric — cost sensitivity, in/out-of-sample, statistical validation, regime split,
whether the AI gate was even in effect — is computed by
:mod:`hermes.research.analysis` and handed over as ``analysis.json`` via
:func:`attach_analysis`. That is why this runs with no Bash: an LLM shelling out to
work out whether an edge survives costs is both unnecessary and unauditable, now that
Hermes can answer it directly and identically every time.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .ledger import RestoredResult, restore_result

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


def load_result(rid: str) -> RestoredResult | None:
    """Reconstruct a result-shaped object from a stored ``result.json``."""
    path = _dir(rid) / "result.json"
    if not path.exists():
        return None
    return restore_result(json.loads(path.read_text()))


def review_path(rid: str) -> Path:
    return _dir(rid) / "review.md"


def analysis_path(rid: str) -> Path:
    return _dir(rid) / "analysis.json"


def attach_analysis(rid: str, analysis: dict) -> Path:
    """Place the computed rubric evidence where the reviewer will read it."""
    d = _dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    path = analysis_path(rid)
    path.write_text(json.dumps(analysis, indent=2, default=str))
    return path


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
    has_analysis = analysis_path(rid).exists()
    evidence = (
        f"The quantitative axes have already been COMPUTED for you in "
        f"{analysis_path(rid)} — cost sensitivity, in/out-of-sample, statistical "
        f"validation, regime split, P&L concentration, and whether the AI gate was in "
        f"effect. Each axis carries a `note` (a one-line reading) and `data` (the "
        f"numbers). Trust those numbers over your own estimation, cite them, and do not "
        f"claim an axis is unknown when it is present. An axis with `ran: false` says why "
        f"in `error` — report it as not measured, and say which command would measure it."
        if has_analysis else
        "No computed analysis was attached to this run, so the quantitative axes are "
        "UNMEASURED. Do not guess at them: say which are missing and that "
        "`hermes analyze <run-key>` would compute them."
    )
    return (
        "You are reviewing a Hermes backtest. Read the rubric in "
        ".claude/skills/hermes-analyze-results/SKILL.md, then the run in "
        f"{d / 'result.json'} (metrics, equity curve, trade blotter). "
        f"{evidence} "
        "You have no Bash — you are the interpreter, not the calculator. "
        "Write your verdict as concise markdown to "
        f"{review_path(rid)} using the Write tool: a headline verdict (is the edge real?), "
        "the single biggest threat to it, and the one change most worth trying next. "
        "Be specific and quantitative, and do not restate metrics without judging them."
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
