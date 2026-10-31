"""Static checks on a Covenant that need no model: binding, parameters, and vacuity.

Contradiction checks between clauses belong to the compiler and come later.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .ir import ClauseStatus, Covenant
from .monitors.effects import base_dir
from .monitors.trace import FormulaError, parse_formula
from .verifiers.registry import PLANNED, REGISTRY


@dataclass(frozen=True)
class Issue:
    clause_id: str
    level: str  # "error" or "warning"
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.clause_id}: {self.message}"


def validate(covenant: Covenant, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for clause in covenant.clauses:
        def add(level: str, message: str) -> None:
            issues.append(Issue(clause.id, level, message))

        if clause.status is not ClauseStatus.ENFORCED:
            add("warning", f"not enforced (status: {clause.status.value}): {clause.source_span!r}")
            continue

        spec = REGISTRY.get(clause.checker or "")
        if spec is None:
            hint = " (planned, not built yet)" if clause.checker in PLANNED else ""
            add("error", f"unknown checker {clause.checker!r}{hint}; mark the clause unenforceable instead")
            continue
        if spec.kind is not clause.kind:
            add("error", f"checker {spec.name} enforces {spec.kind.value}, not {clause.kind.value}")
        for param in spec.required_params:
            if param not in clause.params:
                add("error", f"checker {spec.name} needs param {param!r}")
        if spec.name == "command_guard":
            from .verifiers.command_guard import CATEGORIES, unknown_categories

            for name in unknown_categories(clause):
                add("error", f"unknown command category {name!r}; known: {', '.join(CATEGORIES)}")

        if spec.name == "trace_monitor" and "formula" in clause.params:
            try:
                parse_formula(clause.params["formula"])
            except FormulaError as exc:
                add("error", f"bad formula: {exc}")
            if "test_" in clause.params["formula"] and "test_cmd" not in clause.params:
                add("warning", "no test_cmd; `covenant test` cannot run tests for this clause")
            test_cmd = clause.params.get("test_cmd")
            if test_cmd and shutil.which(test_cmd.split()[0]) is None:
                add("warning", f"test command {test_cmd.split()[0]!r} is not on PATH")

        elif spec.name == "effect_monitor":
            if not clause.params.get("paths") and not clause.params.get("db_tables"):
                add("error", "no paths or db_tables: the clause protects nothing")
            for pattern in clause.params.get("paths", []):
                if not base_dir(pattern, repo_root).exists():
                    add("warning", f"path {pattern!r} matches nothing yet (vacuous until it exists)")

        elif spec.name == "api_surface_diff" and "package" in clause.params:
            pkg = clause.params["package"].replace(".", "/")
            base = repo_root / clause.params.get("root", "")
            if not (base / pkg / "__init__.py").is_file() and not (base / f"{pkg}.py").is_file():
                add("error", f"package {clause.params['package']!r} not found under {repo_root}")
    return issues
