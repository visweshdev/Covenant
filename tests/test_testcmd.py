"""Which shell commands count as running the test suite. Cases come from real agent runs."""

import pytest

from covenant.testcmd import match

PY = "python -m pytest -q"


@pytest.mark.parametrize("command", [
    PY,
    "python -m pytest -q -p no:cacheprovider",  # seen on the TinyDB trial
    "python -m pytest -q -p no:cacheprovider -x",
    "python -m pytest",
    "pytest -xvs",
    "py.test -v --cov=tinydb --cov-report=term",
    "python3 -m pytest --tb=short -rA --maxfail=1",
    "pytest -n auto --durations=10",
    "python -m pytest -q 2>&1",
    "python -m pytest -q > out.txt 2>&1",
    "cd . && python -m pytest -q",
    "python -m pytest -q -W error",
])
def test_whole_suite_runs_count(command):
    assert match(command, [PY]).cmd == PY


@pytest.mark.parametrize(("command", "reason"), [
    ("python -m pytest -q tests/test_pop.py", "tests/test_pop.py"),
    ("pytest -k pop", "-k pop"),
    ("pytest -m 'not slow'", "-m"),
    ("pytest --lf", "--lf"),
    ("pytest --deselect tests/test_x.py::test_y", "--deselect"),
    ("python -m pytest -q | tail -5", "hidden"),
    ("python -m pytest -q || true", "hidden"),
    ("python -m pytest -q &", "hidden"),
])
def test_partial_or_hidden_runs_dont_count(command, reason):
    m = match(command, [PY])
    assert m.cmd is None and reason in m.why_not


def test_other_commands_are_not_test_runs():
    assert match("ls -la", [PY]).cmd is None and match("ls -la", [PY]).why_not is None
    assert match("git status", [PY]).why_not is None


def test_configured_flags_and_other_runners():
    assert match("npm test", ["npm test"]).cmd == "npm test"
    assert match("npm test -- --silent", ["npm test"]).cmd is None
    assert match("go test -v -count=1 ./...", ["go test ./..."]).cmd == "go test ./..."
    assert match("go test ./pkg/...", ["go test ./..."]).cmd is None
    # A configured command that itself selects tests must be run with the same selection.
    assert match("pytest tests/unit -q", ["pytest tests/unit"]).cmd == "pytest tests/unit"
    assert match("pytest", ["pytest tests/unit"]).cmd is None
