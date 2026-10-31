"""The compile pipeline: extract -> ground -> bind -> clarify -> validate -> emit."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..ir import ClauseStatus, Covenant, Goal
from ..monitors.effects import EffectMonitor
from ..validate import Issue, validate
from .bind import Binder
from .clarify import Question, find_questions
from .extract import CompileError, Extractor
from .ground import RepoFacts, ground
from .schema import Extraction

STOPWORDS = {"a", "an", "and", "but", "the", "or", "then", "also", "please", "to", "of", "in", "on", "it", "so",
             "that", "this", "is", "be", "for", "with", "while", "as", "just", "all"}

AskFn = Callable[[Question], int]


@dataclass
class CompileResult:
    covenant: Covenant
    facts: RepoFacts
    extraction: Extraction
    asked: list[tuple[Question, int]] = field(default_factory=list)
    defaulted: list[Question] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]


def check_spans(instruction: str, extraction: Extraction) -> list[Issue]:
    """Every span must be verbatim, and together they must cover the instruction."""
    issues = []
    lowered = instruction.lower()
    spans = [("goal", extraction.goal_span)] + [("clause", c.source_span) for c in extraction.clauses]
    spans += [("note", s) for s in extraction.no_constraint_spans]
    covered = [False] * len(instruction)
    for where, span in spans:
        start = lowered.find(span.lower().strip()) if span.strip() else -1
        if start < 0:
            issues.append(Issue("extract", "error", f"{where} span is not in the instruction: {span!r}"))
            continue
        for i in range(start, start + len(span.strip())):
            covered[i] = True
    leftover = "".join(ch if not covered[i] else " " for i, ch in enumerate(instruction))
    words = [w for w in re.findall(r"[A-Za-z0-9_'/.-]+", leftover) if w.lower().strip(".'") not in STOPWORDS]
    words = [w for w in words if re.search(r"[A-Za-z0-9]", w)]
    if words:
        issues.append(Issue("extract", "warning", f"words not accounted for by any clause: {' '.join(words)!r}"))
    return issues


def contradictions(covenant: Covenant, extraction: Extraction, repo: Path) -> list[Issue]:
    issues = []
    goal = extraction.goal_span.lower()
    destructive_goal = re.search(r"\b(delete|remove|clean ?up|wipe|drop|purge|clear)\b", goal)
    for clause in covenant.enforced():
        if clause.checker != "effect_monitor":
            continue
        monitor = EffectMonitor(clause, repo)
        if destructive_goal:
            issues.append(Issue(clause.id, "warning",
                f"the goal ({extraction.goal_span!r}) may need effects that {clause.id} forbids; check its scope"))
        for path in covenant.goal.scope:
            if monitor.protects(path):
                issues.append(Issue(clause.id, "error",
                    f"the goal works on {path}, which {clause.id} protects: the task would be impossible"))
    return issues


def compile_instruction(
    instruction: str,
    repo: Path,
    extractor: Extractor,
    ask: AskFn | None = None,
    answers: dict[str, int] | None = None,
    max_questions: int = 3,
    review: bool = False,
    packs: list[str] | None = None,
) -> CompileResult:
    from .. import packs as rule_packs
    from .extract import HeuristicExtractor

    instruction = instruction.strip()
    chosen = [rule_packs.get(name) for name in packs or []]
    if not instruction and chosen:
        instruction = f"Work on a {chosen[0].name} task"
        extractor = HeuristicExtractor()  # nothing for Claude to read
    if not instruction:
        raise CompileError("the instruction is empty")
    facts = ground(repo)
    extraction = extractor(instruction, facts)
    issues = check_spans(instruction, extraction)

    binder = Binder(facts, review=review)
    clauses = binder.bind_all(extraction.clauses)
    for pack in chosen:  # packs always compile offline, so a pack means the same rules everywhere
        pack_extraction = HeuristicExtractor()(pack.text, facts)
        for clause in binder.bind_all(pack_extraction.clauses):
            if any(c.checker == clause.checker and c.params == clause.params for c in clauses):
                continue  # the instruction already says this
            clauses.append(clause.model_copy(update={"assumptions": clause.assumptions + [f"from rule pack {pack.name}"]}))
    for n, clause in enumerate(clauses, 1):  # ids stay C1, C2, ... after duplicates are dropped
        clause.id = f"C{n}"
    questions = find_questions(clauses, facts)
    by_id = {c.id: c for c in clauses}
    result_asked, result_defaulted = [], []
    for n, q in enumerate(questions):
        clause = by_id[q.clause_id]
        if answers and q.id in answers:
            choice = answers[q.id]
            q.resolve(clause, choice, "answered")
            result_asked.append((q, choice))
        elif ask is not None and n < max_questions:
            choice = ask(q)
            q.resolve(clause, choice, "answered")
            result_asked.append((q, choice))
        else:
            q.resolve(clause, q.default, "not asked; conservative default")
            result_defaulted.append(q)

    goal_paths = [p for p in re.findall(r"[\w.-]+/[\w./*-]*|\b[\w-]+\.(?:toml|lock|json|ya?ml|txt|cfg)\b", extraction.goal_span)]
    if chosen:
        instruction += "".join(f"\n[rule pack {p.name}] {p.text}" for p in chosen)
    covenant = Covenant(
        instruction=instruction,
        goal=Goal(action=extraction.goal_action, scope=goal_paths),
        clauses=clauses,
    )
    issues += contradictions(covenant, extraction, repo)
    issues += validate(covenant, repo)
    return CompileResult(covenant, facts, extraction, result_asked, result_defaulted, issues)


def render_summary(result: CompileResult) -> str:
    cov = result.covenant
    lines = [f"Goal: {cov.goal.action}" + (f" (scope: {', '.join(cov.goal.scope)})" if cov.goal.scope else "")]
    for c in cov.clauses:
        status = "" if c.status is ClauseStatus.ENFORCED else f"  [{c.status.value}]"
        lines.append(f"{c.id}  {c.source_span!r} -> {c.checker or 'no checker'}{status}")
        for scope in c.scope:
            lines.append(f"      applies to {scope}")
        for a in c.assumptions:
            lines.append(f"      assumes {a}")
    for issue in result.issues:
        lines.append(str(issue))
    return "\n".join(lines)
