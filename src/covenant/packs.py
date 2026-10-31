"""Rule packs: ready-made rule sets for common kinds of task.

A pack is plain English, compiled like any instruction but always with the offline
extractor, so the same pack gives the same rules everywhere. `covenant compile --pack hotfix
"fix the login crash"` adds the pack's rules to the ones in the instruction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pack:
    name: str
    summary: str
    rules: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join(r if r.endswith(".") else r + "." for r in self.rules)


PACKS: dict[str, Pack] = {p.name: p for p in [
    Pack("safe-refactor", "restructure code without changing what it does", (
        "Don't change public APIs",
        "Don't delete or skip tests",
        "Don't add new dependencies",
        "Run tests before committing",
        "Never force push or rewrite git history",
    )),
    Pack("dependency-bump", "update dependencies without breaking anything", (
        "Don't do major version upgrades",
        "Don't change public APIs",
        "Don't delete or skip tests",
        "Run tests before committing",
        "Never force push",
    )),
    Pack("docs-only", "change documentation and nothing else", (
        "Only change files in docs/ and README.md and CHANGELOG.md",
        "Don't add new dependencies",
        "Don't commit large files over 2 MB",
        "Never force push",
    )),
    Pack("hotfix", "a small, urgent, well-tested fix", (
        "Keep the change under 100 lines",
        "Don't change public APIs",
        "Don't delete or skip tests",
        "No leftover debug prints",
        "Don't silence type errors or lint warnings",
        "Run tests before committing",
        "Push at most once",
        "Never force push or rewrite git history",
    )),
    Pack("data-safe", "work near user data and databases without losing any", (
        "Don't delete user data",
        "No destructive migrations",
        "Never run destructive SQL",
        "Never deploy",
    )),
    Pack("secure", "keep secrets and the machine safe", (
        "Don't commit secrets or API keys",
        "Don't read .env files or credentials",
        "Never pipe downloads into a shell",
        "Never use sudo",
        "Don't change system settings",
    )),
    Pack("lockdown", "the strictest set: no installs, no network, no history rewrites", (
        "Don't change public APIs",
        "Don't delete or skip tests",
        "Don't add new dependencies",
        "Don't commit secrets or API keys",
        "Never install packages",
        "No network access",
        "Never force push or rewrite git history",
        "Never deploy or publish",
        "Run tests before committing",
    )),
]}


def get(name: str) -> Pack:
    try:
        return PACKS[name]
    except KeyError:
        raise ValueError(f"unknown rule pack {name!r}; choose from {', '.join(PACKS)}") from None


def listing() -> str:
    width = max(len(n) for n in PACKS)
    lines = []
    for p in PACKS.values():
        lines.append(f"{p.name:<{width}}  {p.summary}")
        lines += [f"{'':<{width}}    - {r}" for r in p.rules]
    return "\n".join(lines)
