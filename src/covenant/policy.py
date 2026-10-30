"""Team-wide policies: rules shared across repos and merged into each repo's Covenant.

A policy file holds clauses like covenant.yaml does:

    policy:
      name: platform-team
      extends: [base.yaml]          # optional, relative to this file
      clauses:
        - id: T1
          kind: EffectProhibition
          source_span: "never touch production config"
          checker: effect_monitor
          params: {paths: ["deploy/prod/**"], deny: [delete, truncate, overwrite]}

A repo opts in with `extends: [path/to/policy.yaml]` in covenant.yaml. A machine can impose
policies on every repo with COVENANT_POLICY (paths separated by the OS path separator).

Policy clauses come first, carry an `origin`, and can't be redefined by the repo: a clashing
id is an error, not an override. Policy files count as Covenant's own files, so editing one
mid-task is reported as tampering, and in CI the base commit's version is used.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .ir import Clause, Covenant

POLICY_ENV = "COVENANT_POLICY"
Reader = Callable[[str], "str | None"]


class PolicyError(ValueError):
    pass


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    extends: list[str] = Field(default_factory=list)
    clauses: list[Clause] = Field(default_factory=list)


def _unwrap(text: str, what: str) -> dict:
    data = yaml.safe_load(text) or {}
    if isinstance(data, dict) and len(data) == 1 and next(iter(data)) in ("policy", "covenant"):
        data = next(iter(data.values()))
    if not isinstance(data, dict):
        raise PolicyError(f"{what} is not a policy file")
    return data


def machine_policies() -> list[str]:
    return [p for p in os.environ.get(POLICY_ENV, "").split(os.pathsep) if p.strip()]


def _join(base: str, ref: str) -> str:
    ref = os.path.expanduser(ref)
    if os.path.isabs(ref):
        return os.path.normpath(ref)
    if os.path.isabs(base):
        return os.path.normpath(os.path.join(base, ref))
    return posixpath.normpath(posixpath.join(base, ref.replace("\\", "/")))


def resolve(covenant: Covenant, read: Reader, base: str, include_machine: bool = True) -> tuple[Covenant, list[str]]:
    """Merge the clauses of every policy the Covenant extends. Returns (merged, policy paths used).

    `base` is the folder covenant.yaml lives in; `read` maps a path to its text (or None).
    The merged Covenant has `extends` cleared, so resolving it again is a no-op.
    """
    merged: list[Clause] = []
    used: list[str] = []

    def visit(ref: str, base_dir: str, chain: list[str]) -> None:
        path = _join(base_dir, ref)
        if path in chain:
            raise PolicyError(f"policies extend each other in a loop: {' -> '.join(chain + [path])}")
        if path in used:
            return
        text = read(path)
        if text is None:
            raise PolicyError(f"policy file not found: {path}")
        try:
            policy = Policy.model_validate(_unwrap(text, path))
        except ValueError as exc:
            raise PolicyError(f"invalid policy {path}: {exc}") from exc
        parent = os.path.dirname(path) if os.path.isabs(path) else posixpath.dirname(path)
        for sub in policy.extends:
            visit(sub, parent, chain + [path])
        used.append(path)
        merged.extend(c.model_copy(update={"origin": f"{policy.name} ({path})"}) for c in policy.clauses)

    refs = list(covenant.extends) + (machine_policies() if include_machine else [])
    for ref in refs:
        visit(ref, base, [])

    owners: dict[str, str] = {}
    for clause in merged:
        if clause.id in owners and owners[clause.id] != clause.origin:
            raise PolicyError(f"clause id {clause.id} is defined by two policies: {owners[clause.id]} and {clause.origin}")
        owners[clause.id] = clause.origin or ""
    for clause in covenant.clauses:
        if clause.id in owners:
            raise PolicyError(
                f"covenant.yaml defines {clause.id}, which policy {owners[clause.id]} already defines; "
                "policy rules can't be redefined by a repo (rename the repo's clause)"
            )
    seen: set[str] = set()
    deduped = [c for c in merged if not (c.id in seen or seen.add(c.id))]
    return covenant.model_copy(update={"clauses": deduped + list(covenant.clauses), "extends": []}), used


def disk_reader(path: str) -> str | None:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.is_file() else None


def load(covenant_path: Path) -> tuple[Covenant, list[Path]]:
    """Load covenant.yaml from disk with every policy merged in."""
    covenant = Covenant.load(covenant_path)
    merged, used = resolve(covenant, disk_reader, str(covenant_path.resolve().parent))
    return merged, [Path(p) for p in used]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<missing>"
