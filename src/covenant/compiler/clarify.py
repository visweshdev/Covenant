"""Clarification: find ambiguities that change what the agent may do, and ask about the biggest.

Each question names a concrete thing in the repo. Questions are ranked by impact
(roughly, how many files or names flip between allowed and blocked); the top few are
asked, the rest take the conservative default and are recorded as assumptions.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..ir import Clause
from ..monitors.effects import base_dir
from .ground import RepoFacts

CACHE_NAMES = {"cache", ".cache", "tmp", "temp", "logs", "log", "__cache__"}


@dataclass
class Option:
    label: str
    apply: Callable[[Clause], None] = field(repr=False)


@dataclass
class Question:
    id: str
    clause_id: str
    text: str
    options: list[Option]
    default: int = 0  # the conservative reading
    impact: int = 0

    def resolve(self, clause: Clause, choice: int, how: str) -> None:
        option = self.options[choice]
        option.apply(clause)
        clause.assumptions.append(f"{self.id}: {self.text} -> {option.label} ({how})")


def _size(path: Path) -> tuple[int, int]:
    files, total = 0, 0
    for p in path.rglob("*"):
        if p.is_file():
            files += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return files, total


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _display(path: Path, repo: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        try:
            return "~/" + path.relative_to(Path.home()).as_posix()
        except ValueError:
            return path.as_posix()


def cache_questions(clause: Clause, repo: Path) -> list[Question]:
    out = []
    for pattern in clause.params.get("paths", []):
        base = base_dir(pattern, repo)
        if not base.is_dir():
            continue
        for sub in sorted(p for p in base.rglob("*") if p.is_dir() and p.name.lower() in CACHE_NAMES):
            if len(sub.relative_to(base).parts) > 2:
                continue
            files, size = _size(sub)
            if not files:
                continue
            shown = _display(sub, repo)

            def exclude(c: Clause, shown: str = shown) -> None:
                c.params.setdefault("exclude", []).append(f"{shown}/**")

            out.append(Question(
                id="", clause_id=clause.id,
                text=f"`{shown}/` holds {files} files ({_human(size)}). Is it user data the agent must not delete?",
                options=[Option("Yes, protect it", lambda c: None), Option("No, the agent may clear it", exclude)],
                impact=files,
            ))
    return out


def private_module_questions(clause: Clause, repo: Path) -> list[Question]:
    package = clause.params.get("package")
    if not package:
        return []
    pkg_dir = repo / clause.params.get("root", "") / package.replace(".", "/")
    docs = [p for p in [*repo.glob("README*"), *(repo / "docs").rglob("*")] if p.is_file() and p.suffix in (".md", ".rst", ".txt", "")]
    out = []
    for module in sorted(pkg_dir.glob("_*.py")):
        if module.name == "__init__.py":
            continue
        dotted = f"{package}.{module.stem}"
        hits = [d for d in docs if dotted in d.read_text(encoding="utf-8", errors="ignore")]
        if not hits:
            continue
        names = len(re.findall(r"^(?:def|class|async def)\s+[A-Za-z]", module.read_text(encoding="utf-8", errors="ignore"), re.M))

        def include(c: Clause, dotted: str = dotted) -> None:
            c.params.setdefault("extra_modules", []).append(dotted)

        doc = _display(hits[0], repo)
        out.append(Question(
            id="", clause_id=clause.id,
            text=f"`{_display(module, repo)}` is not exported, but {doc} references `{dotted}`. Treat it as public API?",
            options=[Option("Yes, treat it as public", include), Option("No, it's internal", lambda c: None)],
            impact=max(names, 1) * 2,
        ))
    return out


def test_command_questions(clause: Clause, facts: RepoFacts) -> list[Question]:
    if len(facts.test_commands) < 2 or "test_cmd" not in clause.params:
        return []

    def setter(cmd: str) -> Callable[[Clause], None]:
        def apply(c: Clause) -> None:
            c.params["test_cmd"] = cmd
        return apply

    options = [Option(f"`{cmd}` (from {src})", setter(cmd)) for cmd, src in facts.test_commands[:4]]
    return [Question(
        id="", clause_id=clause.id,
        text="The repo has several test commands. Which one must pass before the agent commits?",
        options=options, impact=5,
    )]


def find_questions(clauses: list[Clause], facts: RepoFacts) -> list[Question]:
    repo = facts.root
    questions: list[Question] = []
    for clause in clauses:
        if clause.checker == "effect_monitor":
            questions += cache_questions(clause, repo)
        elif clause.checker == "api_surface_diff":
            questions += private_module_questions(clause, repo)
        elif clause.checker == "trace_monitor":
            questions += test_command_questions(clause, facts)
    questions.sort(key=lambda q: -q.impact)
    for n, q in enumerate(questions, 1):
        q.id = f"Q{n}"
    return questions
