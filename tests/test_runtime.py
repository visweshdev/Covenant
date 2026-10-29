"""The worked example from the proposal, end to end, plus runtime edge cases."""

import shutil
import subprocess
import sys

from covenant.events import Commit, Edit, Effect
from covenant.ir import Covenant
from covenant.runtime import Runtime

OLD_SIG = "def parse(text: str, strict: bool = False) -> Doc:"
NEW_SIG = "def parse(text: str, *, mode: str = 'lenient') -> Doc:"


def runtime(repo, tmp_path) -> Runtime:
    rt = Runtime(Covenant.load(repo / "covenant.yaml"), repo, state_dir=tmp_path / "state")
    rt.start()
    return rt


def run_tests_ok(rt):
    [(decision, output)] = rt.run_tests()
    assert decision.reports == [], output
    return decision


def test_worked_example(mylib_repo, tmp_path):
    rt = runtime(mylib_repo, tmp_path)
    core = mylib_repo / "mylib" / "core.py"
    original = core.read_text()

    # The agent adapts to parserlib 3.x and changes the exported signature.
    core.write_text(original.replace(OLD_SIG, NEW_SIG).replace("strict=strict", "strict=mode == 'strict'"))
    rt.record(Edit(path="mylib/core.py"))

    # Commit without testing: the trace monitor (C3) and API verifier (C1) both block.
    d = rt.record(Commit())
    assert not d.allowed
    assert {r.violation for r in d.reports} == {"C1", "C3"}
    c3 = next(r for r in d.reports if r.violation == "C3")
    assert c3.evidence["edits_since_last_passing_test"] == ["mylib/core.py"]
    assert "python -m pytest -q" in c3.repair_hints[0]

    # Tests pass, but the API change still blocks.
    run_tests_ok(rt)
    d = rt.record(Commit())
    assert [r.violation for r in d.reports] == ["C1"]
    c1 = d.reports[0]
    assert c1.blocked_action == "git commit"
    assert c1.evidence["symbol"] == "mylib.parse"
    assert "strict" in c1.evidence["before"] and "mode" in c1.evidence["after"]

    # The agent restores the signature, retests, and the commit goes through.
    core.write_text(original)
    rt.record(Edit(path="mylib/core.py"))
    run_tests_ok(rt)
    d = rt.record(Commit())
    assert d.allowed and d.reports == []


def test_script_deleting_user_data_is_rolled_back(mylib_repo, tmp_path):
    rt = runtime(mylib_repo, tmp_path)
    users = mylib_repo / "data" / "users.json"
    content = users.read_text()

    # A cleanup script the agent wrote removes the data folder; Covenant never saw the command.
    subprocess.run([sys.executable, "-c", "import shutil; shutil.rmtree('data')"], cwd=mylib_repo, check=True)
    d = rt.record(Edit(path="scripts/cleanup.py"))  # the next event triggers a scan

    assert [r.violation for r in d.reports] == ["C2"]
    assert d.reports[0].evidence == {"op": "delete", "target": "data/users.json", "channel": "fs", "rolled_back": True}
    assert users.read_text() == content

    # A resolved rollback does not block later work.
    run_tests_ok(rt)
    assert rt.record(Commit()).allowed


def test_overwrite_detected(mylib_repo, tmp_path):
    rt = runtime(mylib_repo, tmp_path)
    users = mylib_repo / "data" / "users.json"
    content = users.read_text()
    users.write_text("[]")
    d = rt.check()
    assert d.reports[0].evidence["op"] == "overwrite"
    assert users.read_text() == content


def test_db_effect_is_reported_and_blocks_commit(mylib_repo, tmp_path):
    rt = runtime(mylib_repo, tmp_path)
    d = rt.record(Effect(op="drop", target="users", channel="db"))
    assert not d.allowed
    assert d.reports[0].violation == "C2"
    assert "rolled_back" not in d.reports[0].evidence  # no DB snapshot in this scaffold


def test_state_survives_a_new_process(mylib_repo, tmp_path):
    rt = runtime(mylib_repo, tmp_path)
    rt.record(Edit(path="mylib/core.py"))
    run_tests_ok(rt)
    # A git hook runs in a fresh process: it rebuilds the monitors from trace.jsonl.
    fresh = Runtime(Covenant.load(mylib_repo / "covenant.yaml"), mylib_repo, state_dir=tmp_path / "state")
    assert fresh.record(Commit()).allowed


def test_baseline_is_pinned_at_start(mylib_repo, tmp_path):
    rt = runtime(mylib_repo, tmp_path)
    core = mylib_repo / "mylib" / "core.py"
    core.write_text(core.read_text().replace(OLD_SIG, NEW_SIG))
    # Even if the change sneaks into HEAD some other way, the baseline is the start commit.
    from conftest import git

    git(mylib_repo, "commit", "-qam", "sneaky")
    reports = rt.check().reports
    assert [r.violation for r in reports] == ["C1"]


def test_repeated_blocks_escalate(mylib_repo, tmp_path):
    rt = runtime(mylib_repo, tmp_path)
    rt.record(Edit(path="mylib/core.py"))
    codes = [rt.record(Commit()).exit_code for _ in range(5)]
    assert codes == [1, 1, 1, 1, 2]


def test_verifier_that_cannot_run_fails_closed(mylib_repo, tmp_path):
    rt = runtime(mylib_repo, tmp_path)
    shutil.rmtree(mylib_repo / "mylib")
    rt.run_tests()  # the tests fail now too; only C1's report matters here
    d = rt.record(Commit())
    assert not d.allowed
    assert any("error" in r.evidence for r in d.reports if r.violation == "C1")
