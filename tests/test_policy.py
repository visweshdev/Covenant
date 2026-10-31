import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from covenant import ci, policy
from covenant.events import Edit
from covenant.integrations.claude_code import ClaudeCodeHook
from covenant.ir import Covenant
from covenant.runtime import Runtime

from conftest import git

SRC = Path(__file__).resolve().parent.parent / "src"

TEAM = {
    "policy": {
        "name": "platform",
        "extends": ["base.yaml"],
        "clauses": [{
            "id": "T1", "kind": "EffectProhibition", "source_span": "never touch production config",
            "checker": "effect_monitor", "params": {"paths": ["deploy/prod/**"], "deny": ["delete", "truncate", "overwrite"]},
            "on_violation": "rollback",
        }],
    }
}
BASE = {
    "policy": {
        "name": "org",
        "clauses": [{
            "id": "O1", "kind": "TemporalObligation", "source_span": "never push without a human",
            "checker": "trace_monitor", "params": {"formula": "G(!push)"},
        }],
    }
}


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


@pytest.fixture
def team_repo(mylib_repo):
    write(mylib_repo / "policies" / "team.yaml", TEAM)
    write(mylib_repo / "policies" / "base.yaml", BASE)
    (mylib_repo / "deploy" / "prod").mkdir(parents=True)
    (mylib_repo / "deploy" / "prod" / "app.env").write_text("DEBUG=0\n")
    cov = mylib_repo / "covenant.yaml"
    data = yaml.safe_load(cov.read_text())
    data["covenant"]["extends"] = ["policies/team.yaml"]
    write(cov, data)
    git(mylib_repo, "add", "-A")
    git(mylib_repo, "commit", "-qm", "adopt team policy")
    return mylib_repo


def test_policies_merge_with_origin_and_nesting(team_repo):
    merged, used = policy.load(team_repo / "covenant.yaml")
    assert [c.id for c in merged.clauses] == ["O1", "T1", "C1", "C2", "C3"]
    assert merged.clause("T1").origin.startswith("platform (")
    assert merged.extends == []
    assert [Path(p).name for p in used] == ["base.yaml", "team.yaml"]
    # Resolving an already merged Covenant changes nothing.
    again, more = policy.resolve(merged, policy.disk_reader, str(team_repo), include_machine=False)
    assert again == merged and more == []


def test_repo_cannot_redefine_a_policy_rule(team_repo):
    cov = team_repo / "covenant.yaml"
    data = yaml.safe_load(cov.read_text())
    data["covenant"]["clauses"][0]["id"] = "T1"
    write(cov, data)
    with pytest.raises(policy.PolicyError, match="can't be redefined"):
        policy.load(cov)


def test_loops_and_missing_files(tmp_path):
    write(tmp_path / "a.yaml", {"policy": {"name": "a", "extends": ["b.yaml"]}})
    write(tmp_path / "b.yaml", {"policy": {"name": "b", "extends": ["a.yaml"]}})
    cov = Covenant.model_validate({"instruction": "x", "goal": {"action": "x"}, "clauses": [], "extends": ["a.yaml"]})
    with pytest.raises(policy.PolicyError, match="loop"):
        policy.resolve(cov, policy.disk_reader, str(tmp_path), include_machine=False)
    cov.extends = ["nope.yaml"]
    with pytest.raises(policy.PolicyError, match="not found"):
        policy.resolve(cov, policy.disk_reader, str(tmp_path), include_machine=False)


def test_machine_policy_applies_everywhere(mylib_repo, tmp_path, monkeypatch):
    machine = write(tmp_path / "machine.yaml", BASE)
    monkeypatch.setenv(policy.POLICY_ENV, str(machine))
    merged, used = policy.load(mylib_repo / "covenant.yaml")
    assert merged.clauses[0].id == "O1" and used == [machine]


