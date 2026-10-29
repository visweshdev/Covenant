import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import git

SRC = Path(__file__).resolve().parent.parent / "src"


def cli(repo, state, *args):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    return subprocess.run(
        [sys.executable, "-m", "covenant", "--repo", str(repo), "--state-dir", str(state), *args],
        capture_output=True, text=True, env=env,
    )


def test_validate(mylib_repo, tmp_path):
    result = cli(mylib_repo, tmp_path / "state", "validate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 errors" in result.stdout


def test_record_returns_json_and_exit_code(mylib_repo, tmp_path):
    state = tmp_path / "state"
    assert cli(mylib_repo, state, "start").returncode == 0
    assert cli(mylib_repo, state, "record", "edit", "mylib/core.py").returncode == 0
    result = cli(mylib_repo, state, "record", "commit")
    assert result.returncode == 1
    decision = json.loads(result.stdout)
    assert decision["allowed"] is False
    assert decision["reports"][0]["violation"] == "C3"


def test_git_hook_blocks_untested_commit(mylib_repo, tmp_path):
    state = tmp_path / "state"
    assert cli(mylib_repo, state, "install-hooks").returncode == 0
    assert cli(mylib_repo, state, "start").returncode == 0

    readme = mylib_repo / "README.md"
    readme.write_text("notes\n")
    cli(mylib_repo, state, "record", "edit", "README.md")
    git(mylib_repo, "add", "README.md")

    blocked = git(mylib_repo, "commit", "-qm", "untested")
    assert blocked.returncode != 0
    assert "C3" in blocked.stderr

    assert cli(mylib_repo, state, "test").returncode == 0
    ok = git(mylib_repo, "commit", "-qm", "tested")
    assert ok.returncode == 0, ok.stderr


def test_committing_only_setup_files_needs_no_tests(mylib_repo, tmp_path):
    state = tmp_path / "state"
    assert cli(mylib_repo, state, "install-hooks").returncode == 0
    assert cli(mylib_repo, state, "install-claude").returncode == 0
    assert cli(mylib_repo, state, "start").returncode == 0
    git(mylib_repo, "add", ".claude/settings.json")
    setup = git(mylib_repo, "commit", "-qm", "add covenant hooks")
    assert setup.returncode == 0, setup.stderr
    (mylib_repo / "notes.txt").write_text("x\n")
    git(mylib_repo, "add", "notes.txt")
    assert git(mylib_repo, "commit", "-qm", "work").returncode != 0  # ordinary commits still need tests
