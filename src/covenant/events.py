"""Typed events the harness emits. The runtime judges these, never raw tool calls."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ts: datetime = Field(default_factory=_now)


class Edit(_Event):
    type: Literal["edit"] = "edit"
    path: str


class Run(_Event):
    type: Literal["run"] = "run"
    cmd: str
    exit_code: int


class TestRun(_Event):
    __test__ = False  # keep pytest from collecting this class

    type: Literal["test"] = "test"
    cmd: str
    exit_code: int

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


class Commit(_Event):
    type: Literal["commit"] = "commit"
    sha: str | None = None


class Push(_Event):
    type: Literal["push"] = "push"
    remote: str | None = None


class Effect(_Event):
    """A normalized side effect: (operation, target), e.g. ("delete", "data/users.db")."""

    type: Literal["effect"] = "effect"
    op: str  # delete, truncate, overwrite, rename, drop, write, create
    target: str  # a path for fs effects, a table name for db effects
    channel: Literal["fs", "db"] = "fs"


Event = Annotated[Union[Edit, Run, TestRun, Commit, Push, Effect], Field(discriminator="type")]
EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)

# Actions the runtime can still stop because they have not happened yet.
GATED_EVENTS = {"commit", "push"}
