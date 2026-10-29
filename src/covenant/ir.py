"""The Covenant intermediate representation.

A Covenant is a goal plus a list of typed clauses. Each clause records the words
it came from, what it applies to, and exactly one checker - or it is explicitly
marked unenforceable instead of being dropped.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClauseKind(str, Enum):
    EFFECT_PROHIBITION = "EffectProhibition"
    SEMANTIC_INVARIANT = "SemanticInvariant"
    TEMPORAL_OBLIGATION = "TemporalObligation"


class OnViolation(str, Enum):
    BLOCK = "block"
    ROLLBACK = "rollback"
    ASK_HUMAN = "ask_human"


class ClauseStatus(str, Enum):
    ENFORCED = "enforced"
    UNENFORCEABLE = "unenforceable"
    PENDING_CLARIFICATION = "pending_clarification"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Clause(_Model):
    id: str
    kind: ClauseKind
    source_span: str = Field(description="The exact words of the instruction this clause came from.")
    scope: list[str] = Field(default_factory=list, description="What it applies to, grounded in the repo.")
    checker: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    on_violation: OnViolation = OnViolation.BLOCK
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    status: ClauseStatus = ClauseStatus.ENFORCED

    @model_validator(mode="after")
    def _enforced_needs_checker(self) -> Clause:
        if self.status is ClauseStatus.ENFORCED and not self.checker:
            raise ValueError(f"clause {self.id}: an enforced clause must name a checker")
        return self


class Goal(_Model):
    action: str
    scope: list[str] = Field(default_factory=list)


class Covenant(_Model):
    instruction: str
    goal: Goal
    clauses: list[Clause]

    @model_validator(mode="after")
    def _unique_ids(self) -> Covenant:
        seen: set[str] = set()
        for clause in self.clauses:
            if clause.id in seen:
                raise ValueError(f"duplicate clause id {clause.id!r}")
            seen.add(clause.id)
        return self

    def enforced(self, kind: ClauseKind | None = None) -> list[Clause]:
        return [
            c
            for c in self.clauses
            if c.status is ClauseStatus.ENFORCED and (kind is None or c.kind is kind)
        ]

    def clause(self, clause_id: str) -> Clause:
        for c in self.clauses:
            if c.id == clause_id:
                return c
        raise KeyError(clause_id)

    @classmethod
    def load(cls, path: str | Path) -> Covenant:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        # Files may wrap everything in a top-level `covenant:` key.
        if isinstance(data, dict) and list(data) == ["covenant"]:
            data = data["covenant"]
        return cls.model_validate(data)

    def dump(self, path: str | Path) -> None:
        data = {"covenant": self.model_dump(mode="json", exclude_defaults=True)}
        Path(path).write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
