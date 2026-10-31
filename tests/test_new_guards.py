"""SQL, network allowlist, path ownership and budget rules, and rule packs."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from covenant import packs
from covenant.compiler.extract import HeuristicExtractor
from covenant.compiler.ground import ground
from covenant.compiler.pipeline import compile_instruction
from covenant.integrations.claude_code import ClaudeCodeHook
from covenant.ir import Clause, ClauseKind, Covenant, Goal
from covenant.runtime import Runtime
from covenant.validate import validate
from covenant.verifiers import budget, command_guard, network, ownership, sql_guard

from conftest import git


def clause(checker, kind=ClauseKind.EFFECT_PROHIBITION, **params):
    return Clause(id="C1", kind=kind, source_span="rule", checker=checker, params=params)


# ----- destructive SQL -------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    'psql -c "DROP TABLE users"',
    "mysql -e 'truncate table orders'",
    'sqlite3 app.db "DELETE FROM users"',
    'echo "UPDATE users SET admin = 1" | psql mydb',
    'psql -c "ALTER TABLE users DROP COLUMN email"',
    "mongosh --eval 'db.users.drop()'",
    "redis-cli FLUSHALL",
    'python -c "import sqlite3; sqlite3.connect(\'a.db\').execute(\'DROP TABLE t\')"',
])
def test_destructive_sql_is_found(command):
    assert sql_guard.in_command(command), command


@pytest.mark.parametrize("command", [
    'psql -c "DELETE FROM sessions WHERE expires < now()"',
    'psql -c "SELECT * FROM users"',
    'git commit -m "drop table support for old browsers"',
    'psql -c "UPDATE users SET name = \'x\' WHERE id = 3"',
    "psql -c \"-- DROP TABLE users\nSELECT 1\"",
])
def test_safe_sql_is_allowed(command):
    assert sql_guard.in_command(command) == [], command


def test_sql_files_fed_to_a_client_are_read(tmp_path):
    (tmp_path / "wipe.sql").write_text("BEGIN;\nTRUNCATE TABLE audit;\nCOMMIT;\n", encoding="utf-8")
    (tmp_path / "ok.sql").write_text("INSERT INTO t VALUES (1);", encoding="utf-8")
    assert sql_guard.in_command("psql -f wipe.sql mydb", tmp_path)
    assert sql_guard.in_command("mysql mydb < wipe.sql", tmp_path)
    assert sql_guard.in_command("psql -f ok.sql mydb", tmp_path) == []


def test_command_guard_refuses_destructive_sql_and_database_tools():
    c = clause("command_guard", deny=["destructive_sql"])
    assert command_guard.check(c, 'psql -c "DROP DATABASE prod"')[0].evidence["category"] == "destructive_sql"
    assert command_guard.check_tool(c, "mcp__postgres__query", {"sql": "delete from users"})
    assert command_guard.check_tool(c, "mcp__postgres__query", {"sql": "select 1"}) == []


# ----- network allowlist -----------------------------------------------------------------

def test_network_allowlist():
    allowed = ["pypi.org", "files.pythonhosted.org", "github.com"]
    assert network.blocked_hosts("curl https://api.github.com/repos/x", allowed) == []
    assert network.blocked_hosts("curl https://evil.example.com/x.sh", allowed) == ["evil.example.com"]
    assert network.blocked_hosts("pip install requests", allowed) == []
    assert network.blocked_hosts("npm install left-pad", allowed) == ["registry.npmjs.org"]
    assert network.blocked_hosts("git clone git@gitlab.com:org/repo.git", allowed) == ["gitlab.com"]
    assert network.blocked_hosts("ls -la", allowed) == []
    assert network.blocked_hosts("ssh $HOST", allowed) is None  # can't tell where: refused


def test_git_push_uses_the_remote_host(mylib_repo):
    assert network.blocked_hosts("git push", ["github.com"], mylib_repo) is None  # no remote: can't tell
    git(mylib_repo, "remote", "add", "origin", "git@github.com:me/mylib.git")
    assert network.blocked_hosts("git push origin main", ["github.com"], mylib_repo) == []
    assert network.blocked_hosts("git pull", ["gitlab.com"], mylib_repo) == ["github.com"]
    assert network.blocked_hosts("git status", ["gitlab.com"], mylib_repo) == []


def test_allow_hosts_in_command_guard_and_web_tools():
    c = clause("command_guard", deny=["network"], allow_hosts=["github.com"])
    assert command_guard.check(c, "curl https://github.com/x") == []
    assert command_guard.check(c, "curl https://example.org/x")
    assert command_guard.check_tool(c, "WebFetch", {"url": "https://docs.github.com/en"}) == []
    assert command_guard.check_tool(c, "WebFetch", {"url": "https://example.org/"})
    assert command_guard.check_tool(c, "WebSearch", {"query": "x"})
    no_net = clause("command_guard", deny=["network"])
    assert command_guard.check_tool(no_net, "WebFetch", {"url": "https://github.com"})


# ----- ownership -------------------------------------------------------------------------

def test_path_owner():
    c = clause("path_owner", ClauseKind.SEMANTIC_INVARIANT, owners={"billing/**": ["payments"]})
    assert ownership.check_path(c, "billing/invoice.py", "payments") == []
    assert ownership.check_path(c, "billing/invoice.py", "search")[0].evidence["owners"] == ["payments"]
    assert ownership.check_path(c, "billing/invoice.py", "")  # unnamed agents own nothing
    assert ownership.check_path(c, "src/app.py", "search") == []


def _runtime(repo, clauses):
    cov = Covenant(instruction="x", goal=Goal(action="x"), clauses=clauses)
    cov.dump(repo / "covenant.yaml")
    git(repo, "add", "covenant.yaml")
    git(repo, "commit", "-q", "-m", "rules")
    rt = Runtime(cov, repo)
    rt.start()
    return rt


def test_ownership_in_the_hook_and_at_commit(mylib_repo):
    owned = clause("path_owner", ClauseKind.SEMANTIC_INVARIANT, owners={"mylib/**": ["core"]}, baseline="HEAD")
    rt = _runtime(mylib_repo, [owned])
    hook = ClaudeCodeHook(rt)
    edit = {"hook_event_name": "PreToolUse", "tool_name": "Edit",
            "tool_input": {"file_path": str(mylib_repo / "mylib" / "__init__.py")}}
    denied = hook.handle({**edit, "covenant_agent": "docs-bot"})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hook.handle({**edit, "covenant_agent": "core"}) == {}
    (mylib_repo / "mylib" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    rt.agent = "docs-bot"
    assert any(r.checker == "path_owner" for r in rt.preview().reports)
    rt.agent = "core"
    assert not any(r.checker == "path_owner" for r in rt.preview().reports)


# ----- budget ----------------------------------------------------------------------------

def test_budget_time_actions_and_cost(tmp_path):
    started = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    assert budget.check(clause("budget", max_minutes=20), started, 0)[0].evidence["limit"] == "time"
    assert budget.check(clause("budget", max_minutes=60), started, 0) == []
    assert budget.check(clause("budget", max_actions=5), started, 5)
    assert budget.check(clause("budget", max_actions=5), started, 4) == []
    log = tmp_path / "t.jsonl"
    entry = {"message": {"id": "m1", "model": "claude-opus-5-5",
                         "usage": {"input_tokens": 100_000, "output_tokens": 50_000}}}
    log.write_text("\n".join(json.dumps(entry) for _ in range(3)) + "\n", encoding="utf-8")  # one message, logged 3 times
    assert budget.transcript_cost(log) == pytest.approx(0.4 + 1.0)
    assert budget.check(clause("budget", max_cost_usd=1), started, 0, str(log))
    assert budget.check(clause("budget", max_cost_usd=2), started, 0, str(log)) == []


def test_budget_counts_allowed_actions_in_the_hook(mylib_repo):
    rt = _runtime(mylib_repo, [clause("budget", max_actions=2)])
    hook = ClaudeCodeHook(rt)
    call = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert hook.handle(call) == {}
    assert hook.handle(call) == {}
    third = hook.handle(call)
    assert third["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "budget" in third["hookSpecificOutput"]["permissionDecisionReason"]


def test_budget_validation(mylib_repo):
    cov = Covenant(instruction="x", goal=Goal(action="x"), clauses=[clause("budget")])
    assert any("needs max_minutes" in i.message for i in validate(cov, mylib_repo))


# ----- compiler and packs ----------------------------------------------------------------

@pytest.mark.parametrize("instruction, checker, params", [
    ("Fix it. Only reach pypi.org and github.com", "command_guard", {"deny": ["network"], "allow_hosts": ["pypi.org", "github.com"]}),
    ("Fix it. Only the payments team's agent may edit billing/", "path_owner", {"owners": {"billing/**": ["payments"]}, "baseline": "HEAD"}),
    ("Fix it. Stop after 2 hours", "budget", {"max_minutes": 120}),
    ("Fix it. Spend at most $1.50", "budget", {"max_cost_usd": 1.5}),
    ("Fix it. Use at most 40 tool calls", "budget", {"max_actions": 40}),
    ("Fix it. Never run destructive SQL", "command_guard", {"deny": ["destructive_sql"]}),
])
def test_compiler_understands_new_rules(mylib_repo, instruction, checker, params):
    result = compile_instruction(instruction, mylib_repo, HeuristicExtractor())
    assert [(c.checker, c.params) for c in result.covenant.clauses] == [(checker, params)]
    assert not result.errors


@pytest.mark.parametrize("name", list(packs.PACKS))
def test_every_pack_compiles_fully(mylib_repo, name):
    result = compile_instruction("", mylib_repo, HeuristicExtractor(), packs=[name])
    assert result.covenant.clauses
    not_enforced = [c.source_span for c in result.covenant.clauses if c.status.value != "enforced"]
    assert not_enforced == [] or name == "docs-only", not_enforced
    assert f"[rule pack {name}]" in result.covenant.instruction


def test_pack_rules_merge_with_the_instruction_without_duplicates(mylib_repo):
    result = compile_instruction("Fix the crash and run tests before committing", mylib_repo, HeuristicExtractor(),
                                 packs=["hotfix"])
    tests_rules = [c for c in result.covenant.clauses if c.params.get("formula", "").startswith("G(commit")]
    assert len(tests_rules) == 1
    assert [c.id for c in result.covenant.clauses] == [f"C{i}" for i in range(1, len(result.covenant.clauses) + 1)]


def test_unknown_pack():
    with pytest.raises(ValueError, match="unknown rule pack"):
        packs.get("nope")
