import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from covenant.compiler.extract import CompileError, HeuristicExtractor, LLMExtractor
from covenant.compiler.ground import ground
from covenant.compiler.pipeline import check_spans, compile_instruction
from covenant.compiler.schema import CandidateClause, Extraction, Intent
from covenant.ir import ClauseStatus, Covenant

SRC = Path(__file__).resolve().parent.parent / "src"
EXAMPLE = "Update dependencies, but don't change public APIs, don't delete user data, and run tests before committing."


def intents(extraction):
    return [c.intent for c in extraction.clauses]


def test_offline_extraction_of_the_running_example(mylib_repo):
    ex = HeuristicExtractor()(EXAMPLE, ground(mylib_repo))
    assert ex.goal_span == "Update dependencies"
    assert intents(ex) == [Intent.PUBLIC_API_UNCHANGED, Intent.NO_DATA_LOSS, Intent.TESTS_BEFORE_COMMIT]
    assert check_spans(EXAMPLE, ex) == []


@pytest.mark.parametrize(
    "instruction, expected",
    [
        ("Refactor the parser. Never push to the remote.", [Intent.NO_PUSH]),
        ("Fix the bug; don't touch config/settings.yaml", [Intent.PROTECT_PATHS]),
        ("Add a feature and keep the code readable", [Intent.OTHER]),
        ("Migrate the schema without destructive migrations", [Intent.NO_DESTRUCTIVE_MIGRATIONS]),
        ("Ship it, tests must pass before you push", [Intent.TESTS_BEFORE_PUSH]),
    ],
)
def test_offline_intents(instruction, expected, tmp_path):
    assert intents(HeuristicExtractor()(instruction, ground(tmp_path))) == expected


def test_span_checks():
    ex = Extraction(
        goal_action="x", goal_span="Update dependencies",
        clauses=[CandidateClause(source_span="don't change APIs", intent=Intent.PUBLIC_API_UNCHANGED, paths=[], reading="")],
        no_constraint_spans=[],
    )
    issues = check_spans("Update dependencies, don't change public APIs, and be quick.", ex)
    assert any(i.level == "error" and "not in the instruction" in i.message for i in issues)
    assert any(i.level == "warning" and "quick" in i.message for i in issues)


def test_compile_running_example(mylib_repo):
    result = compile_instruction(EXAMPLE, mylib_repo, HeuristicExtractor())
    cov = result.covenant
    assert [c.checker for c in cov.enforced()] == ["api_surface_diff", "effect_monitor", "trace_monitor"]
    assert cov.clause("C2").params["paths"] == ["data/**"]
    assert cov.clause("C3").params["test_cmd"] == "python -m pytest -q"
    assert result.errors == []


