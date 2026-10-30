"""A portable stand-in for the sandbox's copy-on-write layer.

At session start it copies every protected file aside and records its hash. A
scan then finds deletes, truncations and overwrites by comparing against that
record, whatever caused them, and a protected file can be restored from its copy.

In the real sandbox this is replaced by overlayfs or btrfs/ZFS snapshots plus
fanotify/eBPF events; the interface (take / scan / restore) stays the same.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..events import Effect
from .effects import EffectMonitor, base_dir, normalize_path

MISSING = "<missing>"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class CopySnapshotter:
    def __init__(self, state_dir: Path, repo_root: Path):
        self.repo_root = repo_root
        self.dir = state_dir / "snapshot"
        self.manifest_path = self.dir / "manifest.json"
        self.manifest: dict[str, dict] = {}
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def protected_files(self, monitors: Iterable[EffectMonitor]) -> Iterator[Path]:
        seen: set[Path] = set()
        for monitor in monitors:
            for pattern in monitor.path_patterns:
                base = base_dir(pattern, self.repo_root)
                candidates = [base] if base.is_file() else (base.rglob("*") if base.is_dir() else [])
                for path in candidates:
                    if path.is_file() and path not in seen and monitor.protects(str(path)):
                        seen.add(path)
                        yield path

    def take(self, monitors: Iterable[EffectMonitor]) -> int:
        shutil.rmtree(self.dir, ignore_errors=True)
        (self.dir / "files").mkdir(parents=True)
        self.manifest = {}
        for n, path in enumerate(self.protected_files(monitors)):
            copy = self.dir / "files" / str(n)
            shutil.copy2(path, copy)
            target = normalize_path(str(path), self.repo_root)
            self.manifest[target] = {
                "path": str(path),
                "copy": str(copy),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
                "acknowledged": None,
            }
        self._save()
        return len(self.manifest)

    def extend(self, monitors: Iterable[EffectMonitor]) -> int:
        """Copy aside files that newly added rules protect, keeping the existing copies."""
        (self.dir / "files").mkdir(parents=True, exist_ok=True)
        added = 0
        for path in self.protected_files(monitors):
            target = normalize_path(str(path), self.repo_root)
            if target in self.manifest:
                continue
            copy = self.dir / "files" / f"a{len(self.manifest)}"
            shutil.copy2(path, copy)
            self.manifest[target] = {
                "path": str(path), "copy": str(copy), "sha256": _sha256(path),
                "size": path.stat().st_size, "acknowledged": None,
            }
            added += 1
        self._save()
        return added

    def _current_state(self, entry: dict) -> str:
        path = Path(entry["path"])
        return _sha256(path) if path.is_file() else MISSING

    def scan(self) -> list[Effect]:
        """Effects on protected files since the snapshot, skipping ones already reported."""
        effects = []
        for target, entry in self.manifest.items():
            state = self._current_state(entry)
            if state == entry["sha256"] or state == entry["acknowledged"]:
                continue
            if state == MISSING:
                op = "delete"
            elif Path(entry["path"]).stat().st_size == 0 and entry["size"] > 0:
                op = "truncate"
            else:
                op = "overwrite"
            effects.append(Effect(op=op, target=target))
        return effects

    def restore(self, target: str) -> bool:
        entry = self.manifest.get(normalize_path(target, self.repo_root))
        if entry is None:
            return False
        dest = Path(entry["path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry["copy"], dest)
        entry["acknowledged"] = None
        self._save()
        return True

    def acknowledge(self, target: str) -> None:
        """Stop re-reporting a change that was reported but not rolled back."""
        entry = self.manifest.get(normalize_path(target, self.repo_root))
        if entry is not None:
            entry["acknowledged"] = self._current_state(entry)
            self._save()

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
