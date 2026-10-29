"""Binding: candidate clauses + repo facts -> typed clauses with checkers. Deterministic."""

from __future__ import annotations

from ..ir import Clause, ClauseKind, ClauseStatus, OnViolation
from .ground import RepoFacts
from .schema import CandidateClause, Intent

TESTS_BEFORE = "G({action} -> (!edit S test_pass))"


def _as_glob(path: str) -> str:
    path = path.replace("\\", "/").strip()
    if path.endswith("/"):
        return path + "**"
    if "*" in path or "." in path.rsplit("/", 1)[-1]:
        return path
    return path + "/**"


class Binder:
    def __init__(self, facts: RepoFacts):
        self.facts = facts
        self.counter = 0

    def _id(self) -> str:
        self.counter += 1
        return f"C{self.counter}"

    def bind_all(self, candidates: list[CandidateClause]) -> list[Clause]:
        clauses: list[Clause] = []
        for candidate in candidates:
            clauses += self.bind(candidate)
        return clauses

    def _unenforceable(self, c: CandidateClause, kind: ClauseKind, why: str) -> Clause:
        return Clause(
            id=self._id(), kind=kind, source_span=c.source_span, status=ClauseStatus.UNENFORCEABLE,
            assumptions=[why], confidence=0.9,
        )

    def bind(self, c: CandidateClause) -> list[Clause]:
        facts = self.facts
        intent = c.intent

        if intent is Intent.PUBLIC_API_UNCHANGED:
            out = []
            for pkg in facts.python_packages:
                where = f"{pkg.root}/{pkg.name}" if pkg.root else pkg.name
                params = {"language": "python", "package": pkg.name, "baseline": "HEAD", "allow_additive": True}
                if pkg.root:
                    params["root"] = pkg.root
                out.append(Clause(
                    id=self._id(), kind=ClauseKind.SEMANTIC_INVARIANT, source_span=c.source_span,
                    scope=[f"{where}/__init__.py exports"], checker="api_surface_diff", params=params,
                    assumptions=[f"public = names exported from {where}/__init__.py"], confidence=0.85,
                ))
            if facts.ts_entries:
                out.append(Clause(
                    id=self._id(), kind=ClauseKind.SEMANTIC_INVARIANT, source_span=c.source_span,
                    scope=[f"{e} exports" for e in facts.ts_entries], checker="ts_api_surface_diff",
                    params={"language": "typescript", "entries": facts.ts_entries, "baseline": "HEAD", "allow_additive": True},
                    assumptions=[f"public = exports of {', '.join(facts.ts_entries)}"], confidence=0.85,
                ))
            return out or [self._unenforceable(c, ClauseKind.SEMANTIC_INVARIANT, "no Python package or TypeScript entry found to check")]

        if intent in (Intent.NO_DATA_LOSS, Intent.PROTECT_PATHS):
            explicit = [_as_glob(p) for p in c.paths if "/" in p or "." in p]
            paths = explicit or (facts.data_paths if intent is Intent.NO_DATA_LOSS else [])
            tables = facts.db_tables if intent is Intent.NO_DATA_LOSS else []
            if not paths and not tables:
                return [self._unenforceable(c, ClauseKind.EFFECT_PROHIBITION, "no protected files, folders or tables could be found in the repo")]
            deny = ["delete", "truncate", "overwrite"] + (["rename"] if intent is Intent.PROTECT_PATHS else [])
            params: dict = {"deny": deny, "paths": paths}
            if tables:
                params["db_tables"] = tables
            source = "named in the instruction" if explicit else "found in the repo"
            return [Clause(
                id=self._id(), kind=ClauseKind.EFFECT_PROHIBITION, source_span=c.source_span,
                scope=paths + [f"table {t}" for t in tables], checker="effect_monitor", params=params,
                on_violation=OnViolation.ROLLBACK,
                assumptions=[f"protected locations {source}: {', '.join(paths + tables)}"],
                confidence=0.9 if explicit else 0.75,
            )]

        if intent in (Intent.TESTS_BEFORE_COMMIT, Intent.TESTS_BEFORE_PUSH):
            action = "commit" if intent is Intent.TESTS_BEFORE_COMMIT else "push"
            if not facts.test_commands:
                return [Clause(
                    id=self._id(), kind=ClauseKind.TEMPORAL_OBLIGATION, source_span=c.source_span,
                    checker="trace_monitor", params={"formula": TESTS_BEFORE.format(action=action)},
                    status=ClauseStatus.PENDING_CLARIFICATION,
                    assumptions=["no test command found; add params.test_cmd"], confidence=0.5,
                )]
            cmd, source = facts.test_commands[0]
            return [Clause(
                id=self._id(), kind=ClauseKind.TEMPORAL_OBLIGATION, source_span=c.source_span,
                checker="trace_monitor",
                params={"formula": TESTS_BEFORE.format(action=action), "test_cmd": cmd},
                assumptions=[f"tests = `{cmd}` (from {source})"], confidence=0.85,
            )]

        if intent in (Intent.NO_COMMIT, Intent.NO_PUSH):
            action = "commit" if intent is Intent.NO_COMMIT else "push"
            return [Clause(
                id=self._id(), kind=ClauseKind.TEMPORAL_OBLIGATION, source_span=c.source_span,
                checker="trace_monitor", params={"formula": f"G(!{action})"}, confidence=0.95,
            )]

        if intent is Intent.NO_DESTRUCTIVE_MIGRATIONS:
            return [self._unenforceable(c, ClauseKind.SEMANTIC_INVARIANT, "schema_guard (migration checker) is not built yet")]
        if intent is Intent.DEPENDENCY_SCOPE:
            return [self._unenforceable(c, ClauseKind.SEMANTIC_INVARIANT, "dependency_diff (lockfile checker) is not built yet")]
        return [self._unenforceable(c, ClauseKind.SEMANTIC_INVARIANT, f"no checker exists for this kind of rule ({c.reading})")]
