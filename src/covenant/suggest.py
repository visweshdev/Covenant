"""`covenant suggest`: rules worth having, learned from what went wrong in this repo before.

Reads git history (and, with --github, recent PR review comments through the gh CLI) for
signs of trouble: reverted commits, secrets that had to be removed, deleted or skipped tests,
debug code and warning suppressions cleaned up later, broken APIs restored, dependency
downgrades, large files, and the paths that keep getting reverted. Each suggestion comes with
the evidence and a sentence ready for `covenant compile` or `covenant amend`.

Nothing is sent anywhere; it is all local git (plus gh, when asked).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Suggestion:
    rule: str  # plain English, ready to compile
    why: str
    evidence: list[str] = field(default_factory=list)
    score: int = 0


# (rule, what it means, commit-message pattern)
SIGNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("Don't commit secrets or API keys", "secrets had to be removed or rotated",
     re.compile(r"\b(?:remove|delete|rotate|revoke|leak\w*|scrub|purge)\b.*\b(?:secret|api[ _-]?key|token|password|credential|\.env)\b|"
                r"\b(?:secret|api[ _-]?key|token|password|credential)s?\b.*\b(?:leak\w*|exposed|committed by mistake)", re.I)),
    ("Don't delete or skip tests", "tests were deleted, skipped or had to be restored",
     re.compile(r"\b(?:restore|re-?enable|unskip|bring back|un-?xfail)\b.*\btests?\b|\btests?\b.*\b(?:were|was)\s+(?:deleted|skipped|disabled)", re.I)),
    ("No leftover debug prints", "debug code was cleaned up after the fact",
     re.compile(r"\b(?:remove|delete|drop|clean\s*up)\b.*\b(?:debug|console\.log|print\s*statements?|prints|breakpoint|pdb|debugger)\b", re.I)),
    ("Don't silence type errors or lint warnings", "suppressions were removed later",
     re.compile(r"\b(?:remove|delete|drop|fix)\b.*\b(?:noqa|type:\s*ignore|ts-ignore|eslint-disable|suppress\w*)\b", re.I)),
    ("Don't change public APIs", "a public API broke and had to be restored",
     re.compile(r"\b(?:restore|revert|re-?add|bring back)\b.*\b(?:api|signature|parameter|argument|export|backward\w*[ -]compat\w*)\b|"
                r"\bbreaking change\b|\bbackward\w*[ -]compat", re.I)),
    ("Don't do major version upgrades", "dependency upgrades were rolled back",
     re.compile(r"\b(?:downgrade|pin|revert|roll\s*back)\b.*\b(?:dependenc\w*|version|upgrade|bump|package)\b|"
                r"\brevert\b.*\b(?:bump|upgrade)\b", re.I)),
    ("No destructive migrations", "a migration lost data or had to be undone",
     re.compile(r"\b(?:revert|fix|restore|undo)\b.*\bmigration|\bmigration\b.*\b(?:data loss|dropped|lost)\b", re.I)),
    ("Never force push or rewrite git history", "history was rewritten or force-pushed",
     re.compile(r"\bforce[ -]?push\w*\b|\brecover\w*\b.*\b(?:commits?|history|branch)\b", re.I)),
    ("Run tests before committing", "commits broke the build or tests",
     re.compile(r"\bfix\w*\b.*\b(?:broken|failing|red)\b.*\b(?:build|tests?|ci)\b|\b(?:build|ci|tests?)\b.*\b(?:broken|failing)\b", re.I)),
]
REVIEW_SIGNS: list[tuple[str, re.Pattern[str]]] = [
    ("Don't commit secrets or API keys", re.compile(r"\b(?:secret|api[ _-]?key|token|credential|password)\b", re.I)),
    ("Don't delete or skip tests", re.compile(r"\b(?:skip\w*|delet\w*|remov\w*)\b.*\btests?\b|\btests?\b.*\b(?:skip|missing)", re.I)),
    ("No leftover debug prints", re.compile(r"\b(?:debug|console\.log|print\(|leftover)\b", re.I)),
    ("Don't silence type errors or lint warnings", re.compile(r"\b(?:noqa|type:\s*ignore|ts-ignore|eslint-disable)\b", re.I)),
    ("Don't change public APIs", re.compile(r"\b(?:breaking|backward\w*[ -]compat\w*|public api|signature)\b", re.I)),
    ("Don't add new dependencies", re.compile(r"\b(?:new|extra|another)\s+(?:dependency|package|library)\b", re.I)),
    ("Keep the change under 300 lines", re.compile(r"\b(?:too (?:big|large)|split (?:this|it) up|huge pr|smaller prs?)\b", re.I)),
]


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else ""


def _commits(repo: Path, limit: int) -> list[tuple[str, str]]:
    out = _git(repo, "log", f"-n{limit}", "--no-merges", "--format=%h%x00%s%x00%b%x1e")
    commits = []
    for record in out.split("\x1e"):
        parts = record.strip("\n").split("\x00")
        if len(parts) >= 2 and parts[0]:
            commits.append((parts[0], (parts[1] + " " + (parts[2] if len(parts) > 2 else "")).strip()))
    return commits


def _files_of(repo: Path, sha: str) -> list[str]:
    return [f for f in _git(repo, "show", "--name-only", "--format=", sha).splitlines() if f.strip()]


def _deleted_tests(repo: Path, limit: int) -> list[str]:
    out = _git(repo, "log", f"-n{limit}", "-M", "--diff-filter=D", "--name-only", "--format=%x00%h")  # moves aren't deletes
    found, sha = [], ""
    for line in out.splitlines():
        if line.startswith("\x00"):
            sha = line[1:]
        elif re.search(r"(?:^|/)(?:tests?/|test_[^/]+\.py$|[^/]+_test\.(?:py|go)$|[^/]+\.(?:test|spec)\.[jt]sx?$)", line):
            found.append(f"{sha} deleted {line}")
    return found


def _large_blobs(repo: Path, limit: int, min_kb: int = 2048) -> list[str]:
    out = _git(repo, "log", f"-n{limit}", "--diff-filter=A", "--format=", "--raw", "--no-abbrev")
    big = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[4] == "A":
            size = _git(repo, "cat-file", "-s", parts[3]).strip()
            if size.isdigit() and int(size) >= min_kb * 1024:
                big.append(f"{parts[5]} ({int(size) // 1024} KB)")
    return big


def _review_comments(repo: Path) -> list[str]:
    if not shutil.which("gh"):
        return []
    r = subprocess.run(["gh", "api", "repos/{owner}/{repo}/pulls/comments?per_page=100&sort=created&direction=desc",
                        "--jq", ".[].body"], cwd=repo, capture_output=True, text=True, encoding="utf-8", timeout=60)
    return [line for line in r.stdout.splitlines() if line.strip()] if r.returncode == 0 else []


def suggest(repo: Path, limit: int = 500, github: bool = False) -> list[Suggestion]:
    repo = repo.resolve()
    by_rule: dict[str, Suggestion] = {}

    def add(rule: str, why: str, evidence: str, weight: int = 1) -> None:
        s = by_rule.setdefault(rule, Suggestion(rule, why))
        if len(s.evidence) < 5:
            s.evidence.append(evidence)
        s.score += weight

    commits = _commits(repo, limit)
    reverted_paths: Counter[str] = Counter()
    for sha, message in commits:
        for rule, why, rx in SIGNS:
            if rx.search(message):
                add(rule, why, f"{sha} {message[:90]}")
        if re.match(r"revert\b", message, re.I):
            reverted_paths.update(_files_of(repo, sha))
    for line in _deleted_tests(repo, limit):
        add("Don't delete or skip tests", "test files were deleted", line)
    for blob in _large_blobs(repo, min(limit, 300)):
        add("Don't commit large files over 2 MB", "large files were committed", blob, 2)

    hot = [(p, n) for p, n in reverted_paths.most_common(5) if n >= 2]
    for path, n in hot:
        folder = path.rsplit("/", 1)[0] + "/" if "/" in path else path
        add(f"Don't touch {folder}", f"changes to {folder} were reverted {n} times", f"{path}: reverted {n} times", n)

    if github:
        for body in _review_comments(repo):
            for rule, rx in REVIEW_SIGNS:
                if rx.search(body):
                    add(rule, "reviewers kept asking for it", "review: " + " ".join(body.split())[:90])
    return sorted(by_rule.values(), key=lambda s: -s.score)


def render(found: list[Suggestion]) -> str:
    if not found:
        return "No warning signs found in this repo's history. Rule packs are a good start: `covenant packs`."
    lines = ["Rules this repo's history suggests:", ""]
    for n, s in enumerate(found, 1):
        lines.append(f"{n}. {s.rule}  (seen {s.score}x: {s.why})")
        lines += [f"     {e}" for e in s.evidence[:3]]
    sentence = ". ".join(s.rule for s in found[:6]) + "."
    lines += ["", "To use them:", f'  covenant amend "{sentence}"', "or, for a new task:",
              f'  covenant compile "<your task>. {sentence}"']
    return "\n".join(lines)


def as_json(found: list[Suggestion]) -> str:
    return json.dumps([s.__dict__ for s in found], indent=2)
