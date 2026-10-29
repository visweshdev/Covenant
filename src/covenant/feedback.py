"""Structured violation reports: the clause, the evidence, and how to repair it."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .ir import Clause, ClauseKind, OnViolation


class ViolationReport(BaseModel):
    violation: str  # clause id
    clause: str  # the instruction words the clause came from
    kind: ClauseKind
    checker: str
    on_violation: OnViolation
    blocked_action: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    repair_hints: list[str] = Field(default_factory=list)
    options: list[str] = Field(default_factory=lambda: ["revise", "ask_human"])
    escalated: bool = False

    @classmethod
    def for_clause(
        cls,
        clause: Clause,
        evidence: dict[str, Any],
        blocked_action: str | None = None,
        hints: list[str] | None = None,
    ) -> ViolationReport:
        return cls(
            violation=clause.id,
            clause=clause.source_span,
            kind=clause.kind,
            checker=clause.checker or "",
            on_violation=clause.on_violation,
            blocked_action=blocked_action,
            evidence=evidence,
            repair_hints=hints if hints is not None else default_hints(clause, evidence),
        )


def default_hints(clause: Clause, evidence: dict[str, Any]) -> list[str]:
    checker = clause.checker
    if checker == "api_surface_diff":
        if evidence.get("after") is None:
            return [
                "Restore the removed name, or keep a deprecated alias that points to its replacement",
                "Pin the dependency below the version that forced this change",
            ]
        return [
            "Keep the old signature and add new parameters as optional keyword arguments",
            "Pin the dependency below the version that forced this change",
        ]
    if checker == "trace_monitor":
        test_cmd = clause.params.get("test_cmd")
        if test_cmd:
            return [f"Run `{test_cmd}` after your last edit, make sure it passes, then retry"]
        return ["Satisfy the ordering rule in the evidence, then retry"]
    if checker == "effect_monitor":
        return [
            f"Do not {evidence.get('op', 'modify')} {evidence.get('target', 'this target')}; it is protected",
            "If the change is needed, stop and ask the human",
        ]
    return []