def _with_ambiguity(repo: Path) -> Path:
    (repo / "data" / "cache").mkdir()
    for n in range(3):
        (repo / "data" / "cache" / f"m{n}.bin").write_text("x")
    (repo / "mylib" / "_compat.py").write_text("def load():\n    pass\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("Old files: use mylib._compat.load().")
    return repo


def test_questions_default_to_the_safe_reading(mylib_repo):
    result = compile_instruction(EXAMPLE, _with_ambiguity(mylib_repo), HeuristicExtractor())
    cov = result.covenant
    assert len(result.defaulted) == 2 and result.asked == []
    assert "exclude" not in cov.clause("C2").params
    assert cov.clause("C1").params["extra_modules"] == ["mylib._compat"]
    assert any("conservative default" in a for a in cov.clause("C2").assumptions)


def test_answers_change_the_covenant(mylib_repo):
    repo = _with_ambiguity(mylib_repo)
    questions_seen = []

    def ask(q):
        questions_seen.append(q.text)
        return 1  # second option: "may clear it" / "it's internal"

    cov = compile_instruction(EXAMPLE, repo, HeuristicExtractor(), ask=ask).covenant
    assert len(questions_seen) == 2
    assert any("data/cache/" in t for t in questions_seen)
    assert cov.clause("C2").params["exclude"] == ["data/cache/**"]
    assert "extra_modules" not in cov.clause("C1").params


def test_at_most_three_questions_are_asked(mylib_repo):
    repo = _with_ambiguity(mylib_repo)
    for name in ("tmp", "logs", "temp"):
        (repo / "data" / name).mkdir()
        (repo / "data" / name / "f").write_text("x")
    asked = []
    result = compile_instruction(EXAMPLE, repo, HeuristicExtractor(), ask=lambda q: asked.append(q) or 0)
    assert len(asked) == 3 and len(result.defaulted) >= 2


def test_unenforceable_and_pending_are_reported_not_dropped(tmp_path):
    result = compile_instruction("Add caching, keep the code readable, and run tests before committing", tmp_path, HeuristicExtractor())
    statuses = {c.source_span: c.status for c in result.covenant.clauses}
    assert statuses["keep the code readable"] is ClauseStatus.UNENFORCEABLE
    assert statuses["run tests before committing"] is ClauseStatus.PENDING_CLARIFICATION


def test_contradiction_goal_inside_protected_path(mylib_repo):
    result = compile_instruction("Clean up data/tmp, but don't delete user data", mylib_repo, HeuristicExtractor())
    assert any(i.level == "error" and "impossible" in i.message for i in result.issues)


def test_grounding_reads_ci_and_models(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("jobs:\n  t:\n    steps:\n      - run: pytest -x tests\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "models.py").write_text('class U(Base):\n    __tablename__ = "users"\n')
    (tmp_path / "app.db").write_text("")
    facts = ground(tmp_path)
    assert facts.test_commands[0] == ("pytest -x tests", ".github/workflows/ci.yml")
    assert facts.db_tables == ["users"]
    assert "app.db" in facts.data_paths
    assert [p.name for p in facts.python_packages] == ["app"]


# ----- the Claude extractor, with a stand-in client -------------------------------------


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def fake_client(text, stop_reason="end_turn"):
    response = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])
    messages = FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_llm_extractor_request_and_parse(mylib_repo):
    payload = {
        "goal_action": "update_dependencies", "goal_span": "Update dependencies",
        "clauses": [{"source_span": "don't change public APIs", "intent": "public_api_unchanged", "paths": [], "reading": "API stays"}],
        "no_constraint_spans": [],
    }
    client, messages = fake_client(json.dumps(payload))
    ex = LLMExtractor(client=client)("Update dependencies, but don't change public APIs.", ground(mylib_repo))
    assert intents(ex) == [Intent.PUBLIC_API_UNCHANGED]
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "high"
    assert call["fallbacks"] == "default"
    assert "mylib" in call["messages"][0]["content"]


def test_llm_extractor_refusal_and_bad_output(mylib_repo):
    client, _ = fake_client("", stop_reason="refusal")
    with pytest.raises(CompileError, match="declined"):
        LLMExtractor(client=client)("x", ground(mylib_repo))
    client, _ = fake_client('{"goal_action": 1}')
    with pytest.raises(CompileError, match="schema"):
        LLMExtractor(client=client)("x", ground(mylib_repo))


def test_extraction_schema_is_strict():
    schema = Extraction.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"goal_action", "goal_span", "clauses", "no_constraint_spans"}


