"""Where Covenant keeps its state, and which files belong to Covenant itself.

State lives outside the repository by default (~/.covenant/workspaces/<repo>-<hash>),
so an agent working in the repo does not stumble over it. Set COVENANT_HOME to move it.
Real isolation needs the sandbox, where this folder is not writable by the agent.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def covenant_home() -> Path:
    return Path(os.environ.get("COVENANT_HOME") or Path.home() / ".covenant")


def default_state_dir(repo_root: Path) -> Path:
    resolved = os.path.normcase(os.path.abspath(repo_root))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    return covenant_home() / "workspaces" / f"{Path(resolved).name}-{digest}"


def claude_settings_files(repo_root: Path) -> list[Path]:
    return [repo_root / ".claude" / "settings.json", repo_root / ".claude" / "settings.local.json"]


def control_paths(repo_root: Path, covenant_path: Path, state_dir: Path) -> list[Path]:
    """Files and folders the agent must never modify."""
    return [
        covenant_path,
        state_dir,
        repo_root / ".covenant",
        repo_root / ".git" / "hooks",
        repo_root / ".git" / "config",
        *claude_settings_files(repo_root),
    ]


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def is_under(path: Path | str, roots: list[Path]) -> bool:
    p = _norm(path)
    for root in roots:
        r = _norm(root)
        if p == r or p.startswith(r.rstrip("\\/") + os.sep):
            return True
    return False
