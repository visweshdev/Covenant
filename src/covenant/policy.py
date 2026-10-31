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
A policy can also live at an https URL (a raw file in an org-wide repo, say), optionally
pinned: `extends: ["https://example.com/policy.yaml#sha256=<hex>"]`. See `fetch`.

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
from urllib.parse import urljoin

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


MAX_POLICY_BYTES = 1_000_000


def is_url(ref: str) -> bool:
    return ref.startswith(("https://", "http://"))


def fetch(url: str, opener: Callable[[str], bytes] | None = None) -> str:
    """Download a policy from a URL into ~/.covenant/policies/ and return the local copy's path.

    Pin a policy to exact contents with `#sha256=<hex>` at the end of the URL; a mismatch is an
    error. When the network is down the last good copy is used, unless it no longer matches
    the pin. Plain http is refused except for localhost.
    """
    from urllib.parse import urlparse

    from .paths import covenant_home

    address, _, fragment = url.partition("#")
    pin = fragment.removeprefix("sha256=").lower() if fragment.startswith("sha256=") else None
    host = urlparse(address).hostname or ""
    if address.startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
        raise PolicyError(f"policy URLs must use https: {address}")
    cache = covenant_home() / "policies" / (hashlib.sha256(address.encode()).hexdigest()[:20] + ".yaml")

    def download(u: str) -> bytes:
        import urllib.request

        with urllib.request.urlopen(u, timeout=15) as response:  # noqa: S310 (https checked above)
            return response.read(MAX_POLICY_BYTES + 1)

    try:
        data = (opener or download)(address)
        if len(data) > MAX_POLICY_BYTES:
            raise PolicyError(f"policy at {address} is larger than 1 MB")
    except PolicyError:
        raise
    except Exception as exc:
        if not cache.is_file():
            raise PolicyError(f"can't download policy {address}: {exc}") from exc
        data = cache.read_bytes()  # offline: the last good copy
    if pin and hashlib.sha256(data).hexdigest() != pin:
        raise PolicyError(f"policy at {address} doesn't match its pinned sha256; someone changed it")
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.is_file() or cache.read_bytes() != data:
        cache.write_bytes(data)
    return str(cache)


def machine_policies() -> list[str]:
    import re

    # Split on the path separator, but not the ':' in "https://".
    parts = re.split(re.escape(os.pathsep) + ("(?!//)" if os.pathsep == ":" else ""), os.environ.get(POLICY_ENV, ""))
    return [p for p in parts if p.strip()]


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
        if is_url(ref) or is_url(base_dir):
            key = ref if is_url(ref) else urljoin(base_dir.rstrip("/") + "/", ref)
            path = fetch(key)  # a local cached copy; that file is what gets hashed and guarded
            parent = key.split("#", 1)[0].rsplit("/", 1)[0]
            text = disk_reader(path)
        else:
            path = key = _join(base_dir, ref)
            parent = os.path.dirname(path) if os.path.isabs(path) else posixpath.dirname(path)
            text = None
        if key in chain:
            raise PolicyError(f"policies extend each other in a loop: {' -> '.join(chain + [key])}")
        if path in used:
            return
        if text is None:
            text = read(path)
        if text is None:
            raise PolicyError(f"policy file not found: {path}")
        try:
            policy = Policy.model_validate(_unwrap(text, key))
        except ValueError as exc:
            raise PolicyError(f"invalid policy {key}: {exc}") from exc
        for sub in policy.extends:
            visit(sub, parent, chain + [key])
        used.append(path)
        merged.extend(c.model_copy(update={"origin": f"{policy.name} ({key})"}) for c in policy.clauses)

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