def test_runtime_enforces_policy_rules_and_detects_policy_edits(team_repo, tmp_path):
    rt = Runtime.open(team_repo, state_dir=tmp_path / "s")
    rt.start()
    assert "T1" in [c.id for c in rt.covenant.enforced()]

    # The policy's protected file is rolled back like any other.
    prod = team_repo / "deploy" / "prod" / "app.env"
    prod.write_text("DEBUG=1\n")
    d = rt.record(Edit(path="x.py"))
    assert [r.violation for r in d.reports] == ["T1"] and prod.read_text() == "DEBUG=0\n"

    # The org rule forbids pushing.
    hook = ClaudeCodeHook(Runtime.open(team_repo, state_dir=tmp_path / "s"))
    out = hook.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git push"}})
    assert "O1" in out["hookSpecificOutput"]["permissionDecisionReason"]

    # The agent may not edit a policy file, and editing one directly is tampering.
    team_file = team_repo / "policies" / "team.yaml"
    out = hook.handle({"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": str(team_file)}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    team_file.write_text(team_file.read_text().replace("deploy/prod/**", "nothing/**"))
    rt2 = Runtime.open(team_repo, state_dir=tmp_path / "s")
    assert rt2.repo_covenant_changed()
    assert any(r.violation == "INTEGRITY" for r in rt2.check().reports)
    assert "deploy/prod/**" in rt2.covenant.clause("T1").params["paths"]  # the approved version still applies


def test_ci_judges_by_the_base_policy(team_repo):
    base = git(team_repo, "rev-parse", "HEAD").stdout.strip()
    git(team_repo, "checkout", "-qb", "feature")
    team_file = team_repo / "policies" / "team.yaml"
    team_file.write_text(team_file.read_text().replace("deploy/prod/**", "nothing/**"))  # weaken T1...
    (team_repo / "deploy" / "prod" / "app.env").unlink()  # ...and break it
    git(team_repo, "add", "-A")
    git(team_repo, "commit", "-qm", "sneaky")
    result = ci.run_ci(team_repo, team_repo / "covenant.yaml", base, run_test_commands=False)
    assert sorted({r.violation for r in result.reports}) == ["INTEGRITY", "T1"]
    assert any("policies/team.yaml" in n for n in result.notes)


def test_compile_keeps_extends(team_repo):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    r = subprocess.run(
        [sys.executable, "-m", "covenant", "--repo", str(team_repo), "compile", "Fix it, but don't delete user data",
         "--offline", "--unattended", "--yes"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert Covenant.load(team_repo / "covenant.yaml").extends == ["policies/team.yaml"]


# ----- policies from a URL ---------------------------------------------------------------

URL_POLICY = """policy:
  name: org
  extends: [base.yaml]
  clauses:
    - {id: ORG1, kind: EffectProhibition, source_span: never deploy, checker: command_guard, params: {deny: [deploy]}}
"""
URL_BASE = """policy:
  name: org-base
  clauses:
    - {id: ORG0, kind: EffectProhibition, source_span: never force push, checker: command_guard, params: {deny: [force_push]}}
"""


@pytest.fixture
def policy_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    files = {"/team/policy.yaml": URL_POLICY, "/team/base.yaml": URL_BASE}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = files.get(self.path)
            self.send_response(200 if body else 404)
            self.end_headers()
            if body:
                self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", files
    server.shutdown()


def test_policy_from_a_url_with_nested_extends_and_offline_cache(policy_server, mylib_repo):
    base, files = policy_server
    cov = Covenant(instruction="x", goal={"action": "x"}, clauses=[], extends=[f"{base}/team/policy.yaml"])
    merged, used = policy.resolve(cov, policy.disk_reader, str(mylib_repo))
    assert [c.id for c in merged.clauses] == ["ORG0", "ORG1"]
    assert merged.clauses[1].origin == f"org ({base}/team/policy.yaml)"
    assert all(Path(p).is_file() for p in used)  # local cached copies, so they can be hashed and guarded
    files.clear()  # the server is "down": the last good copy is used
    merged, _ = policy.resolve(cov, policy.disk_reader, str(mylib_repo))
    assert [c.id for c in merged.clauses] == ["ORG0", "ORG1"]


def test_pinned_policy_url(policy_server, mylib_repo):
    import hashlib

    base, files = policy_server
    good = hashlib.sha256(URL_BASE.encode()).hexdigest()
    ok = Covenant(instruction="x", goal={"action": "x"}, clauses=[], extends=[f"{base}/team/base.yaml#sha256={good}"])
    assert [c.id for c in policy.resolve(ok, policy.disk_reader, str(mylib_repo))[0].clauses] == ["ORG0"]
    files["/team/base.yaml"] = URL_BASE.replace("force_push", "publish")
    with pytest.raises(policy.PolicyError, match="pinned sha256"):
        policy.resolve(ok, policy.disk_reader, str(mylib_repo))


def test_plain_http_policy_urls_are_refused():
    with pytest.raises(policy.PolicyError, match="https"):
        policy.fetch("http://example.com/policy.yaml")
