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
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from ..feedback import ViolationReport
from ..ir import Clause
from .api_surface import diff_surfaces

EXTRACTOR = Path(__file__).with_name("ts_extract.js")
DEFAULT_ENTRIES = ["src/index.ts", "src/index.tsx", "src/index.mts", "source/index.ts", "index.ts", "lib/index.ts",
                   "src/main.ts", "index.d.ts"]
BUILD_DIRS = ("dist", "distribution", "build", "lib", "out", "esm", "cjs", "types")
SOURCE_DIRS = ("src", "source", "lib", "")
SOURCE_EXTS = (".ts", ".tsx", ".mts", ".cts", ".d.ts", ".d.mts")


class TsSurfaceError(RuntimeError):
    pass


def _published_paths(data: dict) -> list[str]:
    """Files package.json points users at: types first, then the JavaScript entry."""
    found: list[str] = []

    def walk(value, key=""):
        if isinstance(value, str):
            if value.startswith(".") or "/" in value or value.endswith((".js", ".ts", ".mjs", ".cjs", ".mts")):
                found.append(value)
        elif isinstance(value, dict):
            # the package root (".") before subpaths; "types" before "import"/"default"
            items = sorted(value.items(), key=lambda kv: (kv[0] not in (".", "types", "typings"), kv[0] != "import"))
            for k, v in items:
                if not k.startswith("./") or k == "./":
                    walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)

    for key in ("types", "typings"):
        walk(data.get(key))
    walk(data.get("exports"))
    for key in ("module", "main"):
        walk(data.get(key))
    return found


def _out_to_root(repo_root: Path) -> list[tuple[str, str]]:
    """(outDir, rootDir) pairs from the repo's tsconfig files, so built paths map back to source."""
    pairs = []
    for cfg in sorted(repo_root.glob("tsconfig*.json")):
        try:
            text = cfg.read_text(encoding="utf-8")
            opts = json.loads("\n".join(l for l in text.splitlines() if not l.strip().startswith("//"))).get("compilerOptions", {})
        except (ValueError, OSError):
            continue
        out, root = opts.get("outDir"), opts.get("rootDir")
        if isinstance(out, str):
            pairs.append((out.strip("./").rstrip("/"), (root or "src").strip("./").rstrip("/") if root != "." else ""))
    return pairs


