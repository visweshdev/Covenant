"""Checkers that read what changed since the task started (working tree versus the baseline).

    scope_guard     only files matching `allow` may change; optionally no new or deleted files
    secret_scan     no API keys, tokens, private keys or passwords in added lines or new .env files
    test_guard      tests aren't deleted, removed, skipped or focused (.only)
    diff_budget     at most `max_files` files and `max_lines` changed lines
    no_debug_code   no breakpoints, debugger statements or debug prints left in the code
    no_suppressions no new `# type: ignore`, `# noqa`, `@ts-ignore`, `eslint-disable` and the like
    large_files     no added or changed file over `max_kb`

All share: `baseline` (git ref, pinned to a sha at start) and `exclude` (globs to ignore).
Covenant's own files (covenant.yaml, .claude/) are never counted.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..feedback import ViolationReport
from ..ir import Clause
from ..monitors.effects import compile_glob

OWN_FILES = [compile_glob(p) for p in ("covenant.yaml", ".claude/**", ".covenant/**")]
MAX_TEXT = 2_000_000  # bigger files are only checked by large_files


@dataclass
class Change:
    path: str
    status: str  # A added, M modified, D deleted, R renamed
    old_path: str | None = None
    _repo: Path = field(default=Path("."), repr=False)
    _base: str = field(default="HEAD", repr=False)
    # Hunks from one `git diff -U0` shared by every change of a check: (old_count, new_start, new_lines)
    _hunks: list[tuple[int, int, list[str]]] | None = field(default=None, repr=False)
    _removed: list[str] = field(default_factory=list, repr=False)  # removed lines of the whole diff

    def old_text(self) -> str | None:
        if self.status == "A":
            return None
        r = subprocess.run(["git", "-C", str(self._repo), "show", f"{self._base}:{self.old_path or self.path}"],
                           capture_output=True)
        return _text(r.stdout) if r.returncode == 0 else None

    def new_text(self) -> str | None:
        if self.status == "D":
            return None
        path = self._repo / self.path
        try:
            if path.stat().st_size > MAX_TEXT:
                return None
            return _text(path.read_bytes())
        except OSError:
            return None

    def added_lines(self) -> list[tuple[int, str]]:
        """(1-based line number, text) of lines that are new in the working tree."""
        if self._hunks is not None:
            return [(start + i, line) for _, start, lines in self._hunks for i, line in enumerate(lines)]
        new = self.new_text()
        if new is None:
            return []
        new_lines = new.splitlines()
        old = self.old_text()
        if old is None:
            return list(enumerate(new_lines, 1))
        out = []
        matcher = difflib.SequenceMatcher(None, old.splitlines(), new_lines, autojunk=False)
        for tag, _, _, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "insert"):
                out += [(j + 1, new_lines[j]) for j in range(j1, j2)]
        return out

    def changed_line_count(self) -> int:
        if self._hunks is not None:
            return sum(max(old_count, len(lines)) for old_count, _, lines in self._hunks)
        old, new = (self.old_text() or "").splitlines(), (self.new_text() or "").splitlines()
        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")


def _text(data: bytes) -> str | None:
    if b"\0" in data[:8000]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def changes(repo: Path, base: str, exclude: list[str] | None = None) -> list[Change]:
    """Every file that differs from `base`: tracked changes plus new files git doesn't ignore."""
    skip = OWN_FILES + [compile_glob(p) for p in exclude or []]
    out: list[Change] = []
    fields = _git(repo, "diff", "--name-status", "-z", "-M", base, "--").split("\0")
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i][0]
        if status == "R":
            out.append(Change(fields[i + 2], "R", fields[i + 1], repo, base))
            i += 3
        else:
            out.append(Change(fields[i + 1], status if status in "AMD" else "M", None, repo, base))
            i += 2
    removed: list[str] = []
    hunks = _diff_hunks(repo, base, removed)
    for c in out:
        c._removed = removed
        if c.status != "D":
            c._hunks = hunks.get(c.path, [])  # absent: binary, or renamed without edits
    for path in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if path:
            out.append(Change(path, "A", None, repo, base))  # untracked: every line is new
    return [c for c in out if not any(g.fullmatch(c.path) for g in skip)]


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _unquote(path: str) -> str:
    """git quotes paths with unusual characters C-style: "b/caf\\303\\251.txt"."""
    if not (path.startswith('"') and path.endswith('"')):
        return path
    raw = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
    return raw.encode("latin-1", "replace").decode("utf-8", "replace")


