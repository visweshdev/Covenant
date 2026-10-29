import pytest

from covenant.events import Commit, Edit, Push, Run, TestRun
from covenant.monitors.trace import FormulaError, TraceMonitor, parse_formula

TESTS_BEFORE_COMMIT = "G(commit -> (!edit S test_pass))"
PASS = TestRun(cmd="pytest", exit_code=0)
FAIL = TestRun(cmd="pytest", exit_code=1)


def run(formula, events):
    monitor = TraceMonitor(formula)
    return [monitor.step(e) for e in events]


def test_commit_after_passing_test_is_fine():
    assert run(TESTS_BEFORE_COMMIT, [Edit(path="a"), PASS, Commit()]) == [True, True, True]


def test_commit_with_no_test_is_violation():
    assert run(TESTS_BEFORE_COMMIT, [Edit(path="a"), Commit()])[-1] is False


def test_commit_with_edit_after_test_is_violation():
    assert run(TESTS_BEFORE_COMMIT, [PASS, Edit(path="a"), Commit()])[-1] is False


def test_failing_test_does_not_count():
    assert run(TESTS_BEFORE_COMMIT, [Edit(path="a"), FAIL, Commit()])[-1] is False


def test_unrelated_events_do_not_break_since():
    events = [Edit(path="a"), PASS, Run(cmd="ls", exit_code=0), Commit()]
    assert run(TESTS_BEFORE_COMMIT, events)[-1] is True


def test_peek_does_not_change_state():
    monitor = TraceMonitor(TESTS_BEFORE_COMMIT)
    monitor.step(Edit(path="a"))
    ok, _ = monitor.peek(Commit())
    assert ok is False
    monitor.step(PASS)  # the refused commit never happened
    assert monitor.step(Commit()) is True


def test_yesterday_once_historically():
    assert run("Y edit", [Edit(path="a"), Commit()]) == [False, True]
    assert run("push -> O commit", [Push()]) == [False]
    assert run("push -> O commit", [Commit(), PASS, Push()]) == [True, True, True]
    assert run("H !push", [Commit(), Push(), Commit()]) == [True, False, False]


@pytest.mark.parametrize(
    "bad",
    ["", "commit ->", "G(commit", "frobnicate", "commit -> G(edit)", "Commit", "commit $ edit"],
)
def test_bad_formulas(bad):
    with pytest.raises(FormulaError):
        parse_formula(bad)
