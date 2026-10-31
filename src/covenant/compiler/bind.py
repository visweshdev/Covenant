"""Binding: candidate clauses + repo facts -> typed clauses with checkers. Deterministic."""

from __future__ import annotations

import re

from ..ir import Clause, ClauseKind, ClauseStatus, OnViolation
from .ground import DB_SUFFIXES, RepoFacts
from .schema import CandidateClause, Intent

TESTS_BEFORE = "G({action} -> (!edit S test_pass))"
TESTS_WITHIN = "G(!(!test S[>={n}] edit))"

_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}


def _count(span: str, unit: str = "") -> int | None:
    """The number in phrases like "at most twice", "no more than 3 times", "within five steps"."""
    if not unit and re.search(r"\bonce\b", span, re.I):
        return 1
    if not unit and re.search(r"\btwice\b", span, re.I):
        return 2
    m = re.search(rf"\b(\d+|{'|'.join(_NUMBER_WORDS)})\s+{unit or 'times?'}", span, re.I)
    if not m:
        return None
    word = m.group(1).lower()
    return int(word) if word.isdigit() else _NUMBER_WORDS[word]


def _as_glob(path: str) -> str:
    path = path.replace("\\", "/").strip()
    if path.endswith("/"):
        return path + "**"
    if "*" in path or "." in path.rsplit("/", 1)[-1]:
        return path
    return path + "/**"