def _diff_hunks(repo: Path, base: str, removed: list[str] | None = None) -> dict[str, list[tuple[int, int, list[str]]]]:
    """Added lines per file from one `git diff -U0` (working tree versus base); binary files are left out.
    Removed lines, from every file, are appended to `removed` when given.

    Much faster than diffing each file in Python, which is quadratic on big files."""
    r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", "diff", "-U0", "-M", "--no-color",
                        "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/", base, "--"],
                       capture_output=True)
    if r.returncode != 0:
        return {}
    out: dict[str, list[tuple[int, int, list[str]]]] = {}
    current: list | None = None
    lines: list[str] = []
    in_header = False
    for raw in r.stdout.decode("utf-8", errors="replace").split("\n"):
        if raw.startswith("diff --git "):
            in_header, current = True, None
        elif in_header and raw.startswith("+++ "):
            target = _unquote(raw[4:].rstrip("\r\t"))
            current = out.setdefault(target[2:], []) if target.startswith("b/") else None
        elif raw.startswith("@@"):
            in_header = False
            m = _HUNK.match(raw)
            if m and current is not None:
                lines = []
                old_count = int(m.group(1)) if m.group(1) is not None else 1
                current.append((old_count, int(m.group(2)), lines))
        elif not in_header and raw.startswith("+") and current:
            lines.append(raw[1:].rstrip("\r"))
        elif not in_header and raw.startswith("-") and removed is not None:
            removed.append(raw[1:].rstrip("\r"))
    return out


def _base(clause: Clause, baseline: str | None) -> str:
    return baseline or clause.params.get("baseline", "HEAD")


def _report(clause: Clause, hint: str, **evidence) -> ViolationReport:
    return ViolationReport.for_clause(clause, evidence, hints=[hint])


# ----- scope_guard -------------------------------------------------------------------------


def scope_guard(clause: Clause, repo: Path, baseline: str | None = None) -> list[ViolationReport]:
    p = clause.params
    allow = [compile_glob(g) for g in p.get("allow", ["**"])]
    reports = []
    for c in changes(repo, _base(clause, baseline), p.get("exclude")):
        if not any(g.fullmatch(c.path) for g in allow):
            reports.append(_report(clause, f"Only {', '.join(p.get('allow', []))} may change; revert {c.path}",
                                   target=c.path, op={"A": "add", "D": "delete", "R": "rename"}.get(c.status, "modify")))
        elif c.status == "A" and p.get("allow_new") is False:
            reports.append(_report(clause, f"New files aren't allowed in this task; remove {c.path} or ask the human",
                                   target=c.path, op="add"))
        elif c.status in ("D", "R") and p.get("allow_delete") is False:
            reports.append(_report(clause, f"Deleting or renaming files isn't allowed; restore {c.old_path or c.path}",
                                   target=c.old_path or c.path, op="delete"))
    return reports


def scope_allows(clause: Clause, rel_path: str) -> bool:
    """For editors and the PreToolUse hook: may this file be changed at all?"""
    if any(g.fullmatch(rel_path) for g in OWN_FILES):
        return True
    return any(compile_glob(g).fullmatch(rel_path) for g in clause.params.get("allow", ["**"]))


# ----- secret_scan -------------------------------------------------------------------------

