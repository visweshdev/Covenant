import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from covenant.integrations.claude_code import ClaudeCodeHook, git_actions, install, is_test_run
from covenant.runtime import Runtime

SRC = Path(__file__).resolve().parent.parent / "src"
TEST_CMD = "python -m pytest -q"


@pytest.fixture
def hook(mylib_repo, tmp_path):
    rt = Runtime.open(mylib_repo, state_dir=tmp_path / "state")
    h = ClaudeCodeHook(rt)
    h.handle({"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1"})
    return h


def pre(h, tool, **tool_input):
    return h.handle({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input})


def post(h, tool, ok=True, **tool_input):
    name = "PostToolUse" if ok else "PostToolUseFailure"
    return h.handle({"hook_event_name": name, "tool_name": tool, "tool_input": tool_input})


def denied(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def reason(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def test_command_parsing():
    assert git_actions("git commit -m x") == ["commit"]
    assert git_actions("cd a && git -C repo commit -am 'x'") == ["commit"]
    assert git_actions("git --no-pager push origin main") == ["push"]
    assert git_actions("git status && echo commit") == []
    assert is_test_run(TEST_CMD, [TEST_CMD]) == TEST_CMD
    assert is_test_run(f"cd . && {TEST_CMD}", [TEST_CMD]) == TEST_CMD
    assert is_test_run(f"{TEST_CMD} || true", [TEST_CMD]) is None
    assert is_test_run(f"{TEST_CMD} | tail", [TEST_CMD]) is None
    assert is_test_run("python -m pytest -q tests/test_one.py", [TEST_CMD]) is None


def test_session_start_tells_claude_the_rules(mylib_repo, tmp_path):
    rt = Runtime.open(mylib_repo, state_dir=tmp_path / "state")
    out = ClaudeCodeHook(rt).handle({"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1"})
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "don't change public APIs" in context and TEST_CMD in context
    assert rt.has_session


def test_commit_flow(hook, mylib_repo):
    core = mylib_repo / "mylib" / "core.py"
    post(hook, "Edit", file_path=str(core))

    out = pre(hook, "Bash", command="git commit -am 'bump'")
    assert denied(out) and "C3" in reason(out)

    post(hook, "Bash", ok=False, command=TEST_CMD)  # failing run does not count
    assert denied(pre(hook, "Bash", command="git commit -am 'bump'"))

    post(hook, "Bash", command=f"{TEST_CMD} || true")  # can hide failures: does not count
    assert denied(pre(hook, "Bash", command="git commit -am 'bump'"))

    post(hook, "Bash", command=TEST_CMD)
    assert pre(hook, "Bash", command="git commit -am 'bump'") == {}


def test_api_break_blocks_commit_and_stop(hook, mylib_repo):
    core = mylib_repo / "mylib" / "core.py"
    core.write_text(core.read_text().replace("def parse(text: str, strict: bool = False)", "def parse(text: str, *, mode: str = 'x')"))
    post(hook, "Edit", file_path=str(core))
    post(hook, "Bash", command=TEST_CMD)

    out = pre(hook, "Bash", command="git commit -am x")
    assert denied(out) and "C1" in reason(out) and "mylib.parse" in reason(out)

    stop = hook.handle({"hook_event_name": "Stop", "stop_hook_active": False})
    assert stop["decision"] == "block" and "C1" in stop["reason"]


def test_stop_hands_over_to_human_after_repeats(hook, mylib_repo):
    core = mylib_repo / "mylib" / "core.py"
    core.write_text(core.read_text().replace("def parse(text: str, strict: bool = False)", "def parse(text: str, *, mode: str = 'x')"))
    outs = [hook.handle({"hook_event_name": "Stop"}) for _ in range(5)]
    assert outs[0]["decision"] == "block"
    assert "decision" not in outs[4] and "need a human" in outs[4]["systemMessage"]


def test_protected_and_control_files(hook, mylib_repo):
    assert denied(pre(hook, "Write", file_path=str(mylib_repo / "covenant.yaml"), content="x"))
    assert denied(pre(hook, "Edit", file_path=str(mylib_repo / ".claude" / "settings.json")))
    assert denied(pre(hook, "Write", file_path=str(mylib_repo / ".git" / "hooks" / "pre-commit")))
    assert denied(pre(hook, "Write", file_path=str(hook.rt.state_dir / "trace.jsonl")))

    out = pre(hook, "Edit", file_path=str(mylib_repo / "data" / "users.json"))
    assert denied(out) and "C2" in reason(out)
    assert pre(hook, "Write", file_path=str(mylib_repo / "data" / "new.json"), content="{}") == {}
    assert pre(hook, "Edit", file_path=str(mylib_repo / "mylib" / "core.py")) == {}


@pytest.mark.parametrize(
    "command",
    ["rm covenant.yaml", "echo x > .git/hooks/pre-commit", "git commit --no-verify -m x",
     "git config core.hooksPath /dev/null", "sed -i s/C3// covenant.yaml"],
)
def test_bash_tampering_denied(hook, command):
    assert denied(pre(hook, "Bash", command=command))


def test_reading_control_files_is_fine(hook):
    assert pre(hook, "Bash", command="cat covenant.yaml") == {}


def test_deleting_data_from_bash_is_rolled_back_and_reported(hook, mylib_repo):
    users = mylib_repo / "data" / "users.json"
    before = users.read_text()
    users.unlink()  # what `python cleanup.py` did
    out = post(hook, "Bash", command="python cleanup.py")
    assert out["decision"] == "block" and "C2" in out["reason"]
    assert users.read_text() == before


def test_direct_tampering_is_detected(hook, mylib_repo):
    cov = mylib_repo / "covenant.yaml"
    cov.write_text(cov.read_text().replace("run tests before committing", "whatever"))
    out = pre(hook, "Bash", command="git commit -m x")
    assert denied(out) and "INTEGRITY" in reason(out)


def test_approved_copy_is_what_counts(hook, mylib_repo, tmp_path):
    # Dropping C3 from the repo file does not drop it from the running session.
    cov = mylib_repo / "covenant.yaml"
    text = cov.read_text()
    cov.write_text(text[: text.index("    - id: C3")])
    rt = Runtime.open(mylib_repo, state_dir=tmp_path / "state")
    assert "C3" in [c.id for c in rt.covenant.clauses]
    assert rt.repo_covenant_changed()


def test_new_session_does_not_approve_a_changed_file(hook, mylib_repo, tmp_path):
    cov = mylib_repo / "covenant.yaml"
    cov.write_text(cov.read_text() + "\n# edited\n")
    rt = Runtime.open(mylib_repo, state_dir=tmp_path / "state")
    out = ClaudeCodeHook(rt).handle({"hook_event_name": "SessionStart", "source": "startup", "session_id": "s2"})
    assert "changed since it was last approved" in out["systemMessage"]


def test_install_merges_and_is_idempotent(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]}]}}))
    install(tmp_path, settings, on_failure_block=True)
    install(tmp_path, settings, on_failure_block=True)
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    pre_groups = data["hooks"]["PreToolUse"]
    assert len(pre_groups) == 2 and pre_groups[0]["hooks"][0]["command"] == "mine"
    assert pre_groups[1]["hooks"][0]["onFailure"] == "block"
    assert set(data["hooks"]) == {"SessionStart", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"}


def test_hook_process_end_to_end(mylib_repo, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(SRC)}

    def call(payload):
        r = subprocess.run(
            [sys.executable, "-m", "covenant", "--repo", str(mylib_repo), "--state-dir", str(tmp_path / "s"), "claude-hook"],
            input=json.dumps(payload), capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout) if r.stdout.strip() else {}

    call({"hook_event_name": "SessionStart", "source": "startup", "session_id": "x"})
    call({"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": str(mylib_repo / "README.md")}})
    out = call({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git commit -m x"}})
    assert denied(out)


def test_hook_fails_closed(tmp_path):
    from covenant.integrations.claude_code import run_hook

    (tmp_path / "covenant.yaml").write_text("covenant: {not: valid}")
    out, code = run_hook(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}), tmp_path, str(tmp_path / "s"), None)
    assert code == 0 and denied(out) and "Failing closed" in reason(out)