class Binder:
    def __init__(self, facts: RepoFacts, review: bool = False):
        self.facts = facts
        self.review = review  # bind rules no checker covers to an advisory Claude review
        self.counter = 0

    def _id(self) -> str:
        self.counter += 1
        return f"C{self.counter}"

    def bind_all(self, candidates: list[CandidateClause]) -> list[Clause]:
        clauses: list[Clause] = []
        for candidate in candidates:
            clauses += self.bind(candidate)
        return clauses

    def _unenforceable(self, c: CandidateClause, kind: ClauseKind, why: str) -> Clause:
        return Clause(
            id=self._id(), kind=kind, source_span=c.source_span, status=ClauseStatus.UNENFORCEABLE,
            assumptions=[why], confidence=0.9,
        )

    def _diff_rule(self, c: CandidateClause) -> Clause:
        """Rules checked against the change since the task started (see verifiers/diff_guard)."""
        intent, span = c.intent, c.source_span
        params: dict = {"baseline": "HEAD"}
        status, confidence, assumptions = ClauseStatus.ENFORCED, 0.85, []
        checker = {
            Intent.NO_NEW_DEPENDENCIES: "dependency_diff", Intent.ONLY_PATHS: "scope_guard", Intent.NO_SECRETS: "secret_scan",
            Intent.KEEP_TESTS: "test_guard", Intent.SMALL_CHANGE: "diff_budget", Intent.NO_DEBUG_CODE: "no_debug_code",
            Intent.NO_SUPPRESSIONS: "no_suppressions", Intent.NO_LARGE_FILES: "large_files",
        }[intent]
        if intent is Intent.NO_NEW_DEPENDENCIES:
            params.update(allow_new=False, allow_removed=True, deny_major=False, lockfiles=False)
            assumptions.append("upgrading existing packages is fine; adding a package the project doesn't declare is not")
        elif intent is Intent.ONLY_PATHS:
            allow = [_as_glob(p) for p in c.paths if "/" in p or "." in p]
            params["allow"] = allow or ["src/**"]
            if not allow:
                status, confidence = ClauseStatus.PENDING_CLARIFICATION, 0.4
                assumptions.append("the instruction doesn't name the files; set params.allow")
            else:
                assumptions.append(f"only {', '.join(allow)} may change (tests elsewhere too would need listing)")
        elif intent is Intent.SMALL_CHANGE:
            files, lines = _numbers(span, r"files?\b"), _numbers(span, r"lines?\b")
            if files:
                params["max_files"] = files
            if lines:
                params["max_lines"] = lines
            if not (files or lines):
                params.update(max_files=10, max_lines=300)
                status, confidence = ClauseStatus.PENDING_CLARIFICATION, 0.4
                assumptions.append("no size given; 10 files / 300 lines is a placeholder to confirm")
        elif intent is Intent.NO_DEBUG_CODE:
            if re.search(r"\bprint", span, re.I):
                params["include_print"] = True
        elif intent is Intent.NO_LARGE_FILES:
            mb, kb = _numbers(span, r"mb\b"), _numbers(span, r"kb\b")
            params["max_kb"] = mb * 1024 if mb else kb or 1024
        elif intent is Intent.KEEP_TESTS:
            assumptions.append("covers deleted test files, removed test cases, new skips and .only")
        kind = ClauseKind.SEMANTIC_INVARIANT
        return Clause(
            id=self._id(), kind=kind, source_span=span, scope=["the change since the task started"], checker=checker,
            params=params, status=status, assumptions=assumptions, confidence=confidence,
        )

    def bind(self, c: CandidateClause) -> list[Clause]:
        facts = self.facts
        intent = c.intent

        if intent is Intent.PUBLIC_API_UNCHANGED:
            out = []
            for pkg in facts.python_packages:
                where = f"{pkg.root}/{pkg.name}" if pkg.root else pkg.name
                params = {"language": "python", "package": pkg.name, "baseline": "HEAD", "allow_additive": True}
                if pkg.root:
                    params["root"] = pkg.root
                out.append(Clause(
                    id=self._id(), kind=ClauseKind.SEMANTIC_INVARIANT, source_span=c.source_span,
                    scope=[f"{where}/__init__.py exports"], checker="api_surface_diff", params=params,
                    assumptions=[f"public = names exported from {where}/__init__.py"], confidence=0.85,
                ))
            if facts.ts_entries:
                out.append(Clause(
                    id=self._id(), kind=ClauseKind.SEMANTIC_INVARIANT, source_span=c.source_span,
                    scope=[f"{e} exports" for e in facts.ts_entries], checker="ts_api_surface_diff",
                    params={"language": "typescript", "entries": facts.ts_entries, "baseline": "HEAD", "allow_additive": True},
                    assumptions=[f"public = exports of {', '.join(facts.ts_entries)}"], confidence=0.85,
                ))
            if facts.source_language:
                lang = facts.source_language
                out.append(Clause(
                    id=self._id(), kind=ClauseKind.SEMANTIC_INVARIANT, source_span=c.source_span,
                    scope=[f"public {lang.capitalize()} API"], checker="source_api_diff",
                    params={"language": lang, "baseline": "HEAD", "allow_additive": True},
                    assumptions=[f"public = {'exported (capitalized) names outside internal/' if lang == 'go' else 'pub items' if lang == 'rust' else 'public and protected members'}"],
                    confidence=0.75,
                ))
            return out or [self._unenforceable(c, ClauseKind.SEMANTIC_INVARIANT, "no package, entry file or Go/Rust/Java project found to check")]

        if intent in (Intent.NO_DATA_LOSS, Intent.PROTECT_PATHS):
            explicit = [_as_glob(p) for p in c.paths if "/" in p or "." in p]
            paths = explicit or (facts.data_paths if intent is Intent.NO_DATA_LOSS else [])
            tables = facts.db_tables if intent is Intent.NO_DATA_LOSS else []
            if not paths and not tables:
                return [self._unenforceable(c, ClauseKind.EFFECT_PROHIBITION, "no protected files, folders or tables could be found in the repo")]
            source = "named in the instruction" if explicit else "found in the repo"
            out = []
            databases: list[str] = []
            if intent is Intent.NO_DATA_LOSS:
                # SQLite files are guarded row by row, so the agent's own inserts survive;
                # protecting the whole file would roll those back too.
                databases = [p for p in paths if p.endswith(DB_SUFFIXES)]
                if not explicit:
                    databases = list(dict.fromkeys(databases + facts.db_files))
                paths = [p for p in paths if p not in databases]
            deny = ["delete", "truncate", "overwrite"] + (["rename"] if intent is Intent.PROTECT_PATHS else [])
            if paths or tables:
                params: dict = {"deny": deny, "paths": paths}
                if databases and paths:
                    params["exclude"] = [f"{d}{s}" for d in databases for s in ("", "-journal", "-wal", "-shm")]
                if tables:
                    params["db_tables"] = tables
                out.append(Clause(
                    id=self._id(), kind=ClauseKind.EFFECT_PROHIBITION, source_span=c.source_span,
                    scope=paths + [f"table {t}" for t in tables], checker="effect_monitor", params=params,
                    on_violation=OnViolation.ROLLBACK,
                    assumptions=[f"protected locations {source}: {', '.join(paths + tables)}"],
                    confidence=0.9 if explicit else 0.75,
                ))
            if databases:
                out.append(Clause(
                    id=self._id(), kind=ClauseKind.SEMANTIC_INVARIANT, source_span=c.source_span,
                    scope=[f"rows in {d}" for d in databases], checker="sqlite_guard",
                    params={"databases": databases}, on_violation=OnViolation.ROLLBACK,
                    assumptions=[f"SQLite databases {source}: {', '.join(databases)}; new rows are allowed"],
                    confidence=0.9 if explicit else 0.75,
                ))
            return out

        if intent in (Intent.TESTS_BEFORE_COMMIT, Intent.TESTS_BEFORE_PUSH):
            action = "commit" if intent is Intent.TESTS_BEFORE_COMMIT else "push"
            if not facts.test_commands:
                return [Clause(
                    id=self._id(), kind=ClauseKind.TEMPORAL_OBLIGATION, source_span=c.source_span,
                    checker="trace_monitor", params={"formula": TESTS_BEFORE.format(action=action)},
                    status=ClauseStatus.PENDING_CLARIFICATION,
                    assumptions=["no test command found; add params.test_cmd"], confidence=0.5,
                )]
            cmd, source = facts.test_commands[0]
            return [Clause(
                id=self._id(), kind=ClauseKind.TEMPORAL_OBLIGATION, source_span=c.source_span,
                checker="trace_monitor",
                params={"formula": TESTS_BEFORE.format(action=action), "test_cmd": cmd},
                assumptions=[f"tests = `{cmd}` (from {source})"], confidence=0.85,
            )]

        if intent in (Intent.NO_COMMIT, Intent.NO_PUSH):
            action = "commit" if intent is Intent.NO_COMMIT else "push"
            return [Clause(
                id=self._id(), kind=ClauseKind.TEMPORAL_OBLIGATION, source_span=c.source_span,
                checker="trace_monitor", params={"formula": f"G(!{action})"}, confidence=0.95,
            )]

        if intent is Intent.ACTION_LIMIT:
            actions = [a for a in ("commit", "push") if re.search(rf"\b{a}", c.source_span, re.I)] or ["push"]
            n = _count(c.source_span)
            if n is None:
                return [Clause(
                    id=self._id(), kind=ClauseKind.TEMPORAL_OBLIGATION, source_span=c.source_span,
                    checker="trace_monitor", params={"formula": f"G(#{actions[0]} <= 1)"},
                    status=ClauseStatus.PENDING_CLARIFICATION,
                    assumptions=["the limit isn't a number Covenant can read; set it in the formula"], confidence=0.4,
                )]
            return [Clause(
                id=self._id(), kind=ClauseKind.TEMPORAL_OBLIGATION, source_span=c.source_span,
                checker="trace_monitor", params={"formula": "G(" + " & ".join(f"#{a} <= {n}" for a in actions) + ")"},
                assumptions=[f"{' and '.join(actions)}: at most {n} time{'s' if n != 1 else ''} each for the whole task"],
                confidence=0.9,
            )]

        if intent is Intent.TESTS_WITHIN_STEPS:
            n = _count(c.source_span, r"(?:steps?|actions?|tool calls?)")
            params: dict = {"formula": TESTS_WITHIN.format(n=n or 3)}
            if facts.test_commands:
                params["test_cmd"] = facts.test_commands[0][0]
            if n is None:
                return [Clause(
                    id=self._id(), kind=ClauseKind.TEMPORAL_OBLIGATION, source_span=c.source_span,
                    checker="trace_monitor", params=params, status=ClauseStatus.PENDING_CLARIFICATION,
                    assumptions=["the number of steps isn't clear; 3 is a placeholder"], confidence=0.4,
                )]
            return [Clause(
                id=self._id(), kind=ClauseKind.TEMPORAL_OBLIGATION, source_span=c.source_span,
                checker="trace_monitor", params=params,
                assumptions=[f"a step is one agent action (an edit, a command, a commit); the tests must run within {n} of them after any edit"]
                + ([f"tests = `{params['test_cmd']}`"] if "test_cmd" in params else []),
                confidence=0.8,
            )]

        if intent in DIFF_RULES:
            return [self._diff_rule(c)]

        if intent is Intent.FORBIDDEN_COMMANDS:
            deny = [name for name, rx in COMMAND_WORDS if re.search(rx, c.source_span, re.I)]
            if not deny:
                return [self._unenforceable(c, ClauseKind.EFFECT_PROHIBITION, "couldn't tell which commands are meant")]
            return [Clause(
                id=self._id(), kind=ClauseKind.EFFECT_PROHIBITION, source_span=c.source_span,
                scope=[f"shell commands: {', '.join(deny)}"], checker="command_guard", params={"deny": deny},
                assumptions=[f"refused before they run: {', '.join(CATEGORY_WORDS.get(d, d) for d in deny)}"],
                confidence=0.85,
            )]

        if intent is Intent.NO_DESTRUCTIVE_MIGRATIONS:
            dirs = facts.migration_dirs or ["migrations", "alembic/versions", "prisma/migrations", "db/migrate"]
            return [Clause(
                id=self._id(), kind=ClauseKind.SEMANTIC_INVARIANT, source_span=c.source_span,
                scope=[f"{d}/" for d in dirs], checker="schema_guard", params={"dirs": dirs, "baseline": "HEAD"},
                assumptions=[f"migrations live in {', '.join(dirs)}"], confidence=0.8,
            )]
        if intent is Intent.DEPENDENCY_SCOPE:
            names = [p for p in c.paths if "/" not in p and not p.endswith((".toml", ".json", ".txt", ".lock"))]
            names += _named_packages(c.source_span)
            params = {"baseline": "HEAD", "deny_major": bool(re.search(r"major", c.source_span, re.I)) or not names}
            if names:
                params["allow"] = list(dict.fromkeys(names))
            return [Clause(
                id=self._id(), kind=ClauseKind.SEMANTIC_INVARIANT, source_span=c.source_span,
                scope=["dependency manifests and lockfiles"], checker="dependency_diff", params=params,
                assumptions=[f"only {', '.join(names)} may change" if names else "any package may change, within its major version"],
                confidence=0.7,
            )]
        if self.review and intent is Intent.OTHER:
            return [Clause(
                id=self._id(), kind=ClauseKind.SEMANTIC_INVARIANT, source_span=c.source_span,
                scope=["the change since the task started"], checker="llm_review",
                params={"rule": c.reading or c.source_span, "baseline": "HEAD"}, on_violation=OnViolation.WARN,
                assumptions=["no automatic check exists; Claude reviews each commit against it (advisory, never blocks)"],
                confidence=0.6,
            )]
        return [self._unenforceable(c, ClauseKind.SEMANTIC_INVARIANT, f"no checker exists for this kind of rule ({c.reading})")]


