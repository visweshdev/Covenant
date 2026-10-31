"""Undo snapshots: restore points for the whole working tree, taken before risky steps.

Before a shell command that can delete or overwrite files (rm, mv, git reset/checkout/clean,
sed -i, redirects, ...) and before a file is rewritten whole, Covenant records the working
tree as a git commit under refs/covenant/snapshots/. Untracked files are included; files git
ignores are not. Nothing touches your branch, your index or your stash, and the refs are
never pushed.

    covenant snapshots          list restore points
    covenant snapshot [label]   take one now
    covenant undo [ID]          put the working tree back as it was (default: the latest)

Restoring first takes a snapshot of the current state, so an undo can itself be undone.
Set COVENANT_SNAPSHOTS=0 to turn automatic snapshots off.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REF = "refs/covenant/snapshots"
KEEP = 50
RISKY = re.compile(
    r"(?:^|[;&|(\s])(?:rm|rmdir|mv|del|erase|rd|truncate|shred|dd|unlink)\s|>(?!&)|\bsed\s+-i|\bperl\s+-[a-z]*i|"
    r"\bgit\s+(?:reset|checkout|clean|restore|stash|rebase|merge|cherry-pick|revert|apply|am|switch|rm|mv)\b|"
    r"Remove-Item|Move-Item|Set-Content|Clear-Content|Out-File|Rename-Item|\.unlink\(|shutil\.rmtree|os\.remove|"
    r"\b(?:prettier|black|ruff\s+format|isort|gofmt|eslint\s+--fix|cargo\s+fmt)\b",
    re.I,
)


def enabled() -> bool:
    return os.environ.get("COVENANT_SNAPSHOTS", "1").strip().lower() not in ("0", "false", "no", "off")


def _git(repo: Path, *args: str, index: Path | None = None, check: bool = True) -> str:
    env = dict(os.environ)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def _list_file(state_dir: Path) -> Path:
    return state_dir / "snapshots.json"


def listing(state_dir: Path) -> list[dict[str, Any]]:
    try:
        return json.loads(_list_file(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _tree_of_worktree(repo: Path, state_dir: Path) -> str:
    """The working tree as a git tree, built in Covenant's own index (kept between snapshots
    so unchanged files aren't hashed again)."""
    index = state_dir / "snapshot.index"
    state_dir.mkdir(parents=True, exist_ok=True)
    if not index.exists():
        has_head = _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
        if has_head:
            _git(repo, "read-tree", "HEAD", index=index)
    _git(repo, "add", "-A", "--", ".", index=index)
    return _git(repo, "write-tree", index=index)


def take(repo: Path, state_dir: Path, label: str, force: bool = False) -> dict[str, Any] | None:
    """Record the working tree. Returns the new snapshot, or None if nothing changed since the last."""
    repo = repo.resolve()
    tree = _tree_of_worktree(repo, state_dir)
    snaps = listing(state_dir)
    if snaps and snaps[-1]["tree"] == tree and not force:
        return None
    head = _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    parents = ["-p", head] if head else []
    sha = _git(repo, "commit-tree", tree, *parents, "-m", f"covenant snapshot: {label}")
    number = (snaps[-1]["id"] + 1) if snaps else 1
    _git(repo, "update-ref", f"{REF}/{number}", sha)
    snap = {"id": number, "sha": sha, "tree": tree, "label": label[:200], "head": head or None,
            "at": datetime.now(timezone.utc).isoformat()}
    snaps.append(snap)
    for old in snaps[:-KEEP]:
        _git(repo, "update-ref", "-d", f"{REF}/{old['id']}", check=False)
    _list_file(state_dir).write_text(json.dumps(snaps[-KEEP:], indent=1), encoding="utf-8")
    return snap


def maybe_take(repo: Path, state_dir: Path, label: str) -> dict[str, Any] | None:
    """Take a snapshot if automatic snapshots are on; never let a failure stop the agent."""
    if not enabled():
        return None
    try:
        return take(repo, state_dir, label)
    except Exception:
        return None


def get(state_dir: Path, snapshot_id: int | None = None) -> dict[str, Any]:
    snaps = listing(state_dir)
    if not snaps:
        raise ValueError("no snapshots yet")
    if snapshot_id is None:
        return snaps[-1]
    for snap in snaps:
        if snap["id"] == snapshot_id:
            return snap
    raise ValueError(f"no snapshot {snapshot_id}; `covenant snapshots` lists them")


def _files(repo: Path, tree_or_index: str | None = None, index: Path | None = None) -> set[str]:
    if tree_or_index:
        out = _git(repo, "ls-tree", "-r", "--name-only", "-z", tree_or_index)
    else:
        out = _git(repo, "ls-files", "-z", index=index)
    return {p for p in out.split("\0") if p}


def changed_since(repo: Path, state_dir: Path, snapshot_id: int | None = None) -> list[str]:
    """Files that differ between a snapshot and the working tree now."""
    snap = get(state_dir, snapshot_id)
    now = _tree_of_worktree(repo.resolve(), state_dir)
    out = _git(repo, "diff-tree", "-r", "--name-only", "-z", snap["tree"], now)
    return [p for p in out.split("\0") if p]


def restore(repo: Path, state_dir: Path, snapshot_id: int | None = None) -> dict[str, Any]:
    """Put the working tree back as it was at a snapshot. Returns what changed."""
    repo = repo.resolve()
    snap = get(state_dir, snapshot_id)
    before = take(repo, state_dir, f"before undo to snapshot {snap['id']}", force=True)
    current = _files(repo, before["tree"])
    wanted = _files(repo, snap["tree"])
    index = state_dir / "restore.index"
    index.unlink(missing_ok=True)
    _git(repo, "read-tree", snap["tree"], index=index)
    _git(repo, "checkout-index", "-a", "-f", index=index)
    index.unlink(missing_ok=True)
    removed = sorted(current - wanted)
    for rel in removed:
        path = repo / rel
        try:
            path.unlink()
        except OSError:
            continue
        parent = path.parent
        while parent != repo and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    changed = _git(repo, "diff-tree", "-r", "--name-only", "-z", before["tree"], snap["tree"]).split("\0")
    (state_dir / "snapshot.index").unlink(missing_ok=True)  # rebuilt on the next snapshot
    return {"restored": snap, "undo_with": before["id"], "changed": [p for p in changed if p], "removed": removed}