def test_no_covenant_means_no_opinion(tmp_path):
    from covenant.integrations.claude_code import run_hook

    out, _ = run_hook(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}), tmp_path, None, None)
    assert out == {}


def test_parallel_hooks_do_not_lose_events(mylib_repo, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    state = tmp_path / "s"
    Runtime.open(mylib_repo, state_dir=state).start()
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "covenant", "--repo", str(mylib_repo), "--state-dir", str(state), "record", "edit", f"f{i}.py"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        for i in range(8)
    ]
    assert all(p.wait() == 0 for p in procs)
    assert len(Runtime.open(mylib_repo, state_dir=state).trace) == 8


def test_hook_command_runs_in_powershell_and_bash(tmp_path):
    """Claude Code may run shell-form hooks in PowerShell, where a leading quoted path is a syntax error."""
    from covenant.integrations.claude_code import hook_handler

    handler = hook_handler(tmp_path, None)
    if "args" in handler:  # interpreter path has spaces: exec form, no shell involved
        return
    assert not handler["command"].startswith('"')
    shells = [["bash", "-c"]]
    if sys.platform == "win32":
        shells.append(["powershell.exe", "-NoProfile", "-Command"])
    for shell in shells:
        r = subprocess.run(shell + [handler["command"]], input="{}", capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(SRC)})
        assert r.returncode == 0, (shell[0], r.stderr[-500:])


def test_powershell_tool_gets_the_same_checks(hook, mylib_repo):
    assert denied(pre(hook, "PowerShell", command="git commit -m x"))
    assert denied(pre(hook, "PowerShell", command="Remove-Item covenant.yaml"))
    assert denied(pre(hook, "PowerShell", command="git commit --no-verify -m x"))
    post(hook, "PowerShell", command=TEST_CMD)
    assert pre(hook, "PowerShell", command="git commit -m x") == {}
    users = mylib_repo / "data" / "users.json"
    users.unlink()
    out = post(hook, "PowerShell", command="rm data/users.json")
    assert out["decision"] == "block" and "restored" in out["reason"] and users.exists()


def test_installed_matchers_cover_powershell(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    install(tmp_path, settings, on_failure_block=False)
    hooks = json.loads(settings.read_text())["hooks"]
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        assert "PowerShell" in hooks[event][0]["matcher"].split("|")
