import json
import threading
import urllib.error
import urllib.request

import pytest

from covenant import server
from covenant.runtime import Runtime

from conftest import git


@pytest.fixture
def hook_server(mylib_repo, tmp_path):
    state = tmp_path / "state"
    Runtime.open(mylib_repo, state_dir=state).start()
    srv = server.HookServer(("127.0.0.1", 0), mylib_repo, None, str(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def post(url, payload, token):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def test_token_required(hook_server):
    srv, url = hook_server
    for token in (None, "wrong"):
        with pytest.raises(urllib.error.HTTPError) as err:
            post(url + "/hook", {}, token)
        assert err.value.code == 401
    assert json.loads(urllib.request.urlopen(url + "/health", timeout=10).read()) == {"ok": True}
    assert server.token_path(srv.state_dir).read_text() == srv.token


def test_hook_events_over_http(hook_server, mylib_repo):
    srv, url = hook_server
    out = post(url + "/hook", {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}, srv.token)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    post(url + "/hook", {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "python -m pytest -q"}}, srv.token)
    assert post(url + "/hook", {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}, srv.token) == {}


def test_install_and_git_hooks_through_the_server(hook_server, mylib_repo):
    srv, url = hook_server
    settings = mylib_repo / ".claude" / "settings.json"
    server.install(mylib_repo, url, srv.token, settings)
    hooks = json.loads(settings.read_text())["hooks"]
    assert hooks["PreToolUse"][0]["hooks"][0]["type"] == "http"
    assert hooks["PreToolUse"][0]["hooks"][0]["url"] == f"{url}/hook"
    server.install(mylib_repo, url, srv.token, settings)  # idempotent
    assert len(json.loads(settings.read_text())["hooks"]["PreToolUse"]) == 1
    Runtime.open(mylib_repo, state_dir=srv.state_dir).start()  # re-approve after changing hooks

    (mylib_repo / "notes.txt").write_text("x\n")
    git(mylib_repo, "add", "notes.txt")
    refused = git(mylib_repo, "commit", "-qm", "untested")
    assert refused.returncode != 0 and "C3" in refused.stderr
    post(url + "/hook", {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "python -m pytest -q"}}, srv.token)
    ok = git(mylib_repo, "commit", "-qm", "tested")
    assert ok.returncode == 0, ok.stderr


def test_git_hook_refuses_when_server_is_down(mylib_repo, tmp_path):
    hook = mylib_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(server.git_hook_script("pre-commit", "http://127.0.0.1:9", "t"), newline="\n")
    hook.chmod(0o755)
    (mylib_repo / "n.txt").write_text("x\n")
    git(mylib_repo, "add", "n.txt")
    r = git(mylib_repo, "commit", "-qm", "x")
    assert r.returncode != 0 and "unreachable" in r.stderr
