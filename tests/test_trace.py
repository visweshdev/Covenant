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


# ----- bounded and counting operators ----------------------------------------

E, T, C = Edit(path="a"), PASS, Commit()


def test_count_limits():
    assert run("G(#push <= 1)", [Push(), C, Push()]) == [True, True, False]
    assert run("#commit == 0", [E, C]) == [True, False]
    assert run("commit -> #test_pass >= 2", [T, C, T, C]) == [True, False, True, True]


def test_within_recent_steps():
    assert run("commit -> O[<=1] test_pass", [T, C]) == [True, True]
    assert run("commit -> O[<=1] test_pass", [T, E, C]) == [True, True, False]  # 2 steps back
    assert run("commit -> O[<1] test_pass", [T, C]) == [True, False]
    assert run("H[<=1] !edit", [E, C, C]) == [False, False, True]


def test_deadline_after_edit():
    deadline = "G(!(!test S[>=2] edit))"  # tests within 2 steps of any edit
    assert run(deadline, [E, C, T, C, C]) == [True, True, True, True, True]
    assert run(deadline, [E, C, C]) == [True, True, False]
    assert run(deadline, [E, E, C]) == [True, True, False]  # the first edit has gone 2 steps


def _since(a, b, cmp, n, events):
    """Reference meaning of a S[cmp n] b at the last step, by brute force."""
    import operator
    op = {"<=": operator.le, "<": operator.lt, ">=": operator.ge, ">": operator.gt}[cmp]
    t = len(events) - 1
    return any(b(events[j]) and op(t - j, n) and all(a(e) for e in events[j + 1:]) for j in range(t + 1))


@pytest.mark.parametrize("cmp", ["<=", "<", ">=", ">"])
def test_bounded_since_matches_definition(cmp):
    import itertools
    is_edit, not_test = (lambda e: e.type == "edit"), (lambda e: e.type != "test")
    for length in range(1, 7):
        for events in itertools.product([E, T, C], repeat=length):
            for n in range(4):
                got = run(f"!test S[{cmp}{n}] edit", list(events))
                want = [_since(not_test, is_edit, cmp, n, list(events[:k + 1])) for k in range(length)]
                assert got == want, (cmp, n, events)


def test_bounded_peek_does_not_change_state():
    monitor = TraceMonitor("G(#commit <= 1)")
    monitor.step(C)
    assert monitor.peek(C)[0] is False
    assert monitor.peek(C)[0] is False
    assert monitor.step(E) is True


@pytest.mark.parametrize("bad", ["#commit", "#commit <= x", "O[>=2] edit", "edit S[==1] commit", "O[<=2 edit", "Y[<=1] edit"])
def test_bad_bounded_formulas(bad):
    with pytest.raises(FormulaError):
        parse_formula(bad)
