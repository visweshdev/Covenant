"""The benchmark task format."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Step(_M):
    """One action of the scripted agent. Exactly one of the action fields is set."""

    replace: str | None = None  # path; with old/new
    old: str | None = None
    new: str | None = None
    write: str | None = None  # path; with content
    content: str | None = None
    run: str | None = None  # shell command
    commit: str | None = None  # commit message (runs `git add -A && git commit -m ...`)


class Check(_M):
    """An oracle or acceptance check, evaluated on the final repo without Covenant's code."""

    type: Literal["file_intact", "python", "no_sql", "manifest_line", "tests_pass_at_each_commit", "file_contains", "committed"]
    path: str | None = None
    code: str | None = None  # python: runs in the repo; must exit 0
    pattern: str | None = None  # no_sql / file_contains: regex
    glob: str | None = None
    text: str | None = None  # manifest_line: the exact line that must still be there
    cmd: str | None = None  # tests_pass_at_each_commit
    description: str = ""


class GroundTruthClause(_M):
    checker: str
    params: dict[str, Any] = Field(default_factory=dict)  # each listed value must match (lists: subset)


class Task(_M):
    id: str
    instruction: str
    task_type: str = "other"
    repo: str = "repo"  # folder next to task.yaml
    ground_truth: list[GroundTruthClause]
    covenant: dict[str, Any] | None = None  # a full ground-truth covenant.yaml body (for --mode groundtruth)
    traps: list[str] = Field(default_factory=list)
    oracle: list[Check]
    acceptance: list[Check]
    underspecified: bool = False
    should_ask: str | None = None  # regex the clarification questions should match
    # The simulated developer's answers: question regex -> chosen option (0-based).
    answers: dict[str, int] = Field(default_factory=dict)
    scripted: dict[str, list[Step]] = Field(default_factory=dict)  # "naive" and "careful" plans
    root: Path = Field(default=Path("."), exclude=True)

    @classmethod
    def load(cls, path: Path) -> Task:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        task = cls.model_validate(data)
        task.root = path.parent
        return task


def load_tasks(folder: Path) -> list[Task]:
    return [Task.load(p) for p in sorted(folder.glob("*/task.yaml"))]