def test_cli_compile_then_init(mylib_repo, tmp_path):
    (mylib_repo / "covenant.yaml").unlink()
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    base = [sys.executable, "-m", "covenant", "--repo", str(mylib_repo), "--state-dir", str(tmp_path / "s")]
    r = subprocess.run(base + ["compile", EXAMPLE, "--offline", "--unattended", "--yes"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    cov = Covenant.load(mylib_repo / "covenant.yaml")
    assert len(cov.enforced()) == 3
    r = subprocess.run(base + ["init", "--no-claude"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr


def test_user_data_in_sqlite_is_guarded_row_by_row(tmp_path):
    import sqlite3
    from contextlib import closing

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "uploads.txt").write_text("x")
    with closing(sqlite3.connect(tmp_path / "data" / "app.db")) as db:
        db.execute("CREATE TABLE t (x)")
    (tmp_path / "data" / "notes.db").write_text("not sqlite at all")
    facts = ground(tmp_path)
    assert facts.db_files == ["data/app.db"]

    result = compile_instruction("Add a signup endpoint, but don't delete user data", tmp_path, HeuristicExtractor())
    by_checker = {c.checker: c for c in result.covenant.clauses}
    assert by_checker["sqlite_guard"].params == {"databases": ["data/app.db"]}
    assert by_checker["sqlite_guard"].on_violation.value == "rollback"
    effect = by_checker["effect_monitor"]
    assert effect.params["paths"] == ["data/**"]
    assert "data/app.db" in effect.params["exclude"] and "data/app.db-journal" in effect.params["exclude"]
    assert not [i for i in result.issues if i.level == "error"]


def test_explicit_database_path_only(tmp_path):
    result = compile_instruction("Fix the login bug. Don't delete anything in app.db", tmp_path, HeuristicExtractor())
    [clause] = [c for c in result.covenant.clauses if c.checker]
    assert clause.checker == "sqlite_guard" and clause.params["databases"] == ["app.db"]


def test_endpoint_note():
    from covenant.compiler.extract import endpoint_note

    assert endpoint_note({}) is None
    assert endpoint_note({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}) is None
    note = endpoint_note({"ANTHROPIC_BASE_URL": "https://openrouter.ai/api"})
    assert "openrouter.ai" in note and "COVENANT_ANTHROPIC_BASE_URL" in note
    assert endpoint_note({"ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "COVENANT_ANTHROPIC_BASE_URL": "https://api.anthropic.com"}) is None


@pytest.mark.parametrize(
    ("instruction", "formula", "status"),
    [
        ("Fix the login bug, and push at most once", "G(#push <= 1)", ClauseStatus.ENFORCED),
        ("Fix the login bug. Don't commit more than 3 times", "G(#commit <= 3)", ClauseStatus.ENFORCED),
        ("Fix the login bug. Commit and push no more than twice", "G(#commit <= 2 & #push <= 2)", ClauseStatus.ENFORCED),
        ("Fix the login bug. Run the tests within five steps of any edit", "G(!(!test S[>=5] edit))", ClauseStatus.ENFORCED),
        ("Fix the login bug. Push only a few times", "G(#push <= 1)", ClauseStatus.PENDING_CLARIFICATION),
    ],
)
def test_limits_and_deadlines(instruction, formula, status, mylib_repo):
    cov = compile_instruction(instruction, mylib_repo, HeuristicExtractor()).covenant
    [clause] = [c for c in cov.clauses if c.checker == "trace_monitor"]
    assert clause.params["formula"] == formula and clause.status is status


def test_limit_is_enforced_end_to_end(mylib_repo, tmp_path):
    from covenant.events import Push
    from covenant.runtime import Runtime

    compile_instruction("Fix the login bug, and push at most once", mylib_repo, HeuristicExtractor()).covenant.dump(mylib_repo / "covenant.yaml")
    rt = Runtime.open(mylib_repo, state_dir=tmp_path / "state")
    rt.start()
    assert rt.record(Push()).allowed
    d = rt.record(Push())
    assert not d.allowed and d.blocking[0].evidence["counts_so_far"] == {"push": 1}
    assert "limit is reached" in d.blocking[0].repair_hints[0]


def test_deadline_is_enforced_end_to_end(mylib_repo, tmp_path):
    from covenant.events import Commit, Edit, Run
    from covenant.runtime import Runtime

    instruction = "Fix the login bug. Run the tests within 2 steps of any edit"
    compile_instruction(instruction, mylib_repo, HeuristicExtractor()).covenant.dump(mylib_repo / "covenant.yaml")
    rt = Runtime.open(mylib_repo, state_dir=tmp_path / "state")
    rt.start()
    assert rt.record(Edit(path="mylib/core.py")).reports == []
    assert rt.record(Run(cmd="ls", exit_code=0)).reports == []
    late = rt.record(Run(cmd="ls", exit_code=0))  # second step after the edit, still no tests
    assert late.reports and "Run the tests now" in late.reports[0].repair_hints[0]
    assert not rt.record(Commit()).allowed
    rt.run_tests()
    assert rt.record(Commit()).allowed


def test_test_commands_rank_the_main_suite_first(tmp_path, monkeypatch):
    """From a real repo (TinyDB): CI runs `py.test` for the suite and `pytest --mypy -m mypy` for types."""
    import shutil

    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n  t:\n    steps:\n"
        "    - name: Type check\n      run: |\n        tox -e mypy\n"
        "    - name: Suite\n      run: |\n        tox -e py\n"
        "    - name: Old\n      run: |\n        py.test -v --cov=pkg\n"
    )
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(shutil, "which", lambda name: None)  # tox and py.test aren't installed
    cmds = [c for c, _ in ground(tmp_path).test_commands]
    assert cmds == ["python -m pytest -q", "tox -e py", "py.test -v --cov=pkg", "tox -e mypy"]
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    assert [c for c, _ in ground(tmp_path).test_commands][0] == "tox -e py"


def test_answering_the_test_command_question_replaces_the_guess(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("jobs:\n  t:\n    steps:\n      - run: python -m pytest -x\n")
    (tmp_path / "tests").mkdir()
    cov = compile_instruction("Fix it, and run tests before committing", tmp_path, HeuristicExtractor(),
                              answers={"Q1": 1}).covenant
    [clause] = cov.clauses
    assert clause.params["test_cmd"] == "python -m pytest -q"
    assert not [a for a in clause.assumptions if a.startswith("tests = ")]


# ----- the Claude Code extractor, with a stand-in for `claude -p` ------------------------


def fake_claude(payload, returncode=0, record=None):
    def run(cmd, **kwargs):
        if record is not None:
            record.append((cmd, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="")
    return run


EXTRACTION = {
    "goal_action": "update_deps", "goal_span": "Update dependencies",
    "clauses": [{"source_span": "don't change public APIs", "intent": "public_api_unchanged", "paths": [], "reading": "x"}],
    "no_constraint_spans": [],
}


def test_claude_code_extractor_request_and_parse(mylib_repo):
    from covenant.compiler.extract import ClaudeCodeExtractor

    calls = []
    ex = ClaudeCodeExtractor(exe="claude", runner=fake_claude({"is_error": False, "structured_output": EXTRACTION}, record=calls))
    result = ex("Update dependencies, but don't change public APIs", ground(mylib_repo))
    assert result.clauses[0].intent is Intent.PUBLIC_API_UNCHANGED
    cmd, kwargs = calls[0]
    assert "--restricted" in cmd and cmd[cmd.index("--tools") + 1] == ""  # no tools, repo settings ignored
    assert json.loads(cmd[cmd.index("--json-schema") + 1])["title"] == "Extraction"
    assert "<instruction>" in kwargs["input"] and kwargs["cwd"] != str(mylib_repo)


def test_claude_code_extractor_errors(mylib_repo):
    from covenant.compiler.extract import ClaudeCodeExtractor

    facts = ground(mylib_repo)
    with pytest.raises(CompileError, match="isn't logged in"):
        ClaudeCodeExtractor(exe="claude", runner=fake_claude({"is_error": True, "result": "Not logged in · Please run /login"}))("x", facts)
    with pytest.raises(CompileError, match="no structured"):
        ClaudeCodeExtractor(exe="claude", runner=fake_claude({"is_error": False, "result": "hi"}))("x", facts)
    missing = ClaudeCodeExtractor(exe="claude")
    missing.exe = None  # as when `claude` isn't on PATH
    with pytest.raises(CompileError, match="isn't installed"):
        missing("x", facts)


def test_choose_extractor(monkeypatch):
    import shutil
    from covenant.compiler.extract import ClaudeCodeExtractor, choose_extractor

    assert isinstance(choose_extractor("auto", env={"ANTHROPIC_API_KEY": "x"}), LLMExtractor)
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/claude")
    assert isinstance(choose_extractor("auto", env={}), ClaudeCodeExtractor)
    assert isinstance(choose_extractor("offline", env={}), HeuristicExtractor)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(CompileError, match="--offline"):
        choose_extractor("auto", env={})


@pytest.mark.parametrize(("rule", "checker", "params"), [
    ("Don't add new dependencies", "dependency_diff", {"allow_new": False, "lockfiles": False}),
    ("Only change files in src/", "scope_guard", {"allow": ["src/**"]}),
    ("Don't commit secrets or API keys", "secret_scan", {}),
    ("Don't delete or skip tests", "test_guard", {}),
    ("Keep the change under 200 lines", "diff_budget", {"max_lines": 200}),
    ("Touch at most 3 files", "diff_budget", {"max_files": 3}),
    ("No leftover debug prints", "no_debug_code", {"include_print": True}),
    ("Don't silence type errors with type: ignore", "no_suppressions", {}),
    ("Don't commit large files over 5 MB", "large_files", {"max_kb": 5120}),
    ("Never force push or rewrite history", "command_guard", {"deny": ["force_push", "history_rewrite"]}),
    ("Don't deploy or publish anything", "command_guard", {"deny": ["publish", "deploy"]}),
    ("Don't read the .env file", "command_guard", {"deny": ["read_secrets"]}),
    ("Don't install packages and no network access", "command_guard", {"deny": ["install_packages", "network"]}),
])
def test_new_rule_phrasings(rule, checker, params, mylib_repo):
    cov = compile_instruction(f"Fix the bug. {rule}", mylib_repo, HeuristicExtractor()).covenant
    [clause] = cov.clauses
    assert clause.checker == checker and clause.status is ClauseStatus.ENFORCED
    assert {k: clause.params[k] for k in params} == params


def test_rules_without_specifics_ask(mylib_repo):
    cov = compile_instruction("Fix the bug. Keep the change small. Only edit the files you need", mylib_repo,
                              HeuristicExtractor()).covenant
    assert [c.status for c in cov.clauses] == [ClauseStatus.PENDING_CLARIFICATION] * 2
