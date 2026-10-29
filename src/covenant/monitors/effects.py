"""Effect monitor for EffectProhibition clauses.

Every side effect is normalized to (operation, target) and matched against the
clause's scope, so a deletion counts the same whether it came from `rm`, a
script the agent wrote, or a migration.

Clause params:
    deny:      operations to forbid, e.g. [delete, truncate, overwrite]
    paths:     globs of protected paths (repo-relative, or ~/...); ** crosses folders
    exclude:   globs carved out of `paths`
    db_tables: protected table names
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..events import Effect
from ..ir import Clause

# An operation also counts as the operations listed here. Renaming a protected
# file away removes it from where it was, so it counts as deleting it.
IMPLIED_OPS: dict[str, set[str]] = {
    "rename": {"delete"},
    "rmdir": {"delete"},
    "drop": {"delete", "truncate"},
}


def _glob_body(pattern: str) -> str:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def compile_glob(pattern: str) -> re.Pattern[str]:
    pattern = normalize_pattern(pattern)
    body = _glob_body(pattern)
    if pattern.endswith("/**"):
        # "data/**" also protects the folder "data" itself.
        body = f"(?:{_glob_body(pattern[:-3])}|{body})"
    return re.compile(f"^{body}$")


def normalize_pattern(pattern: str) -> str:
    pattern = pattern.replace("\\", "/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    return pattern


def normalize_path(path: str, repo_root: Path) -> str:
    """Repo-relative posix path when inside the repo, ~/... under home, else absolute."""
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        return os.path.normpath(expanded).replace("\\", "/")
    absolute = Path(os.path.abspath(expanded))
    for base, prefix in ((Path(os.path.abspath(repo_root)), ""), (Path.home(), "~/")):
        try:
            return prefix + absolute.relative_to(base).as_posix()
        except ValueError:
            continue
    return absolute.as_posix()


def static_prefix(pattern: str) -> str:
    """The part of a glob before its first wildcard, e.g. 'data' for 'data/**'."""
    parts = []
    for part in normalize_pattern(pattern).split("/"):
        if any(ch in part for ch in "*?["):
            break
        parts.append(part)
    return "/".join(parts)


def base_dir(pattern: str, repo_root: Path) -> Path:
    """The folder (or file) on disk that a protected-path glob starts from."""
    prefix = static_prefix(pattern)
    if prefix.startswith("~"):
        return Path(os.path.expanduser(prefix))
    return repo_root / prefix


class EffectMonitor:
    def __init__(self, clause: Clause, repo_root: Path):
        self.clause = clause
        self.repo_root = repo_root
        params = clause.params
        self.deny: set[str] = set(params.get("deny", ["delete"]))
        self.path_patterns: list[str] = list(params.get("paths", []))
        self.exclude_patterns: list[str] = list(params.get("exclude", []))
        self.tables: set[str] = set(params.get("db_tables", []))
        self._paths = [compile_glob(p) for p in self.path_patterns]
        self._exclude = [compile_glob(p) for p in self.exclude_patterns]

    def protects(self, target: str) -> bool:
        t = normalize_path(target, self.repo_root)
        return any(r.match(t) for r in self._paths) and not any(r.match(t) for r in self._exclude)

    def violates(self, effect: Effect) -> bool:
        ops = {effect.op} | IMPLIED_OPS.get(effect.op, set())
        if not ops & self.deny:
            return False
        if effect.channel == "db":
            return effect.target in self.tables
        return self.protects(effect.target)
