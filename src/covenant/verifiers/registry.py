"""Which checkers exist, and which clause kind each one can enforce.

Expressiveness grows by adding checkers here, not by growing the logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ir import ClauseKind


@dataclass(frozen=True)
class CheckerSpec:
    name: str
    kind: ClauseKind
    description: str
    required_params: tuple[str, ...] = ()


REGISTRY: dict[str, CheckerSpec] = {
    spec.name: spec
    for spec in [
        CheckerSpec(
            "effect_monitor",
            ClauseKind.EFFECT_PROHIBITION,
            "Watches file and database effects; blocks or rolls back protected ones",
        ),
        CheckerSpec(
            "trace_monitor",
            ClauseKind.TEMPORAL_OBLIGATION,
            "Checks an ordering rule (past-time LTL) over the event trace",
            ("formula",),
        ),
        CheckerSpec(
            "api_surface_diff",
            ClauseKind.SEMANTIC_INVARIANT,
            "Exported names and signatures unchanged versus a baseline commit",
            ("package",),
        ),
        CheckerSpec(
            "schema_guard",
            ClauseKind.SEMANTIC_INVARIANT,
            "New or changed migrations drop no tables or columns and change no column types",
        ),
        CheckerSpec(
            "dependency_diff",
            ClauseKind.SEMANTIC_INVARIANT,
            "Only allowed dependencies change, and locked versions keep their major version",
        ),
        CheckerSpec(
            "source_api_diff",
            ClauseKind.SEMANTIC_INVARIANT,
            "Go, Rust or Java public API unchanged versus a baseline commit (read from source)",
        ),
        CheckerSpec(
            "sqlite_guard",
            ClauseKind.SEMANTIC_INVARIANT,
            "Rows, tables and columns in SQLite databases are never lost; inserts are allowed",
            ("databases",),
        ),
        CheckerSpec(
            "ts_api_surface_diff",
            ClauseKind.SEMANTIC_INVARIANT,
            "TypeScript exported names and types unchanged versus a baseline commit",
        ),
    ]
}

# Named in the proposal but not built yet. Clauses bound to these are reported as unenforceable.
PLANNED: set[str] = set()
