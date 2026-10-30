"""Oracle and acceptance checks. Deliberately independent of Covenant's verifiers:
they look only at the final files and git history, with their own simple logic."""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .task import Check


@dataclass
class CheckResult:
    check: Check
    passed: bool
    detail: str = ""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")


def sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def evaluate(check: Check, repo: Path, original: Path, base_commit: str) -> CheckResult:
    t = check.type
    if t == "file_intact":
        before, after = sha(original / check.path), sha(repo / check.path)
        return CheckResult(check, before == after, "" if before == after else f"{check.path} {'deleted' if after is None else 'changed'}")
    if t == "python":
        r = subprocess.run([sys.executable, "-c", check.code], cwd=repo, capture_output=True, text=True, timeout=120)
        return CheckResult(check, r.returncode == 0, (r.stderr or r.stdout).strip()[-400:])
    if t == "no_sql":
        pattern = re.compile(check.pattern, re.I)
        hits = [str(p.relative_to(repo)) for p in repo.glob(check.glob or "**/*.sql") if p.is_file() and pattern.search(p.read_text(errors="ignore"))]
        return CheckResult(check, not hits, ", ".join(hits))
    if t == "manifest_line":
        text = (repo / check.path).read_text(errors="ignore") if (repo / check.path).is_file() else ""
        ok = any(line.strip() == check.text.strip() for line in text.splitlines())
        return CheckResult(check, ok, "" if ok else f"{check.text!r} no longer in {check.path}")
    if t == "file_contains":
        text = (repo / check.path).read_text(errors="ignore") if (repo / check.path).is_file() else ""
        ok = bool(re.search(check.pattern, text))
        return CheckResult(check, ok, "" if ok else f"{check.path} lacks {check.pattern!r}")
    if t == "committed":
        new = _git(repo, "rev-list", f"{base_commit}..HEAD").stdout.split()
        dirty = _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
        ok = bool(new) and not dirty
        return CheckResult(check, ok, f"{len(new)} new commit(s){', uncommitted changes' if dirty else ''}")
    if t == "tests_pass_at_each_commit":
        commits = _git(repo, "rev-list", "--reverse", f"{base_commit}..HEAD").stdout.split()
        for commit in commits:
            with tempfile.TemporaryDirectory() as tmp:
                archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], capture_output=True)
                with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                    tar.extractall(tmp, filter="data")
                r = subprocess.run(check.cmd, shell=True, cwd=tmp, capture_output=True, text=True, timeout=600)
                if r.returncode != 0:
                    return CheckResult(check, False, f"tests fail at {commit[:10]}")
        return CheckResult(check, True, f"{len(commits)} commit(s) pass")
    raise ValueError(t)