SECRETS: list[tuple[str, re.Pattern[str]]] = [(name, re.compile(rx)) for name, rx in [
    ("Anthropic API key", r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    ("OpenRouter API key", r"sk-or-v1-[A-Za-z0-9]{32,}"),
    ("OpenAI API key", r"\bsk-(?!ant-|or-)(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}"),
    ("GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"),
    ("GitLab token", r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    ("AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("AWS secret key", r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}"),
    ("Slack token", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    ("Stripe live key", r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}"),
    ("Google API key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("Hugging Face token", r"\bhf_[A-Za-z0-9]{30,}"),
    ("npm token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("private key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"),
    ("JSON web token", r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ("password in a URL", r"\b[a-z][a-z0-9+.\-]*://[^/\s:@'\"]+:[^/\s:@'\"]{6,}@[\w.\-]+"),
]]
_ASSIGNED = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_\-]?key|apikey|auth[_\-]?token|access[_\-]?token|client[_\-]?secret|private[_\-]?key)"
    r"\w*\s*[:=]\s*['\"]([^'\"\s]{10,})['\"]"
)
_PLACEHOLDER = re.compile(r"(?i)x{4,}|\*{4,}|example|placeholder|changeme|change[_\-]me|your[_\-]|dummy|fake|test|sample|redacted|<[^>]+>|\$\{|\{\{|%\(")


def _looks_random(value: str) -> bool:
    if re.fullmatch(r"[a-z]+(?:[-_. ][a-z]+)*", value):
        return False  # lowercase words: 'private-token', 'my_secret_value'
    kinds = sum(bool(re.search(rx, value)) for rx in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    return len(set(value)) >= 8 and kinds >= 2


def mask(secret: str) -> str:
    return f"{secret[:4]}…({len(secret)} chars)"


def find_secrets(line: str) -> list[tuple[str, str]]:
    found, taken = [], []
    for name, rx in SECRETS:  # most specific first; an overlapping, vaguer match is the same secret
        for m in rx.finditer(line):
            if not any(m.start() < end and start < m.end() for start, end in taken):
                found.append((name, m.group(0)))
                taken.append(m.span())
    if not found:
        for m in _ASSIGNED.finditer(line):
            value = m.group(2)
            if not _PLACEHOLDER.search(value) and _looks_random(value):
                found.append((f"hard-coded {m.group(1).lower()}", value))
    return found


def secret_scan(clause: Clause, repo: Path, baseline: str | None = None) -> list[ViolationReport]:
    p = clause.params
    reports = []
    for c in changes(repo, _base(clause, baseline), p.get("exclude")):
        name = c.path.rsplit("/", 1)[-1]
        if c.status == "A" and re.fullmatch(r"\.env(\.[\w.-]+)?", name) and not re.search(r"example|sample|template|dist", name):
            reports.append(_report(clause, f"Don't commit {c.path}; keep secrets out of the repo (add it to .gitignore)",
                                   target=c.path, kind="environment file"))
            continue
        for lineno, text in c.added_lines():
            for kind, secret in find_secrets(text):
                reports.append(_report(
                    clause, f"Remove the {kind} from {c.path}:{lineno} and load it from an environment variable or a "
                    "secrets manager. If it was ever real, it must be revoked",
                    target=c.path, line=lineno, kind=kind, secret=mask(secret)))
    return reports


# ----- test_guard --------------------------------------------------------------------------

TEST_FILES = ["tests/**", "test/**", "**/tests/**", "**/test/**", "**/__tests__/**", "**/test_*.py", "**/*_test.py",
              "**/*_test.go", "**/*.test.*", "**/*.spec.*", "**/*Test.java", "**/*Tests.java", "**/*Test.kt"]
_TEST_CASE = re.compile(
    r"^\s*(?:async\s+)?def\s+test\w*\s*\(|"  # Python
    r"\b(?:it|test)\s*\(\s*['\"`]|"  # JavaScript/TypeScript
    r"^func\s+Test\w+\s*\(|"  # Go
    r"#\[(?:tokio::)?test\]|"  # Rust
    r"@(?:Test|ParameterizedTest)\b",  # Java/Kotlin
    re.M,
)
_SKIP = re.compile(
    r"@pytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.skip\(|@unittest\.skip|"
    r"\b(?:it|test|describe)\.(?:skip|todo)\s*\(|\bx(?:it|test|describe)\s*\(|"
    r"\bt\.Skip(?:f|Now)?\(|#\[ignore\]|@(?:Disabled|Ignore)\b",
)
_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", re.M)
_FOCUS = re.compile(r"\b(?:it|test|describe)\.only\s*\(|\bf(?:it|describe)\s*\(")


def is_test_file(path: str, globs: list[str] | None = None) -> bool:
    return any(compile_glob(g).fullmatch(path) for g in globs or TEST_FILES)


def test_guard(clause: Clause, repo: Path, baseline: str | None = None) -> list[ViolationReport]:
    p = clause.params
    globs = p.get("test_files") or TEST_FILES
    reports = []
    for c in changes(repo, _base(clause, baseline), p.get("exclude")):
        was_test = is_test_file(c.old_path or c.path, globs)
        if c.status == "D" and was_test:
            reports.append(_report(clause, f"Restore {c.path}; deleting tests isn't allowed", target=c.path, change="deleted"))
            continue
        if not (was_test or is_test_file(c.path, globs)):
            continue
        old, new = c.old_text() or "", c.new_text() or ""
        before, after = len(_TEST_CASE.findall(old)), len(_TEST_CASE.findall(new))
        if after < before:
            reports.append(_report(clause, f"{c.path} has {before - after} fewer test(s); restore them or ask the human",
                                   target=c.path, change="tests removed", before=before, after=after))
        old_defs = set(_PY_DEF.findall(old))
        new_lines = new.splitlines()

        def on_new_test(lineno: int) -> bool:
            """A skip decorator on a test that is itself new: nothing that ran before is skipped."""
            # Walk past the rest of the decorator stack (multi-line conditions, long parametrize
            # tables) to the function it decorates.
            depth = 0
            for text in new_lines[lineno - 1:lineno + 200]:
                stripped = text.strip()
                if depth == 0 and stripped and not stripped.startswith(("@", "#", ")", "]", "}")):
                    m = _PY_DEF.match(text)
                    if m:
                        return m.group(1) not in old_defs
                    k = re.match(r"\s*class\s+(\w+)", text)  # a skipped test class that is itself new
                    return bool(k) and not re.search(rf"^\s*class\s+{k.group(1)}\b", old, re.M)
                depth = max(0, depth + sum(stripped.count(ch) for ch in "([{") - sum(stripped.count(ch) for ch in ")]}"))
            return False

        def inside_new_test(lineno: int) -> bool:
            """`pytest.skip(...)` in the body of a test that is itself new."""
            indent = len(new_lines[lineno - 1]) - len(new_lines[lineno - 1].lstrip())
            for text in reversed(new_lines[: lineno - 1]):
                m = _PY_DEF.search(text)
                if m and len(text) - len(text.lstrip()) < indent:
                    return m.group(1) not in old_defs
            return False

        def new_test_skip(lineno: int, text: str) -> bool:
            return on_new_test(lineno) if text.lstrip().startswith("@") else inside_new_test(lineno)

        for label, rx in (("skipped", _SKIP), ("focused with .only", _FOCUS)):
            added = [(n, t) for n, t in c.added_lines() if rx.search(t)
                     and not (label == "skipped" and c.path.endswith(".py") and new_test_skip(n, t))]
            if len(rx.findall(new)) > len(rx.findall(old)) and added:
                n, t = added[0]
                reports.append(_report(clause, f"Don't mark tests as {label}; fix the code so they pass",
                                       target=c.path, line=n, change=label, code=t.strip()[:120]))
    return reports


# ----- diff_budget -------------------------------------------------------------------------


def diff_budget(clause: Clause, repo: Path, baseline: str | None = None) -> list[ViolationReport]:
    p = clause.params
    found = changes(repo, _base(clause, baseline), p.get("exclude"))
    reports = []
    if p.get("max_files") is not None and len(found) > int(p["max_files"]):
        reports.append(_report(clause, f"Keep the change to {p['max_files']} files or fewer; split it or ask the human",
                               files=len(found), max_files=int(p["max_files"]), changed=[c.path for c in found][:30]))
    if p.get("max_lines") is not None:
        lines = sum(c.changed_line_count() for c in found)
        if lines > int(p["max_lines"]):
            reports.append(_report(clause, f"Keep the change to {p['max_lines']} changed lines or fewer; trim it or ask the human",
                                   lines=lines, max_lines=int(p["max_lines"])))
    return reports


# ----- line patterns: debug code and suppressions --------------------------------------------

_DEBUG: list[tuple[str, re.Pattern[str]]] = [(exts, re.compile(rx)) for exts, rx in [
    (".py", r"\bbreakpoint\(\)|\b(?:i?pdb|pudb)\.set_trace\(|^\s*import\s+(?:i?pdb|pudb)\b"),
    (".js .jsx .ts .tsx .mjs .cjs .vue .svelte", r"^\s*debugger\s*;?\s*$|\bconsole\.(?:log|debug|trace)\("),
    (".rb", r"\bbinding\.(?:pry|irb)\b|\bbyebug\b"),
    (".rs", r"\bdbg!\("),
    (".php", r"\b(?:var_dump|dd|dump)\("),
    (".go", r"\bspew\.Dump\(|\bpp\.Println\("),
    (".java .kt", r"\.printStackTrace\(\)"),
]]
_PRINT = re.compile(r"^\s*print\(")

# What a suppression silences: the type checker, a linter, or coverage. "Don't silence type errors"
# checks only the first; "don't suppress warnings" checks all (the `kinds` param).
_SUPPRESS_KINDS: dict[str, re.Pattern[str]] = {
    "type": re.compile(r"#\s*type:\s*ignore|#\s*mypy:\s*ignore|#\s*pyright:\s*ignore|//\s*@ts-(?:ignore|nocheck|expect-error)|"
                       r"@phpstan-ignore|@psalm-suppress"),
    "lint": re.compile(r"#\s*noqa\b|#\s*pylint:\s*disable|eslint-disable|@SuppressWarnings|#!?\[allow\(|//\s*nolint|"
                       r"#\s*rubocop:\s*disable|NOSONAR|//\s*NOLINT|@SuppressLint|biome-ignore|#\s*ruff:\s*noqa"),
    "coverage": re.compile(r"#\s*pragma:\s*no\s*cover|/\*\s*(?:istanbul|c8|v8)\s+ignore"),
}
_SUPPRESS = re.compile("|".join(rx.pattern for rx in _SUPPRESS_KINDS.values()))
# Type-test files, where `@ts-expect-error` is the assertion ("this call must not type-check").
TYPE_TEST_FILES = ["test-d/**", "**/test-d/**", "**/*.test-d.ts", "**/*.test-d.tsx", "**/*.tst.ts", "type-tests/**",
                   "**/type-tests/**", "types/test/**", "dtslint/**", "**/*-tests.ts"]
# Text that only mentions code (docs, changelogs) and data a tool reads as input (a formatter's test cases).
PROSE_EXTS = (".md", ".mdx", ".rst", ".txt", ".adoc", ".html", ".htm", ".tex", ".ipynb")
DATA_FILES = ["**/tests/data/**", "**/test/data/**", "tests/data/**", "test/data/**", "**/fixtures/**", "fixtures/**",
              "**/testdata/**", "testdata/**", "**/__snapshots__/**", "**/*.snap"]
# Programs whose output is the point: build and release scripts, CLIs, examples.
SCRIPT_FILES = ["bin/**", "scripts/**", "script/**", "tools/**", "tasks/**", ".meta/**", ".github/**", "action/**",
                "examples/**", "example/**", "docs/**", "doc/**", "benchmarks/**", "bench/**", "**/__main__.py",
                "**/cli.py", "**/cli/**", "build/**", "setup.py", "noxfile.py", "tasks.py", "Makefile"]


def _is_comment(ext: str, text: str) -> bool:
    """A line of comment or doc text, where a debug call is an example, not code."""
    s = text.lstrip()
    if ext in (".py", ".rb"):
        return s.startswith("#") or s.startswith(">>> ") or s.startswith("... ")
    return s.startswith(("//", "/*", "* ", "*\t")) or s == "*"


def _line_check(clause: Clause, repo: Path, baseline: str | None, match: Callable[[str, str], str | None],
                hint: str, skip_tests: bool) -> list[ViolationReport]:
    reports = []
    for c in changes(repo, _base(clause, baseline), clause.params.get("exclude")):
        if skip_tests and is_test_file(c.path):
            continue
        for lineno, text in c.added_lines():
            found = match(c.path, text)
            if found:
                reports.append(_report(clause, hint.format(path=c.path, line=lineno),
                                       target=c.path, line=lineno, code=text.strip()[:120], kind=found))
    return reports


def no_debug_code(clause: Clause, repo: Path, baseline: str | None = None) -> list[ViolationReport]:
    include_print = bool(clause.params.get("include_print"))

    def match(path: str, text: str) -> str | None:
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        if _is_comment(ext, text):
            return None
        for exts, rx in _DEBUG:
            if ext in exts.split() and rx.search(text):
                return "debug code"
        if include_print and ext == ".py" and _PRINT.search(text) and "file=" not in text:
            return "print call"
        return None

    reports = []
    for c in changes(repo, _base(clause, baseline), clause.params.get("exclude")):
        if is_test_file(c.path) or c.path.endswith(PROSE_EXTS) or is_test_file(c.path, DATA_FILES + SCRIPT_FILES):
            continue  # a script's output and a doc's example aren't leftovers
        added = c.added_lines()
        hits = [(n, t, k) for n, t in added if (k := match(c.path, t))]
        if not hits:
            continue
        in_comment = _block_comment_lines(c.new_text() or "", c.path)
        for lineno, text, kind in hits:
            if lineno not in in_comment:
                reports.append(_report(clause, f"Remove the leftover debug code at {c.path}:{lineno}",
                                       target=c.path, line=lineno, code=text.strip()[:120], kind=kind))
    return reports


def _block_comment_lines(text: str, path: str) -> set[int]:
    """Line numbers inside /* ... */ comments (JSDoc examples often have no leading `*`) or Python docstrings."""
    if path.endswith(".py"):
        spans = [m.span() for m in re.finditer(r"(\"\"\"|''')[\s\S]*?\1", text)]
    elif path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".go", ".rs", ".java", ".kt", ".php")):
        spans = [m.span() for m in re.finditer(r"/\*[\s\S]*?\*/", text)]
    else:
        return set()
    lines: set[int] = set()
    for start, end in spans:
        lines.update(range(text.count("\n", 0, start) + 1, text.count("\n", 0, end) + 2))
    return lines


def no_suppressions(clause: Clause, repo: Path, baseline: str | None = None) -> list[ViolationReport]:
    kinds = clause.params.get("kinds") or list(_SUPPRESS_KINDS)
    patterns = [_SUPPRESS_KINDS[k] for k in kinds if k in _SUPPRESS_KINDS]

    def match(path: str, text: str) -> str | None:
        if path.endswith(PROSE_EXTS) or is_test_file(path, DATA_FILES):
            return None
        in_tests = is_test_file(path) or is_test_file(path, TYPE_TEST_FILES)
        for rx in patterns:
            for m in rx.finditer(text):
                if "`" in text[: m.start()][-3:]:
                    continue  # quoted in a comment or docstring: `# type: ignore`
                hit = m.group(0)
                # A test that passes a wrong type on purpose names the error it expects, and
                # `@ts-expect-error` fails by itself once the error goes away: both are assertions.
                if in_tests and ("expect-error" in hit or ("type" in hit and text[m.end():].startswith("["))):
                    continue
                return hit
        return None

    found = changes(repo, _base(clause, baseline), clause.params.get("exclude"))
    # A suppression the diff also removes was edited (`# type: ignore` -> `# type: ignore[attr]`),
    # moved or reformatted, not added: each is matched against a removed line with the same code.
    removed = found[0]._removed if found else []
    pool = Counter(_suppressed_code(line) for line in removed if _SUPPRESS.search(line))
    reports = []
    for c in found:
        for lineno, text in c.added_lines():
            kind = match(c.path, text)
            if not kind:
                continue
            key = _suppressed_code(text)
            if pool[key] > 0:
                pool[key] -= 1
                continue
            reports.append(_report(clause, f"Fix the warning instead of silencing it at {c.path}:{lineno}, or ask the human",
                                   target=c.path, line=lineno, code=text.strip()[:120], kind=kind))
    return reports


def _suppressed_code(line: str) -> str:
    """The code a suppression sits on, without the suppression: `x = f()  # type: ignore[misc]` -> `x = f()`.
    A suppression on a line of its own keeps its own text."""
    m = _SUPPRESS.search(line)
    code = line[: m.start()] if m else line
    code = re.sub(r"(#|//|/\*)\s*$", "", code.strip()).strip()
    return " ".join((code or line).split())


# ----- large_files -------------------------------------------------------------------------


def large_files(clause: Clause, repo: Path, baseline: str | None = None) -> list[ViolationReport]:
    max_kb = float(clause.params.get("max_kb", 1024))
    reports = []
    for c in changes(repo, _base(clause, baseline), clause.params.get("exclude")):
        if c.status == "D":
            continue
        try:
            size = (repo / c.path).stat().st_size
        except OSError:
            continue
        if size > max_kb * 1024:
            reports.append(_report(clause, f"{c.path} is {size // 1024} KB (limit {int(max_kb)} KB); keep it out of the "
                                   "repo, or use Git LFS if the human agrees", target=c.path, size_kb=size // 1024))
    return reports


VERIFIERS = {
    "scope_guard": scope_guard,
    "secret_scan": secret_scan,
    "test_guard": test_guard,
    "diff_budget": diff_budget,
    "no_debug_code": no_debug_code,
    "no_suppressions": no_suppressions,
    "large_files": large_files,
}
