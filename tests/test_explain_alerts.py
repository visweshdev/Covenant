"""covenant explain, alerts and suggest."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from covenant import alerts, explain, suggest
from covenant.events import Commit, Edit
from covenant.integrations.claude_code import ClaudeCodeHook
from covenant.ir import Clause, ClauseKind, Covenant, Goal
from covenant.runtime import Runtime

from conftest import git


def _rt(repo, clauses):
    cov = Covenant(instruction="x", goal=Goal(action="x"), clauses=clauses)
    cov.dump(repo / "covenant.yaml")
    rt = Runtime(cov, repo)
    rt.start()
    return rt


TESTS_FIRST = Clause(id="C1", kind=ClauseKind.TEMPORAL_OBLIGATION, source_span="run tests before committing",
                     checker="trace_monitor", params={"formula": "G(commit -> (!edit S test_pass))", "test_cmd": "pytest"})
NO_FORCE = Clause(id="C2", kind=ClauseKind.EFFECT_PROHIBITION, source_span="never force push",
                  checker="command_guard", params={"deny": ["force_push"]})


def _entries(rt):
    return [json.loads(l) for l in (rt.state_dir / "reports.jsonl").read_text(encoding="utf-8").splitlines()]


def test_explain_a_blocked_commit(mylib_repo):
    rt = _rt(mylib_repo, [TESTS_FIRST])
    rt.record(Edit(path="mylib/core.py"))
    assert not rt.record(Commit()).allowed
    text = explain.explain(_entries(rt))
    assert "the agent tried to commit. Covenant blocked it." in text
    assert 'Rule C1: "run tests before committing"' in text
    assert "The tests haven't passed yet" in text and "mylib/core.py" in text
    assert "What the agent can do: Run `pytest`" in text
    assert "covenant undo" in text


def test_refused_commands_are_logged_and_explained(mylib_repo):
    rt = _rt(mylib_repo, [NO_FORCE])
    out = ClaudeCodeHook(rt).handle({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                     "tool_input": {"command": "git push --force origin main"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    entries = _entries(rt)
    assert entries[-1]["event"].startswith("refused") and entries[-1]["counted"] == []
    text = explain.explain(entries)
    assert "tried to use a tool" in text and "counts as a force push" in text


def test_explain_with_nothing_to_explain():
    assert "Nothing has been blocked" in explain.explain([])


@pytest.fixture
def inbox():
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook", got
    server.shutdown()


def test_alerts_go_out_once_per_problem(mylib_repo, inbox, monkeypatch):
    url, got = inbox
    monkeypatch.setenv("COVENANT_SLACK_WEBHOOK", url)
    monkeypatch.setenv("COVENANT_ALERT_WEBHOOK", url)
    (alerts.config_path().parent).mkdir(parents=True, exist_ok=True)
    alerts.config_path().write_text("events: [escalated, denied]\n", encoding="utf-8")
    rt = _rt(mylib_repo, [NO_FORCE])
    hook = ClaudeCodeHook(rt)
    push = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git push -f"}}
    hook.handle(push)
    assert len(got) == 2  # slack + webhook
    slack, webhook = sorted(got, key=lambda d: "event" in d)
    assert "tried something it isn't allowed to" in slack["text"] and "C2: never force push" in slack["text"]
    assert webhook["event"] == "denied" and webhook["reports"][0]["violation"] == "C2"
    hook.handle(push)
    assert len(got) == 2  # quiet period: the same problem isn't sent again


def test_alerts_are_off_unless_configured(mylib_repo, monkeypatch):
    for var in ("COVENANT_SLACK_WEBHOOK", "COVENANT_ALERT_WEBHOOK", "COVENANT_ALERT_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    assert not alerts.configured()
    assert alerts.send(mylib_repo, mylib_repo / "s", "escalated", []) == []
    assert "No alerts configured" in alerts.describe()


def test_failing_channels_never_break_a_check(mylib_repo, monkeypatch, tmp_path):
    monkeypatch.setenv("COVENANT_ALERT_WEBHOOK", "http://127.0.0.1:9/nothing-listens-here")
    assert alerts.send(mylib_repo, tmp_path, "escalated", []) == []
    assert "webhook" in (tmp_path / "alerts.log").read_text(encoding="utf-8")


def test_suggest_from_history(mylib_repo):
    def commit(msg, path="mylib/x.py", text="x"):
        (mylib_repo / path).parent.mkdir(parents=True, exist_ok=True)
        (mylib_repo / path).write_text(text, encoding="utf-8")
        git(mylib_repo, "add", "-A")
        git(mylib_repo, "commit", "-q", "-m", msg)

    commit("Remove leaked API key from settings", text="1")
    commit("Remove debug prints", text="2")
    commit("Add tests", path="tests/test_x.py")
    git(mylib_repo, "rm", "-q", "tests/test_x.py")
    git(mylib_repo, "commit", "-q", "-m", "cleanup")
    for n in range(2):
        commit(f"Tweak config {n}", path="config/app.yaml", text=str(n))
        git(mylib_repo, "revert", "--no-edit", "HEAD")
    rules = {s.rule: s for s in suggest.suggest(mylib_repo)}
    assert "Don't commit secrets or API keys" in rules
    assert "No leftover debug prints" in rules
    assert "Don't delete or skip tests" in rules
    assert "Don't touch config/" in rules
    text = suggest.render(list(rules.values()))
    assert 'covenant amend "' in text
