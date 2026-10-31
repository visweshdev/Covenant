"""CI check: judge a pull request (base..HEAD) against the Covenant, with no local session.

- The Covenant is read from the *base* commit, so a PR can't weaken the rules it is judged by.
  A PR that changes the Covenant itself is flagged for human review.
- SemanticInvariant clauses run their verifiers with the base commit as baseline.
- EffectProhibition clauses are checked against the diff: deleted, modified or renamed
  protected files.
- TemporalObligation clauses that require passing tests are checked by running the test
  command on the PR's final state (CI can't see the agent's local event order).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import posixpath

import yaml

from . import policy
from .feedback import ViolationReport
from .ir import ClauseKind, Covenant, OnViolation
from .monitors.effects import EffectMonitor
from .events import Effect
from .runtime import PRE_ACTION_CHECKERS, SEMANTIC_VERIFIERS
from .verifiers.test_gate import run_tests

DIFF_OPS = {"D": "delete", "M": "overwrite", "R": "rename", "T": "overwrite"}


@dataclass
class CiResult:
    base: str
    covenant_source: str
    reports: list[ViolationReport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    test_output: dict[str, str] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return bool(self.reports)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")


def resolve_base(repo: Path, base: str | None) -> str:
    candidates = [base] if base else []
    if os.environ.get("GITHUB_BASE_REF"):
        candidates.append(f"origin/{os.environ['GITHUB_BASE_REF']}")
    candidates += ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        result = _git(repo, "merge-base", ref, "HEAD")
        if result.returncode == 0:
            return result.stdout.strip()
    raise ValueError("cannot find the base commit; pass --base (and fetch full history: fetch-depth: 0)")


def load_base_covenant(repo: Path, covenant_path: Path, base: str) -> tuple[Covenant, str, list[str], bool]:
    """The Covenant as of the base commit, with team policies (also as of base) merged in."""
    rel = covenant_path.resolve().relative_to(repo.resolve()).as_posix()
    notes = []
    shown = _git(repo, "show", f"{base}:{rel}")
    if shown.returncode != 0:
        notes.append(f"{rel} does not exist at the base commit; using the PR's copy, which the PR itself could have weakened.")
        merged, _ = policy.load(covenant_path)
        return merged, f"{rel} (from the PR)", notes, False

    def read_at_base(path: str) -> str | None:
        if Path(path).is_absolute():  # a machine-wide policy outside the repo
            return policy.disk_reader(path)
        result = _git(repo, "show", f"{base}:{path}")
        return result.stdout if result.returncode == 0 else None

    data = yaml.safe_load(shown.stdout)
    if isinstance(data, dict) and list(data) == ["covenant"]:
        data = data["covenant"]
    covenant, used = policy.resolve(Covenant.model_validate(data), read_at_base, posixpath.dirname(rel))
    governed = [rel] + [p for p in used if not Path(p).is_absolute()]
    changed = [p for p in governed if _git(repo, "diff", "--quiet", base, "HEAD", "--", p).returncode != 0]
    for path in changed:
        notes.append(f"This PR changes {path}. It was judged by the base version; the change needs human review.")
    return covenant, f"{rel} at {base[:12]}", notes, bool(changed)


def changed_files(repo: Path, base: str) -> list[tuple[str, str]]:
    out = _git(repo, "diff", "--name-status", "-M", base, "HEAD")
    changes = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            changes.append((parts[0][0], parts[1]))  # a rename reports the old path
    return changes


def run_ci(repo: Path, covenant_path: Path, base: str | None = None, run_test_commands: bool = True) -> CiResult:
    repo = repo.resolve()
    base_sha = resolve_base(repo, base)
    covenant, source, notes, covenant_changed = load_base_covenant(repo, covenant_path, base_sha)
    result = CiResult(base_sha, source, notes=notes)
    if covenant_changed:
        result.reports.append(ViolationReport(
            violation="INTEGRITY", clause="the Covenant can't be changed by the work it governs",
            checker="integrity", on_violation=OnViolation.ASK_HUMAN, evidence={"path": covenant_path.name},
            repair_hints=["A human must review and merge Covenant changes separately"], options=["ask_human"],
        ))

    for clause in covenant.enforced(ClauseKind.SEMANTIC_INVARIANT):
        verifier = SEMANTIC_VERIFIERS.get(clause.checker or "")
        if verifier is None:
            continue
        try:
            result.reports += verifier(clause, repo, base_sha)
        except Exception as exc:
            result.reports.append(ViolationReport.for_clause(
                clause, {"error": f"{type(exc).__name__}: {exc}"}, hints=["The verifier could not run in CI"]))

    changes = changed_files(repo, base_sha)
    for clause in covenant.enforced(ClauseKind.EFFECT_PROHIBITION):
        if clause.checker in PRE_ACTION_CHECKERS:  # commands and budgets are judged while the agent works
            continue
        monitor = EffectMonitor(clause, repo)
        for status, path in changes:
            op = DIFF_OPS.get(status)
            if op and monitor.violates(Effect(op=op, target=path)):
                result.reports.append(ViolationReport.for_clause(clause, {"op": op, "target": path, "channel": "git"}))

    for clause in covenant.enforced(ClauseKind.TEMPORAL_OBLIGATION):
        cmd = clause.params.get("test_cmd")
        if not cmd or "test_pass" not in clause.params.get("formula", ""):
            continue
        if not run_test_commands:
            result.notes.append(f"{clause.id}: tests not run (--no-tests)")
            continue
        if cmd not in result.test_output:
            event, output = run_tests(cmd, repo)
            result.test_output[cmd] = output
            if not event.passed:
                result.reports.append(ViolationReport.for_clause(
                    clause, {"test_cmd": cmd, "exit_code": event.exit_code, "output_tail": output[-2000:]},
                    hints=[f"`{cmd}` fails on this PR; fix the failures before merging"]))
    return result


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def github_annotations(result: CiResult) -> list[str]:
    lines = []
    for r in result.reports:
        file = r.evidence.get("target") or r.evidence.get("file") or r.evidence.get("exported_from") or r.evidence.get("path")
        where = f" file={file}" if file and ":" not in str(file) else ""
        line = r.evidence.get("line")
        if where and isinstance(line, int):
            where += f",line={line}"
        detail = r.evidence.get("symbol") or r.evidence.get("target") or r.evidence.get("test_cmd") or ""
        hint = f" Fix: {r.repair_hints[0]}" if r.repair_hints else ""
        lines.append(f"::error{where},title=Covenant {r.violation}::{_escape(f'{r.clause}: {detail}.{hint}')}")
    for note in result.notes:
        lines.append(f"::warning title=Covenant::{_escape(note)}")
    return lines


def markdown_summary(result: CiResult) -> str:
    head = "### Covenant: " + ("❌ rules broken" if result.failed else "✅ all rules hold")
    lines = [head, "", f"Rules from `{result.covenant_source}`, compared with base `{result.base[:12]}`.", ""]
    if result.reports:
        lines += ["| Rule | Instruction | Evidence | Fix |", "|---|---|---|---|"]
        for r in result.reports:
            ev = r.evidence.get("symbol") or r.evidence.get("target") or r.evidence.get("test_cmd") or r.evidence.get("error", "")
            if r.evidence.get("before") or r.evidence.get("after"):
                ev = f"{ev}: `{r.evidence.get('before')}` → `{r.evidence.get('after')}`"
            fix = r.repair_hints[0] if r.repair_hints else ""
            lines.append(f"| {r.violation} | {r.clause} | {str(ev).replace('|', '/')} | {fix.replace('|', '/')} |")
    for note in result.notes:
        lines.append(f"\n> {note}")
    return "\n".join(lines) + "\n"
