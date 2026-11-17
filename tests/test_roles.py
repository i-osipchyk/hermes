"""Phase 4 step 3: role definitions and the real runner's launch/parse, with no agent started."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from hermes.research.roles import (
    ROLE_NAMES,
    STEP_ROLE,
    ClaudeCodeRunner,
    load_role,
    parse_result,
)


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_every_role_definition_loads(role):
    rd = load_role(role)
    assert rd.role == role and rd.model in ("opus", "sonnet", "haiku") and rd.prompt


def test_the_models_are_the_agreed_split():
    assert {r: load_role(r).model for r in ROLE_NAMES} == {
        "researcher": "opus", "quant": "sonnet", "validator": "opus", "developer": "sonnet",
        "mechanical": "haiku"}


def test_the_validator_has_no_edit_tools_and_the_quant_cannot_read_verdicts():
    v, q = load_role("validator"), load_role("quant")
    assert {"Edit", "Write"} <= set(v.tools_deny) and not any(t.startswith("Write") for t in
                                                               v.tools_allow)
    assert any("validation" in t for t in q.tools_deny)
    assert any("strategies" in t for t in q.tools_allow)


def _cmds(prefixes, subs):
    return [f"Bash({p} {x}:*)" for p in prefixes for x in subs]


def test_only_the_developer_may_run_arbitrary_python():
    """Python in a role's shell is every guard bypassed at once: bars, ideas.json, verdicts."""
    for role in ROLE_NAMES:
        rd = load_role(role)
        has_python = any("python" in t or t == "Bash" for t in rd.tools_allow)
        assert has_python == (role == "developer"), role


def test_the_validator_may_read_and_judge_but_not_run_trials():
    v = load_role("validator")
    assert "Bash(hermes:*)" not in v.tools_allow and "Bash(.venv/bin/hermes:*)" not in v.tools_allow
    for sub in ("runs", "show", "analyze", "trials", "pipeline context", "pipeline validate",
                "pipeline write", "pipeline approve-plan"):
        assert f"Bash(hermes {sub}:*)" in v.tools_allow, sub
    for sub in ("run", "holdout", "ideas set"):
        assert f"Bash(hermes {sub}:*)" in v.tools_deny, sub
        assert f"Bash(.venv/bin/hermes {sub}:*)" in v.tools_deny, sub


def test_the_decisions_that_belong_to_other_roles_are_denied_by_name():
    q, r = load_role("quant"), load_role("researcher")
    for sub in ("holdout", "pipeline approve-plan", "pipeline answer", "pipeline validate"):
        assert f"Bash(hermes {sub}:*)" in q.tools_deny, sub
    for sub in ("run", "holdout", "pipeline approve-plan", "pipeline answer"):
        assert f"Bash(hermes {sub}:*)" in r.tools_deny, sub


def test_the_developer_never_pushes():
    assert "Bash(git push:*)" in load_role("developer").tools_deny


def test_every_step_has_a_role_that_exists():
    assert set(STEP_ROLE.values()) <= set(ROLE_NAMES)


def test_a_role_file_with_the_wrong_name_is_rejected(tmp_path):
    (tmp_path / "quant.md").write_text('---\nrole: researcher\nmodel: opus\n---\nbody')
    with pytest.raises(ValueError, match="frontmatter role"):
        load_role("quant", tmp_path)


def test_the_command_sets_model_tools_system_prompt_and_session():
    runner = ClaudeCodeRunner()
    cmd = runner.command(load_role("validator"), "do it", "sid-1")
    assert cmd[:3] == ["claude", "-p", "do it"]
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert "--output-format" in cmd and cmd[cmd.index("--session-id") + 1] == "sid-1"
    assert "--append-system-prompt" in cmd and "--allowedTools" in cmd
    assert cmd[cmd.index("--disallowedTools") + 1] == "Edit"


def test_the_subprocess_environment_carries_the_role_and_the_idea():
    env = ClaudeCodeRunner().env("quant", "abc123")
    assert (env["HERMES_ROLE"], env["HERMES_IDEA"], env["HERMES_RESEARCH"]) == (
        "quant", "abc123", "1")


def test_a_clean_result_is_parsed_with_tokens_and_session():
    out = json.dumps({"result": "done", "is_error": False, "session_id": "s9",
                      "usage": {"input_tokens": 10, "output_tokens": 5,
                                "cache_read_input_tokens": 100}})
    r = parse_result(out, "", 0, "opus")
    assert r.ok and r.tokens == 115 and r.session_id == "s9" and not r.paused


def test_a_usage_limit_is_a_pause_with_the_reset_time():
    out = json.dumps({"result": "Claude usage limit reached. Your limit resets at 3pm (Europe/Kyiv).",
                      "is_error": True})
    r = parse_result(out, "", 1)
    assert r.paused and not r.ok and r.reset_at.startswith("3pm")


def test_an_ordinary_error_is_a_failure_not_a_pause():
    r = parse_result(json.dumps({"result": "tool exploded", "is_error": True}), "", 1)
    assert not r.ok and not r.paused and "tool exploded" in r.error


def test_unparseable_output_with_a_nonzero_exit_is_a_failure():
    r = parse_result("garbage", "boom", 2)
    assert not r.ok and not r.paused


def test_run_launches_in_the_cwd_with_the_role_env_and_reports_the_result():
    seen = {}

    def fake(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return SimpleNamespace(stdout=json.dumps({"result": "ok", "session_id": "zz"}),
                               stderr="", returncode=0)

    r = ClaudeCodeRunner(runner=fake).run("quant", "i1", "write_plan", "prompt", cwd="/work")
    assert r.ok and r.session_id == "zz" and r.model == "sonnet"
    assert seen["cwd"] == "/work" and seen["env"]["HERMES_ROLE"] == "quant"
    assert seen["capture_output"] is True


def test_a_timeout_and_a_missing_cli_are_failures_not_crashes():
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    def missing(cmd, **kw):
        raise FileNotFoundError("claude")

    assert "timed out" in ClaudeCodeRunner(runner=slow).run("quant", "i", "s", "p").error
    assert "not installed" in ClaudeCodeRunner(runner=missing).run("quant", "i", "s", "p").error
