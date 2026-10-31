"""path_owner: some folders belong to one team's agent; other agents may not change them.

Clause params:
    owners: {"billing/**": ["payments"], "infra/**": ["platform", "sre"]}
    baseline: the commit the task started from

An agent says who it is with the COVENANT_AGENT environment variable, set where the agent is
launched (`COVENANT_AGENT=payments claude`). Edits to an owned path are refused before they
happen, and a commit that changes one is blocked, unless the agent is one of its owners. An
agent with no name owns nothing. In CI, set COVENANT_AGENT in the job (from a label, say).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..feedback import ViolationReport
from ..ir import Clause
from .diff_guard import changes, compile_glob

ENV = "COVENANT_AGENT"


def current_agent(override: str | None = None) -> str:
    return (override if override is not None else os.environ.get(ENV, "")).strip().lower()


def owners_of(clause: Clause, rel_path: str) -> list[str] | None:
    """The owners of a path, or None if this rule doesn't cover it."""
    for pattern, names in clause.params.get("owners", {}).items():
        if compile_glob(pattern).fullmatch(rel_path):
            return [str(n).lower() for n in (names if isinstance(names, list) else [names])]
    return None


def check_path(clause: Clause, rel_path: str, agent: str | None = None) -> list[ViolationReport]:
    owners = owners_of(clause, rel_path)
    who = current_agent(agent)
    if owners is None or who in owners:
        return []
    named = who or "an agent with no COVENANT_AGENT name"
    return [ViolationReport.for_clause(
        clause, {"target": rel_path, "owners": owners, "agent": who or None},
        hints=[f"{rel_path} belongs to {', '.join(owners)}; {named} may not change it. Ask the human or the owning team"],
    )]


def verify(clause: Clause, repo: Path, baseline: str | None = None) -> list[ViolationReport]:
    base = baseline or clause.params.get("baseline", "HEAD")
    reports = []
    for change in changes(repo, base):
        for path in {change.path, change.old_path} - {None}:
            reports += check_path(clause, path)
    return reports
