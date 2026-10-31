"""Claude review of rules no checker covers: reported, cached, never blocking."""

import json
from types import SimpleNamespace

from covenant.compiler.extract import HeuristicExtractor
from covenant.compiler.pipeline import compile_instruction
from covenant.events import Commit
from covenant.ir import ClauseStatus, OnViolation
from covenant.runtime import Runtime
from covenant.verifiers import llm_review

from conftest import git


def fake(verdicts, calls):
    def run(cmd, **kwargs):
        calls.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"is_error": False, "structured_output": {"verdicts": verdicts}}), stderr="")
    return run


def setup(repo, state):
    cov = compile_instruction("Fix the parser. Keep the code readable", repo, HeuristicExtractor(), review=True).covenant
    [clause] = cov.clauses
    assert clause.checker == "llm_review" and clause.on_violation is OnViolation.WARN and clause.status is ClauseStatus.ENFORCED
    cov.dump(repo / "covenant.yaml")
    git(repo, "commit", "-qam", "rules")
    rt = Runtime.open(repo, state_dir=state)
    rt.start()
    return rt, clause


def test_offline_without_review_stays_unenforceable(mylib_repo):
    cov = compile_instruction("Fix the parser. Keep the code readable", mylib_repo, HeuristicExtractor()).covenant
    assert cov.clauses[0].status is ClauseStatus.UNENFORCEABLE


def test_review_reports_but_never_blocks(mylib_repo, tmp_path, monkeypatch):
    rt, clause = setup(mylib_repo, tmp_path / "state")
    (mylib_repo / "mylib" / "core.py").write_text((mylib_repo / "mylib" / "core.py").read_text() + "\ndef f(a,b,c,d,e): return a\n")
    calls = []
    verdict = [{"rule_id": clause.id, "follows": False, "explanation": "f has unclear parameter names",
                "file": "mylib/core.py", "line": 20}]
    monkeypatch.setattr(llm_review.shutil, "which", lambda name: "claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    original = llm_review.subprocess.run
    monkeypatch.setattr(llm_review.subprocess, "run",
                        lambda cmd, **kw: fake(verdict, calls)(cmd, **kw) if cmd[0] == "claude" else original(cmd, **kw))
    d = rt.record(Commit(), dry_run=True)
    [r] = [r for r in d.reports if r.checker == "llm_review"]
    assert d.allowed and d.blocking == [] and d.exit_code == 0
    assert r.evidence["target"] == "mylib/core.py" and "unclear" in r.repair_hints[0]
    assert "def f(a,b,c,d,e)" in calls[0] and "Keep the code readable" in calls[0]
    rt.record(Commit(), dry_run=True)
    assert len(calls) == 1  # same change: cached
    assert [r for r in rt.preview().reports if r.checker == "llm_review"] == []  # editors never pay for a review
    assert len(calls) == 1


def test_review_failure_is_advisory(mylib_repo, tmp_path, monkeypatch):
    rt, _ = setup(mylib_repo, tmp_path / "state")
    (mylib_repo / "mylib" / "core.py").write_text("x = 1\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm_review.shutil, "which", lambda name: None)
    d = rt.record(Commit(), dry_run=True)
    [r] = [r for r in d.reports if r.checker == "llm_review"]
    assert "review didn't run" in r.evidence["error"] and d.blocking == []


def test_diff_includes_new_files(mylib_repo):
    (mylib_repo / "mylib" / "new_module.py").write_text("VALUE = 42\n")
    assert "+VALUE = 42" in llm_review.diff_text(mylib_repo, "HEAD")