def _source_for(repo_root: Path, published: str, pairs: list[tuple[str, str]]) -> str | None:
    path = published.lstrip("./")
    stem = path
    for ext in (".d.mts", ".d.cts", ".d.ts", ".mjs", ".cjs", ".js", ".mts", ".cts", ".tsx", ".ts"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    if (repo_root / path).is_file() and path.endswith(SOURCE_EXTS):  # source or hand-written types in the repo
        return path
    candidates = []
    first, _, rest = stem.partition("/")
    for out, root in pairs:
        if out and (stem == out or stem.startswith(out + "/")):
            candidates.append(f"{root}/{stem[len(out) + 1:]}".lstrip("/"))
    if first in BUILD_DIRS and rest:
        candidates += [f"{d}/{rest}".lstrip("/") for d in SOURCE_DIRS]
        if "/" in rest:  # dist/esm/index.js, dist/src/index.d.ts
            candidates += [f"{d}/{rest.split('/', 1)[1]}".lstrip("/") for d in SOURCE_DIRS]
    candidates.append(stem)
    for cand in candidates:
        for ext in SOURCE_EXTS:
            if (repo_root / f"{cand}{ext}").is_file():
                return f"{cand}{ext}"
    return None


def detect_entries(repo_root: Path) -> list[str]:
    pkg = repo_root / "package.json"
    if pkg.is_file():
        data = json.loads(pkg.read_text(encoding="utf-8"))
        source = data.get("source")
        if isinstance(source, str) and (repo_root / source).is_file():
            return [source]
        pairs = _out_to_root(repo_root)
        for published in _published_paths(data):
            found = _source_for(repo_root, published, pairs)
            if found:
                return [found]
    for entry in DEFAULT_ENTRIES:
        if (repo_root / entry).is_file():
            return [entry]
    found = _workspace_entry(repo_root)
    if found:
        return [found]
    raise TsSurfaceError("no TypeScript entry file found; set params.entries")


def _workspace_entry(repo_root: Path) -> str | None:
    """In a monorepo, the published package named like the repo (zod's packages/zod)."""
    patterns: list[str] = []
    try:
        ws = json.loads((repo_root / "package.json").read_text(encoding="utf-8")).get("workspaces")
        patterns += ws if isinstance(ws, list) else (ws or {}).get("packages", [])
    except (OSError, ValueError, AttributeError):
        pass
    pnpm = repo_root / "pnpm-workspace.yaml"
    if pnpm.is_file():
        patterns += [m.strip().strip("'\"") for m in
                     re.findall(r"^\s*-\s*(.+)$", pnpm.read_text(encoding="utf-8"), re.M)]
    packages = []
    for pattern in patterns:
        if pattern.startswith("!"):
            continue
        for pkg_json in sorted(repo_root.glob(f"{pattern.rstrip('/')}/package.json")):
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not data.get("private") and data.get("name"):
                packages.append((data["name"].rsplit("/", 1)[-1].lower(), pkg_json.parent))
    named = [d for n, d in packages if repo_root.name.lower().endswith(n)]
    chosen = named[0] if named else packages[0][1] if len(packages) == 1 else None
    if chosen is None:
        return None
    try:
        inner = detect_entries(chosen)
    except TsSurfaceError:
        return None
    return f"{chosen.relative_to(repo_root).as_posix()}/{inner[0]}"


def extract_full(root: Path, entries: list[str], timeout: float = 300) -> tuple[dict[str, str], dict[str, dict]]:
    """(surface, signatures): display strings per exported name, plus structured call signatures."""
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
    data = json.loads(result.stdout)
    return data["surface"], data.get("sigs", {})


def extract(root: Path, entries: list[str], timeout: float = 300) -> dict[str, str]:
    return extract_full(root, entries, timeout)[0]


def signatures_compatible(before: dict, after: dict) -> bool:
    """True when every call that type-checked against `before` still type-checks against `after`.

    Parameter names don't matter to TypeScript callers. Existing parameters keep their type
    and kind (a required one may become optional); new parameters must be optional or a rest
    parameter; type parameters and the return type are unchanged.
    """
    if before.get("typeParams") != after.get("typeParams") or before.get("ret") != after.get("ret"):
        return False
    old, new = before.get("params", []), after.get("params", [])
    if len(new) < len(old):
        return False
    for o, n in zip(old, new):
        if o["type"] != n["type"] or o["rest"] != n["rest"]:
            return False
        if o["optional"] and not n["optional"]:
            return False
    extra = new[len(old):]
    if old and old[-1]["rest"] and extra:
        return False
    return all(n["optional"] or n["rest"] for n in extra)


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
    after, after_sigs = extract_full(repo_root, entries)
    before, before_sigs = extract_full(materialize(repo_root, ref), entries)
    diff = diff_surfaces(before, after)
    additive_ok = params.get("allow_additive", True)

    def compatible(symbol: str) -> bool:
        return (
            additive_ok
            and symbol in before_sigs
            and symbol in after_sigs
            and signatures_compatible(before_sigs[symbol], after_sigs[symbol])
        )

    def report(symbol: str, old: str | None, new: str | None) -> ViolationReport:
        entry, _, _ = symbol.partition(":")
        evidence = {"symbol": symbol, "before": old, "after": new, "exported_from": entry, "baseline": ref}
        return ViolationReport.for_clause(clause, evidence)

    def private(symbol: str) -> bool:  # `_name` and `#name` members are internal by convention
        return symbol.rsplit(":", 1)[-1].rsplit(".", 1)[-1].startswith(("_", "#"))

    reports = [report(s, b, a) for s, (b, a) in diff.changed.items() if not compatible(s) and not private(s)]
    reports += [report(s, b, None) for s, b in diff.removed.items() if not private(s)]
    if not additive_ok:
        reports += [report(s, None, a) for s, a in diff.added.items()]
    return reports
