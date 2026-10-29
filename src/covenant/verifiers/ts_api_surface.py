"""ts_api_surface_diff: a TypeScript package's exported API must not change.

Runs the TypeScript compiler (the project's own copy) over the entry files and
records every exported name with its resolved type, plus public class and
interface members. The baseline commit is extracted inside node_modules so it
resolves dependency types exactly like the working tree does.

Clause params:
    entries:        entry files, e.g. ["src/index.ts"] (default: detected)
    baseline:       git ref (the runtime pins it to a sha at start)
    allow_additive: new exports are fine (default true)
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from ..feedback import ViolationReport
from ..ir import Clause
from .api_surface import diff_surfaces

EXTRACTOR = Path(__file__).with_name("ts_extract.js")
DEFAULT_ENTRIES = ["src/index.ts", "src/index.tsx", "index.ts", "lib/index.ts", "src/main.ts"]


class TsSurfaceError(RuntimeError):
    pass


def detect_entries(repo_root: Path) -> list[str]:
    pkg = repo_root / "package.json"
    if pkg.is_file():
        data = json.loads(pkg.read_text(encoding="utf-8"))
        source = data.get("source")
        if isinstance(source, str) and (repo_root / source).is_file():
            return [source]
    for entry in DEFAULT_ENTRIES:
        if (repo_root / entry).is_file():
            return [entry]
    raise TsSurfaceError("no TypeScript entry file found; set params.entries")


def extract(root: Path, entries: list[str], timeout: float = 300) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        raise TsSurfaceError("node is not installed")
    result = subprocess.run(
        [node, str(EXTRACTOR), str(root), *entries],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise TsSurfaceError(result.stderr.strip() or f"extractor exited {result.returncode}")
    return json.loads(result.stdout)


def materialize(repo_root: Path, ref: str) -> Path:
    """Check out `ref` (read-only use) where node resolution reaches the repo's node_modules."""
    sha = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True, text=True,
    )
    if sha.returncode != 0:
        raise TsSurfaceError(f"cannot resolve baseline {ref!r}")
    node_modules = repo_root / "node_modules"
    base = node_modules / ".covenant-baseline" if node_modules.is_dir() else Path(tempfile.gettempdir()) / "covenant-baseline"
    dest = base / sha.stdout.strip()
    if (dest / ".complete").exists():
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    archive = subprocess.run(["git", "-C", str(repo_root), "archive", "--format=tar", sha.stdout.strip()], capture_output=True)
    if archive.returncode != 0:
        raise TsSurfaceError(archive.stderr.decode(errors="replace").strip())
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest, filter="data")
    (dest / ".complete").write_text("", encoding="utf-8")
    return dest


def verify(clause: Clause, repo_root: Path, baseline: str | None = None) -> list[ViolationReport]:
    params = clause.params
    entries = list(params.get("entries") or detect_entries(repo_root))
    ref = baseline or params.get("baseline", "HEAD")
    after = extract(repo_root, entries)
    before = extract(materialize(repo_root, ref), entries)
    diff = diff_surfaces(before, after)

    def report(symbol: str, old: str | None, new: str | None) -> ViolationReport:
        entry, _, _ = symbol.partition(":")
        evidence = {"symbol": symbol, "before": old, "after": new, "exported_from": entry, "baseline": ref}
        return ViolationReport.for_clause(clause, evidence)

    reports = [report(s, b, a) for s, (b, a) in diff.changed.items()]
    reports += [report(s, b, None) for s, b in diff.removed.items()]
    if not params.get("allow_additive", True):
        reports += [report(s, None, a) for s, a in diff.added.items()]
    return reports
