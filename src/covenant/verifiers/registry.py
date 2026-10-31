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
        CheckerSpec("scope_guard", ClauseKind.SEMANTIC_INVARIANT, "Only files matching `allow` change", ("allow",)),
        CheckerSpec("secret_scan", ClauseKind.SEMANTIC_INVARIANT, "No keys, tokens, private keys or passwords in added code"),
        CheckerSpec("test_guard", ClauseKind.SEMANTIC_INVARIANT, "Tests aren't deleted, removed, skipped or focused"),
        CheckerSpec("diff_budget", ClauseKind.SEMANTIC_INVARIANT, "The change stays within max_files and max_lines"),
        CheckerSpec("no_debug_code", ClauseKind.SEMANTIC_INVARIANT, "No breakpoints, debugger statements or debug prints"),
        CheckerSpec("no_suppressions", ClauseKind.SEMANTIC_INVARIANT, "No new type-check or lint suppressions"),
        CheckerSpec("large_files", ClauseKind.SEMANTIC_INVARIANT, "No added or changed file over max_kb"),
        CheckerSpec("command_guard", ClauseKind.EFFECT_PROHIBITION, "Denied shell commands are refused before they run", ("deny",)),
        CheckerSpec("budget", ClauseKind.EFFECT_PROHIBITION,
                    "The task stops at a time, action or cost limit (max_minutes, max_actions, max_cost_usd)"),
        CheckerSpec("path_owner", ClauseKind.SEMANTIC_INVARIANT,
                    "Owned folders change only by their owning agent (COVENANT_AGENT)", ("owners",)),
        CheckerSpec("llm_review", ClauseKind.SEMANTIC_INVARIANT, "Claude reviews the change against a rule no checker covers (advisory)"),
        CheckerSpec(
            "ts_api_surface_diff",
            ClauseKind.SEMANTIC_INVARIANT,
            "TypeScript exported names and types unchanged versus a baseline commit",
        ),
    ]
}

# Named in the proposal but not built yet. Clauses bound to these are reported as unenforceable.
PLANNED: set[str] = set()
