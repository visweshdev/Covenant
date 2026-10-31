"""Undo snapshots of the whole working tree."""

from pathlib import Path

import pytest

from covenant import snapshots
from covenant.integrations.claude_code import ClaudeCodeHook
from covenant.ir import Covenant, Goal
from covenant.runtime import Runtime

from conftest import git


def test_take_and_restore_including_untracked_files(mylib_repo, tmp_path):
    state = tmp_path / "state"
    (mylib_repo / "notes.txt").write_text("draft\n", encoding="utf-8")  # untracked work
    first = snapshots.take(mylib_repo, state, "before the mess")
    assert first["id"] == 1
    assert snapshots.take(mylib_repo, state, "again") is None  # nothing changed

    init = mylib_repo / "mylib" / "__init__.py"
    original = init.read_text(encoding="utf-8")
    init.write_text("broken\n", encoding="utf-8")
    (mylib_repo / "notes.txt").unlink()
    (mylib_repo / "junk.tmp").write_text("x", encoding="utf-8")
    assert set(snapshots.changed_since(mylib_repo, state, 1)) == {"mylib/__init__.py", "notes.txt", "junk.tmp"}

    result = snapshots.restore(mylib_repo, state, 1)
    assert init.read_text(encoding="utf-8") == original
    assert (mylib_repo / "notes.txt").read_text(encoding="utf-8") == "draft\n"
    assert not (mylib_repo / "junk.tmp").exists()
    assert result["removed"] == ["junk.tmp"]

    # the undo can be undone
    snapshots.restore(mylib_repo, state, result["undo_with"])
    assert init.read_text(encoding="utf-8") == "broken\n"
    assert (mylib_repo / "junk.tmp").exists()


def test_snapshots_leave_branch_index_and_status_alone(mylib_repo, tmp_path):
    head = git(mylib_repo, "rev-parse", "HEAD").stdout
    (mylib_repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    snapshots.take(mylib_repo, tmp_path / "s", "x")
    assert git(mylib_repo, "rev-parse", "HEAD").stdout == head
    assert git(mylib_repo, "diff", "--cached", "--name-only").stdout == ""
    assert "refs/covenant/snapshots/1" in git(mylib_repo, "for-each-ref", "refs/covenant").stdout


def test_ignored_files_are_left_out(mylib_repo, tmp_path):
    (mylib_repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (mylib_repo / "debug.log").write_text("big\n", encoding="utf-8")
    snapshots.take(mylib_repo, tmp_path / "s", "x")
    (mylib_repo / "debug.log").write_text("changed\n", encoding="utf-8")
    snapshots.restore(mylib_repo, tmp_path / "s", 1)
    assert (mylib_repo / "debug.log").read_text(encoding="utf-8") == "changed\n"


def test_old_snapshots_are_pruned(mylib_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "KEEP", 3)
    for n in range(5):
        (mylib_repo / "f.txt").write_text(str(n), encoding="utf-8")
        snapshots.take(mylib_repo, tmp_path / "s", f"n{n}")
    assert [s["id"] for s in snapshots.listing(tmp_path / "s")] == [3, 4, 5]
    with pytest.raises(ValueError):
        snapshots.get(tmp_path / "s", 1)


def test_the_hook_snapshots_before_risky_commands(mylib_repo, monkeypatch):
    monkeypatch.setenv("COVENANT_SNAPSHOTS", "1")
    cov = Covenant(instruction="x", goal=Goal(action="x"), clauses=[])
    cov.dump(mylib_repo / "covenant.yaml")
    rt = Runtime(cov, mylib_repo)
    rt.start()
    hook = ClaudeCodeHook(rt)

    def pre(command):
        return hook.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}})

    pre("ls -la")
    assert snapshots.listing(rt.state_dir) == []
    pre("rm -f mylib/__init__.py")
    assert snapshots.listing(rt.state_dir)[-1]["label"].startswith("before: rm -f")
    monkeypatch.setenv("COVENANT_SNAPSHOTS", "0")
    (mylib_repo / "x.txt").write_text("1", encoding="utf-8")
    pre("git reset --hard")
    assert len(snapshots.listing(rt.state_dir)) == 1
