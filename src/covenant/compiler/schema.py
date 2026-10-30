"""What the extraction step must return. Kept small so the model's output is easy to check."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Intent(str, Enum):
    PUBLIC_API_UNCHANGED = "public_api_unchanged"
    NO_DATA_LOSS = "no_data_loss"
    PROTECT_PATHS = "protect_paths"
    TESTS_BEFORE_COMMIT = "tests_before_commit"
    TESTS_BEFORE_PUSH = "tests_before_push"
    NO_COMMIT = "no_commit"
    NO_PUSH = "no_push"
    ACTION_LIMIT = "action_limit"
    TESTS_WITHIN_STEPS = "tests_within_steps"
    NO_DESTRUCTIVE_MIGRATIONS = "no_destructive_migrations"
    DEPENDENCY_SCOPE = "dependency_scope"
    OTHER = "other"


INTENT_HELP = {
    Intent.PUBLIC_API_UNCHANGED: "the package's exported API must not change",
    Intent.NO_DATA_LOSS: "user data (files, databases) must not be deleted, truncated or overwritten",
    Intent.PROTECT_PATHS: "specific files or folders named in the instruction must not be modified",
    Intent.TESTS_BEFORE_COMMIT: "a passing test run must come before each commit",
    Intent.TESTS_BEFORE_PUSH: "a passing test run must come before each push",
    Intent.NO_COMMIT: "the agent must not commit at all",
    Intent.NO_PUSH: "the agent must not push at all",
    Intent.ACTION_LIMIT: "the agent may commit or push at most a stated number of times",
    Intent.TESTS_WITHIN_STEPS: "after each edit, the tests must run within a stated number of agent steps",
    Intent.NO_DESTRUCTIVE_MIGRATIONS: "no database migration may drop tables or columns or narrow types",
    Intent.DEPENDENCY_SCOPE: "only certain dependencies or version ranges may change",
    Intent.OTHER: "any other constraint (style, readability, process) with no automatic check",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateClause(_Strict):
    source_span: str = Field(description="The exact words of the instruction this constraint comes from, copied verbatim")
    intent: Intent
    paths: list[str] = Field(description="Paths, folders or names the instruction mentions for this constraint; empty if none")
    reading: str = Field(description="One sentence: what this constraint forbids or requires")


class Extraction(_Strict):
    goal_action: str = Field(description="Short snake_case name of the task, e.g. update_dependencies")
    goal_span: str = Field(description="The exact words of the instruction that state the task")
    clauses: list[CandidateClause]
    no_constraint_spans: list[str] = Field(
        description="Exact words of the instruction that are neither the goal nor a constraint (filler, context)"
    )