DIFF_RULES = {
    Intent.NO_NEW_DEPENDENCIES, Intent.ONLY_PATHS, Intent.NO_SECRETS, Intent.KEEP_TESTS, Intent.SMALL_CHANGE,
    Intent.NO_DEBUG_CODE, Intent.NO_SUPPRESSIONS, Intent.NO_LARGE_FILES,
}

# Which command_guard categories an instruction names. Order doesn't matter; all that match apply.
COMMAND_WORDS: list[tuple[str, str]] = [
    ("force_push", r"force[ -]?push|push\s+--force|push\s+-f\b"),
    ("history_rewrite", r"rewrit\w*\s+(?:git\s+)?history|\brebase|\bamend|reset\s+--hard|squash"),
    ("branch_delete", r"delet\w*\s+(?:any\s+|the\s+)?branch|remov\w*\s+(?:any\s+)?branch"),
    ("discard_changes", r"discard|throw\w*\s+away|git\s+clean"),
    ("sudo", r"\bsudo\b|\badmin(?:istrator)?\b|\broot\b|elevat"),
    ("pipe_to_shell", r"\bpipe\b|curl\s*\|\s*(?:ba)?sh|download\w*.*\b(?:run|execut)|remote\s+scripts?"),
    ("recursive_delete", r"rm\s+-rf|recursive\w*\s+delet"),
    ("install_packages", r"\binstall"),
    ("publish", r"\bpublish|\brelease"),
    ("deploy", r"\bdeploy|\bproduction\b|\bprod\b|infrastructure|kubectl|terraform|\bhelm\b"),
    ("network", r"\bnetwork|\binternet|\bonline\b|\bcurl\b|\bwget\b|http\s+requests"),
    ("read_secrets", r"\b(?:read|open|look\s+at|access|cat|print|show)\b.*(?:\.env\b|secret|credential|\bssh\b|\bkeys?\b)"),
    ("database_cli", r"database\s+(?:shell|cli|directly)|\bpsql\b|\bmysql\b"),
    ("kill_processes", r"\bkill|terminat\w*\s+process|stop\w*\s+process"),
    ("system_changes", r"system\s+(?:settings|config\w*)|registry|\bchmod\b|crontab"),
]
CATEGORY_WORDS = {
    "force_push": "force pushes", "history_rewrite": "rewriting git history", "branch_delete": "deleting branches",
    "discard_changes": "discarding uncommitted work", "sudo": "admin rights", "pipe_to_shell": "piping downloads into a shell",
    "recursive_delete": "rm -rf outside the task", "install_packages": "installing packages", "publish": "publishing",
    "deploy": "deploying", "network": "network access", "read_secrets": "reading secret files",
    "database_cli": "database shells", "kill_processes": "killing processes", "system_changes": "system settings",
}


def _numbers(span: str, unit: str) -> int | None:
    m = re.search(rf"(\d[\d,]*)\s*(?:k\s*)?{unit}", span, re.I)
    return int(m.group(1).replace(",", "")) if m else None


def _named_packages(span: str) -> list[str]:
    """Package names in phrases like "only update requests and urllib3"."""
    m = re.search(
        r"\b(?:only|just)\s+(?:update|upgrade|bump|change)\s+(?:the\s+)?(.+?)(?:\s+(?:dependenc\w*|packages?))?$",
        span,
        re.I,
    )
    if not m:
        return []
    words = re.split(r"\s*(?:,|\band\b|\bor\b)\s*", m.group(1))
    names = [w.strip("`'\" ") for w in words]
    return [n for n in names if re.fullmatch(r"[A-Za-z0-9@][\w.@/-]*", n)]
