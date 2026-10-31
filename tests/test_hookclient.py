"""The fast hook client: daemon when there is one, the full hook otherwise, never a skipped check."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from covenant import server
from covenant.integrations.claude_code import fast_hook_args, install

CLIENT = Path(server.__file__).parent / "hookclient.py"
COMMIT = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}


def run_client(repo, state, payload):
    args = [sys.executable] + fast_hook_args(repo, state)
    env = {**os.environ, "COVENANT_NO_DAEMON": "1"}  # don't leave daemons behind from tests
    r = subprocess.run(args, input=json.dumps({"cwd": str(repo), **payload}), capture_output=True, text=True, env=env)
    return r.returncode, (json.loads(r.stdout) if r.stdout.strip() else {})


def denied(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.fixture
def started(mylib_repo, tmp_path):
    from covenant.runtime import Runtime

    state = tmp_path / "state"
    Runtime.open(mylib_repo, state_dir=state).start()
    (mylib_repo / "mylib" / "core.py").write_text((mylib_repo / "mylib" / "core.py").read_text() + "\n# edit\n")
    from covenant.events import Edit

    Runtime.open(mylib_repo, state_dir=state).record(Edit(path="mylib/core.py"))
    return mylib_repo, state


def test_without_a_daemon_the_full_hook_runs(started):
    repo, state = started
    code, out = run_client(repo, state, COMMIT)
    assert code == 0 and denied(out) and "C3" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_with_a_daemon_the_same_answer_comes_back(started):
    repo, state = started
    httpd = server.HookServer(("127.0.0.1", 0), repo, None, str(state))
    (state / "daemon.json").write_text(json.dumps({"port": httpd.server_address[1], "token": httpd.token, "pid": 0}))
    seen = []
    original = server.Handler.do_POST
    server.Handler.do_POST = lambda self: (seen.append(self.path), original(self))[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        code, out = run_client(repo, state, COMMIT)
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.Handler.do_POST = original
    assert seen == ["/hook"]
    assert code == 0 and denied(out)


def test_a_broken_daemon_falls_back(started):
    repo, state = started
    (state / "daemon.json").write_text(json.dumps({"port": 1, "token": "x", "pid": 0}))  # nothing listens there
    code, out = run_client(repo, state, COMMIT)
    assert code == 0 and denied(out)
    (state / "daemon.json").write_text("not json")
    assert denied(run_client(repo, state, COMMIT)[1])


def test_install_uses_exec_form_fast_hooks(mylib_repo, tmp_path):
    settings = install(mylib_repo, mylib_repo / ".claude" / "settings.json", tmp_path / "state", on_failure_block=True)
    handler = json.loads(settings.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]
    assert handler["command"] == Path(sys.executable).as_posix()  # no shell in between
    assert handler["args"][:3] == ["-I", "-S", CLIENT.resolve().as_posix()] and handler["onFailure"] == "block"
    # Re-installing replaces our hook instead of adding a second one.
    install(mylib_repo, settings, tmp_path / "state", on_failure_block=True, fast=False)
    groups = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    assert len(groups) == 1 and groups[0]["hooks"][0]["command"].endswith("claude-hook")


def test_client_uses_only_the_standard_library():
    r = subprocess.run([sys.executable, "-I", "-S", "-c",
                        f"import runpy, sys; sys.argv=['x']; m = runpy.run_path({str(CLIENT)!r}); "
                        "print(sorted(n for n in sys.modules if n.startswith('covenant')))"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "[]"


def test_daemon_writes_and_removes_its_file(started):
    repo, state = started
    proc = subprocess.Popen([sys.executable, "-m", "covenant", "--repo", str(repo), "--state-dir", str(state), "daemon", "--idle", "1"])
    try:
        proc.wait(timeout=90)  # exits on its own once idle
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0 and not (state / "daemon.json").exists()
