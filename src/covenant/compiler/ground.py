"""Grounding: resolve abstract terms ("public API", "user data", "tests") against the repo.

Everything here is deterministic and reads files only.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..verifiers import source_api, ts_api_surface

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", ".tox", ".mypy_cache",
             ".pytest_cache", "site-packages", ".next", "coverage", ".covenant"}
NOT_PACKAGES = {"tests", "test", "docs", "doc", "examples", "example", "scripts", "benchmarks", "migrations", "alembic"}
DATA_DIRS = ["data", "storage", "uploads", "user_data", "userdata", "var", "db", "instance", "media"]
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
MIGRATION_DIRS = ["alembic/versions", "migrations", "prisma/migrations", "db/migrate", "db/migrations"]
TEST_RUNNERS = re.compile(
    r"\b(pytest|python -m pytest|tox|nox|npm (run )?test|yarn test|pnpm (run )?test|npx (jest|vitest)|jest|vitest|"
    r"go test|cargo test|make test|bun test)\b"
)


@dataclass
class PythonPackage:
    name: str
    root: str  # "" or "src"


@dataclass
class RepoFacts:
    root: Path
    python_packages: list[PythonPackage] = field(default_factory=list)
    ts_entries: list[str] = field(default_factory=list)
    data_paths: list[str] = field(default_factory=list)
    db_tables: list[str] = field(default_factory=list)
    source_language: str | None = None  # go, rust or java, checked from source
    test_commands: list[tuple[str, str]] = field(default_factory=list)  # (command, where it was found)
    migration_dirs: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Repository facts for {self.root.name}:"]
        pkgs = ", ".join(f"{p.root + '/' if p.root else ''}{p.name}" for p in self.python_packages) or "none"
        lines.append(f"- Python packages: {pkgs}")
        lines.append(f"- TypeScript entry files: {', '.join(self.ts_entries) or 'none'}")
        if self.source_language:
            lines.append(f"- {self.source_language.capitalize()} project (public API read from source)")
        lines.append(f"- User data locations: {', '.join(self.data_paths) or 'none found'}")
        lines.append(f"- Database tables: {', '.join(self.db_tables) or 'none found'}")
        tests = "; ".join(f"`{c}` (from {s})" for c, s in self.test_commands) or "none found"
        lines.append(f"- Test commands: {tests}")
        lines.append(f"- Migration folders: {', '.join(self.migration_dirs) or 'none'}")
        return "\n".join(lines)


def _walk(root: Path, max_depth: int = 4):
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".covenant")]
        if len(Path(dirpath).parts) - root_depth >= max_depth:
            dirnames[:] = []
        yield Path(dirpath), filenames


def find_python_packages(root: Path) -> list[PythonPackage]:
    found = []
    for base, prefix in ((root, ""), (root / "src", "src")):
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "__init__.py").is_file() and child.name not in NOT_PACKAGES:
                if not child.name.startswith((".", "_")):
                    found.append(PythonPackage(child.name, prefix))
    return found


def find_ts_entries(root: Path) -> list[str]:
    if not (root / "package.json").is_file() and not (root / "tsconfig.json").is_file():
        return []
    try:
        return ts_api_surface.detect_entries(root)
    except Exception:
        return []


def find_data_paths(root: Path, packages: list[PythonPackage]) -> list[str]:
    paths = [f"{d}/**" for d in DATA_DIRS if (root / d).is_dir()]
    for dirpath, filenames in _walk(root, max_depth=3):
        for name in filenames:
            if name.endswith(DB_SUFFIXES):
                rel = (dirpath / name).relative_to(root).as_posix()
                if not any(rel.startswith(p[:-3] + "/") for p in paths):
                    paths.append(rel)
    for pkg in packages:
        home = Path.home() / f".{pkg.name}"
        if home.is_dir():
            paths.append(f"~/.{pkg.name}/**")
    return paths


def find_db_tables(root: Path) -> list[str]:
    tables: list[str] = []
    patterns = [
        (".py", re.compile(r"__tablename__\s*=\s*[\"'](\w+)[\"']")),
        (".prisma", re.compile(r"^\s*model\s+(\w+)\s*\{", re.M)),
        (".sql", re.compile(r"create\s+table\s+(?:if\s+not\s+exists\s+)?[\"`]?(\w+)", re.I)),
    ]
    for dirpath, filenames in _walk(root):
        for name in filenames:
            for suffix, pattern in patterns:
                if name.endswith(suffix):
                    try:
                        text = (dirpath / name).read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    tables += pattern.findall(text)
    return list(dict.fromkeys(tables))


def find_test_commands(root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        for wf in sorted(workflows.glob("*.y*ml")):
            for line in wf.read_text(encoding="utf-8", errors="ignore").splitlines():
                cmd = line.strip()
                if cmd.startswith("- "):
                    cmd = cmd[2:].strip()
                if cmd.startswith("run:"):
                    cmd = cmd[4:].strip()
                cmd = cmd.strip("'\"|> ").strip()
                if cmd and TEST_RUNNERS.search(cmd) and not cmd.startswith(("#", "-", "name:", "uses:")):
                    found.append((cmd, f".github/workflows/{wf.name}"))
    makefile = root / "Makefile"
    if makefile.is_file() and re.search(r"^test\s*:", makefile.read_text(encoding="utf-8", errors="ignore"), re.M):
        found.append(("make test", "Makefile"))
    for marker, cmd in (("go.mod", "go test ./..."), ("Cargo.toml", "cargo test"), ("pom.xml", "mvn -q test"),
                        ("build.gradle", "./gradlew test"), ("build.gradle.kts", "./gradlew test")):
        if (root / marker).is_file():
            found.append((cmd, marker))
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            if json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}).get("test"):
                found.append(("npm test", "package.json"))
        except ValueError:
            pass
    pyproject = root / "pyproject.toml"
    has_pytest = (root / "pytest.ini").is_file() or (root / "tests").is_dir()
    if pyproject.is_file():
        try:
            has_pytest |= "pytest" in tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {})
        except (tomllib.TOMLDecodeError, OSError):
            pass
    if has_pytest:
        found.append(("python -m pytest -q", "tests/ folder" if (root / "tests").is_dir() else "pytest config"))
    unique: dict[str, str] = {}
    for cmd, src in found:
        unique.setdefault(cmd, src)
    return list(unique.items())


def ground(root: Path) -> RepoFacts:
    root = root.resolve()
    packages = find_python_packages(root)
    return RepoFacts(
        root=root,
        python_packages=packages,
        ts_entries=find_ts_entries(root),
        source_language=source_api.detect_language(root),
        data_paths=find_data_paths(root, packages),
        db_tables=find_db_tables(root),
        test_commands=find_test_commands(root),
        migration_dirs=[d for d in MIGRATION_DIRS if (root / d).is_dir()],
    )
