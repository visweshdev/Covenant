import os
import subprocess
import sys
from pathlib import Path

import pytest

from covenant import ci

from conftest import git

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def pr(mylib_repo):
    """The repo with a feature branch; returns (repo, base sha)."""
    base = git(mylib_repo, "rev-parse", "HEAD").stdout.strip()
    git(mylib_repo, "checkout", "-qb", "feature")
    return mylib_repo, base


def commit(repo, message="change"):
    git(repo, "add", "-A")
    assert git(repo, "commit", "-qm", message).returncode == 0


def run(repo, base, **kw):
    return ci.run_ci(repo, repo / "covenant.yaml", base, **kw)


def violations(result):
    return sorted({r.violation for r in result.reports})


def test_clean_pr_passes(pr):
    repo, base = pr
    (repo / "NOTES.md").write_text("hello\n")
    commit(repo)
    result = run(repo, base)
    assert violations(result) == [], ci.markdown_summary(result)
    assert "all rules hold" in ci.markdown_summary(result)


def test_api_change_fails(pr):
    repo, base = pr
    core = repo / "mylib" / "core.py"
    core.write_text(core.read_text().replace("def parse(text: str, strict: bool = False)", "def parse(text: str)").replace("strict=strict", "strict=False"))
    commit(repo)
    result = run(repo, base)
    assert violations(result) == ["C1"]
    assert "mylib.parse" in ci.markdown_summary(result)


def test_deleting_protected_file_fails(pr):
    repo, base = pr
    (repo / "data" / "users.json").unlink()
    commit(repo)
    result = run(repo, base, run_test_commands=False)
    assert violations(result) == ["C2"]
    assert result.reports[0].evidence == {"op": "delete", "target": "data/users.json", "channel": "git"}


def test_failing_tests_fail(pr):
    repo, base = pr
    (repo / "tests" / "test_core.py").write_text("def test_broken():\n    assert False\n")
    commit(repo)
    result = run(repo, base)
    assert violations(result) == ["C3"]


def test_pr_cannot_weaken_its_own_rules(pr):
    repo, base = pr
    cov = repo / "covenant.yaml"
    text = cov.read_text()
    cov.write_text(text[: text.index("    - id: C3")])  # drop the tests rule...
    (repo / "tests" / "test_core.py").write_text("def test_broken():\n    assert False\n")  # ...and break tests
    commit(repo)
    result = run(repo, base)
    assert violations(result) == ["C3", "INTEGRITY"]


def test_github_output(pr):
    repo, base = pr
    (repo / "data" / "users.json").unlink()
    commit(repo)
    lines = ci.github_annotations(run(repo, base, run_test_commands=False))
    assert lines[0].startswith("::error file=data/users.json,title=Covenant C2::")


def test_cli_ci_exit_codes(pr):
    repo, base = pr
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    cmd = [sys.executable, "-m", "covenant", "--repo", str(repo), "ci", "--base", base, "--no-tests"]
    clean = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert clean.returncode == 0, clean.stderr  # never let pytest print `env`: it holds credentials
    (repo / "data" / "users.json").unlink()
    commit(repo)
    r = subprocess.run(cmd + ["--format", "json"], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and '"C2"' in r.stdout
