"""Adding rules mid-task: new rules apply from now on, history and old rules stay."""

import json

import pytest

from covenant.events import Commit, Edit, Run
from covenant.ir import Clause, ClauseKind, Covenant, OnViolation
from covenant.runtime import Runtime

from test_cli import cli


def add_clause(repo, clause):
    cov = Covenant.load(repo / "covenant.yaml")
    cov.model_copy(update={"clauses": cov.clauses + [clause]}).dump(repo / "covenant.yaml")


NO_PUSH = Clause(id="C4", kind=ClauseKind.TEMPORAL_OBLIGATION, source_span="never push",
                 checker="trace_monitor", params={"formula": "G(!commit)"})


def test_new_rule_applies_from_now_on(mylib_repo, tmp_path):
    state = tmp_path / "state"
    rt = Runtime.open(mylib_repo, state_dir=state)
    rt.start()
    rt.run_tests()
    assert rt.record(Commit()).allowed  # history: one commit before the amendment

    add_clause(mylib_repo, NO_PUSH)
    rt = Runtime.open(mylib_repo, state_dir=state)
    assert rt.integrity_reports()  # the edit alone is tampering until approved
    assert rt.amend() == ["C4"]

    rt = Runtime.open(mylib_repo, state_dir=state)
    assert rt.integrity_reports() == []
    assert len(rt.trace) == 2  # history kept
    d = rt.record(Commit())
    assert not d.allowed and [r.violation for r in d.blocking] == ["C4"]


def test_new_protected_files_are_snapshotted(mylib_repo, tmp_path):
    state = tmp_path / "state"
    (mylib_repo / "config").mkdir()
    (mylib_repo / "config" / "prod.toml").write_text("x = 1\n")
    rt = Runtime.open(mylib_repo, state_dir=state)
    rt.start()
    add_clause(mylib_repo, Clause(
        id="C4", kind=ClauseKind.EFFECT_PROHIBITION, source_span="don't touch config/", checker="effect_monitor",
        params={"deny": ["delete", "overwrite"], "paths": ["config/**"]}, on_violation=OnViolation.ROLLBACK))
    Runtime.open(mylib_repo, state_dir=state).amend()
    (mylib_repo / "config" / "prod.toml").write_text("x = 2\n")
    d = Runtime.open(mylib_repo, state_dir=state).record(Run(cmd="sed -i ...", exit_code=0))
    assert d.reports[0].evidence["rolled_back"] is True
    assert (mylib_repo / "config" / "prod.toml").read_text() == "x = 1\n"


def test_amend_refuses_changes_and_tampering(mylib_repo, tmp_path):
    state = tmp_path / "state"
    Runtime.open(mylib_repo, state_dir=state).start()
    cov = Covenant.load(mylib_repo / "covenant.yaml")
    loosened = [c for c in cov.clauses if c.id != "C3"] + [NO_PUSH]
    cov.model_copy(update={"clauses": loosened}).dump(mylib_repo / "covenant.yaml")
    with pytest.raises(ValueError, match="C3 changed or disappeared"):
        Runtime.open(mylib_repo, state_dir=state).amend()

    cov.dump(mylib_repo / "covenant.yaml")
    add_clause(mylib_repo, NO_PUSH)
    settings = mylib_repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"disableAllHooks": True}))
    with pytest.raises(ValueError, match="only `covenant start`"):
        Runtime.open(mylib_repo, state_dir=state).amend()


def test_amend_cli(mylib_repo, tmp_path):
    state = tmp_path / "state"
    assert cli(mylib_repo, state, "start").returncode == 0
    assert cli(mylib_repo, state, "record", "edit", "mylib/core.py").returncode == 0

    dry = cli(mylib_repo, state, "amend", "Also never push", "--offline", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "id: C4" in dry.stderr and "G(!push)" in dry.stderr
    assert "never push" not in (mylib_repo / "covenant.yaml").read_text()

    r = cli(mylib_repo, state, "amend", "Also never push", "--offline", "--yes")
    assert r.returncode == 0, r.stderr
    assert "added and approved C4" in r.stderr
    cov = Covenant.load(mylib_repo / "covenant.yaml")
    assert cov.clauses[-1].id == "C4" and cov.instruction.endswith("Also never push")

    status = json.loads(cli(mylib_repo, state, "status", "--format", "json").stdout)
    assert status["trace_length"] == 1 and status["session"]["amendments"][0]["clauses"] == ["C4"]  # the edit from before the amendment
    push = json.loads(cli(mylib_repo, state, "record", "push").stdout)
    assert "C4" in [r["violation"] for r in push["reports"]]


def test_amend_needs_a_covenant(tmp_path):
    r = cli(tmp_path, tmp_path / "state", "amend", "never push", "--offline", "--yes")
    assert r.returncode == 3 and "no covenant.yaml" in r.stderr


def test_record_edit_after_amend_keeps_old_rules(mylib_repo, tmp_path):
    state = tmp_path / "state"
    rt = Runtime.open(mylib_repo, state_dir=state)
    rt.start()
    rt.record(Edit(path="mylib/core.py"))
    add_clause(mylib_repo, NO_PUSH)
    Runtime.open(mylib_repo, state_dir=state).amend()
    d = Runtime.open(mylib_repo, state_dir=state).record(Commit())
    assert {r.violation for r in d.blocking} == {"C3", "C4"}  # C3 still sees the untested edit
