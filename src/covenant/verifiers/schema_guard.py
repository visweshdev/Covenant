"""schema_guard: no new or changed migration may destroy data or narrow a schema.

Scans migration files added or changed since the baseline (committed or not) for
destructive operations: dropped tables or columns, truncation, unqualified deletes,
and column type changes (flagged for review, since narrowing loses data).
Understands Alembic and Django migrations and raw SQL (Prisma, Flyway, Rails SQL).

Clause params:
    dirs:     migration folders (default: common locations)
    allow:    operation names to permit, e.g. ["drop_index"]
    baseline: git ref (the runtime pins it to a sha at start)
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..feedback import ViolationReport
from ..ir import Clause

DEFAULT_DIRS = ["alembic/versions", "migrations", "prisma/migrations", "db/migrate", "db/migrations", "sql", "flyway"]

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("drop_table", re.compile(r"\bop\.drop_table\s*\(|\bmigrations\.DeleteModel\s*\(|\bDROP\s+TABLE\b", re.I)),
    ("drop_column", re.compile(r"\bop\.drop_column\s*\(|\bmigrations\.RemoveField\s*\(|\bDROP\s+COLUMN\b|\bremove_column\b", re.I)),
    ("truncate", re.compile(r"\bTRUNCATE\b", re.I)),
    ("delete_rows", re.compile(r"\bDELETE\s+FROM\s+\S+\s*(?:;|$)", re.I | re.M)),
    ("change_type", re.compile(r"\balter_column\s*\([^)]*\btype_\s*=|\bALTER\s+COLUMN\s+\S+\s+(?:SET\s+DATA\s+)?TYPE\b|\bmigrations\.AlterField\s*\(|\bchange_column\b", re.I | re.S)),
    ("drop_schema", re.compile(r"\bDROP\s+(?:SCHEMA|DATABASE)\b", re.I)),
]
MIGRATION_SUFFIXES = (".py", ".sql", ".rb")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8").stdout


def changed_migrations(repo: Path, baseline: str, dirs: list[str]) -> list[str]:
    existing = [d for d in dirs if (repo / d).is_dir()]
    if not existing:
        return []
    committed = _git(repo, "diff", "--name-only", "--diff-filter=AM", baseline, "--", *existing).splitlines()
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "--", *existing).splitlines()
    files = dict.fromkeys(p.strip() for p in committed + untracked if p.strip().endswith(MIGRATION_SUFFIXES))
    return [f for f in files if (repo / f).is_file()]


def findings(text: str, allow: set[str]) -> list[tuple[str, int, str]]:
    out = []
    for op, pattern in PATTERNS:
        if op in allow:
            continue
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            line = text.splitlines()[line_no - 1].strip() if text.splitlines() else ""
            out.append((op, line_no, line[:200]))
    return out


def verify(clause: Clause, repo_root: Path, baseline: str | None = None) -> list[ViolationReport]:
    params = clause.params
    ref = baseline or params.get("baseline", "HEAD")
    allow = set(params.get("allow", []))
    reports = []
    for rel in changed_migrations(repo_root, ref, list(params.get("dirs", DEFAULT_DIRS))):
        text = (repo_root / rel).read_text(encoding="utf-8", errors="ignore")
        for op, line_no, line in findings(text, allow):
            hint = (
                "Review the type change: narrowing a column can lose data. If it widens, add it to params.allow"
                if op == "change_type"
                else "Keep the old table/column (mark it deprecated) or move the destructive step to a separate, human-reviewed migration"
            )
            reports.append(ViolationReport.for_clause(
                clause, {"target": rel, "line": line_no, "operation": op, "code": line}, hints=[hint]))
    return reports
