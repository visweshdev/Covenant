"""api_surface_diff: a Python package's exported names and signatures must not change.

The surface is read from source with the standard-library `ast` module: names in
`__all__` (or the public top-level names of `<package>/__init__.py`), followed
through re-exports inside the package. Classes contribute their public methods
and annotated fields. The plan is to swap this for griffe; the diff format stays.

Clause params:
    package:        import name of the package, e.g. "mylib"
    baseline:       git ref to compare against (the runtime pins it to a sha at start)
    allow_additive: new exports are fine (default true)
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..feedback import ViolationReport
from ..ir import Clause

Reader = Callable[[str], "str | None"]


class ApiSurfaceError(RuntimeError):
    pass


def working_tree_reader(root: Path) -> Reader:
    def read(rel: str) -> str | None:
        path = root / rel
        return path.read_text(encoding="utf-8") if path.is_file() else None

    return read


def git_reader(root: Path, ref: str) -> Reader:
    def read(rel: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"{ref}:{rel}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout if result.returncode == 0 else None

    return read


def _read_module(read: Reader, module: str) -> tuple[ast.Module, bool] | None:
    base = module.replace(".", "/")
    for rel, is_package in ((f"{base}/__init__.py", True), (f"{base}.py", False)):
        source = read(rel)
        if source is not None:
            return ast.parse(source), is_package
    return None


def _resolve_relative(module: str, is_package: bool, level: int, name: str | None) -> str:
    parts = module.split(".")
    if not is_package:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    return ".".join(parts + ([name] if name else []))


def _decorators(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    return "".join(f"@{ast.unparse(d)} " for d in node.decorator_list)


def _function_sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{_decorators(node)}{keyword} {node.name}({ast.unparse(node.args)}){returns}"


def _class_entries(node: ast.ClassDef, qual: str) -> dict[str, str]:
    bases = ", ".join([ast.unparse(b) for b in node.bases] + [ast.unparse(k) for k in node.keywords])
    entries = {qual: f"{_decorators(node)}class {node.name}({bases})"}
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not item.name.startswith("_") or item.name == "__init__":
                entries[f"{qual}.{item.name}"] = _function_sig(item)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if not item.target.id.startswith("_"):
                default = f" = {ast.unparse(item.value)}" if item.value is not None else ""
                entries[f"{qual}.{item.target.id}"] = f"{item.target.id}: {ast.unparse(item.annotation)}{default}"
    return entries


def _exported_names(tree: ast.Module) -> list[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                return [e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            names += [a.asname or a.name for a in node.names if a.name != "*"]
    return list(dict.fromkeys(n for n in names if not n.startswith("_")))


def _resolve(read: Reader, root: str, module: str, name: str, qual: str, depth: int) -> dict[str, str]:
    loaded = _read_module(read, module)
    if loaded is None:
        return {qual: f"<unresolved: module {module} not found>"}
    tree, is_package = loaded
    found: dict[str, str] | None = None
    for node in tree.body:  # later definitions win, as at import time
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            found = {qual: _function_sig(node)}
        elif isinstance(node, ast.ClassDef) and node.name == name:
            found = _class_entries(node, qual)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            found = {qual: f"{name} (variable)"}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            found = {qual: f"{name}: {ast.unparse(node.annotation)}"}
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) != name:
                    continue
                source = (
                    _resolve_relative(module, is_package, node.level, node.module)
                    if node.level
                    else (node.module or "")
                )
                if depth < 5 and (source == root or source.startswith(root + ".")):
                    found = _resolve(read, root, source, alias.name, qual, depth + 1)
                else:
                    found = {qual: f"re-export of {source}.{alias.name}"}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    found = {qual: f"module {alias.name}"}
    return found if found is not None else {qual: "<unresolved>"}


def extract_surface(read: Reader, package: str) -> dict[str, str]:
    loaded = _read_module(read, package)
    if loaded is None:
        raise ApiSurfaceError(f"package {package!r} not found")
    tree, _ = loaded
    surface: dict[str, str] = {}
    for name in _exported_names(tree):
        surface.update(_resolve(read, package, package, name, f"{package}.{name}", 0))
    return surface


@dataclass
class ApiDiff:
    removed: dict[str, str] = field(default_factory=dict)
    added: dict[str, str] = field(default_factory=dict)
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)


def diff_surfaces(before: dict[str, str], after: dict[str, str]) -> ApiDiff:
    return ApiDiff(
        removed={k: v for k, v in before.items() if k not in after},
        added={k: v for k, v in after.items() if k not in before},
        changed={k: (v, after[k]) for k, v in before.items() if k in after and after[k] != v},
    )


def verify(clause: Clause, repo_root: Path, baseline: str | None = None) -> list[ViolationReport]:
    params = clause.params
    package = params["package"]
    ref = baseline or params.get("baseline", "HEAD")
    exported_from = f"{package.replace('.', '/')}/__init__.py"
    before = extract_surface(git_reader(repo_root, ref), package)
    after = extract_surface(working_tree_reader(repo_root), package)
    diff = diff_surfaces(before, after)

    def report(symbol: str, old: str | None, new: str | None) -> ViolationReport:
        evidence = {"symbol": symbol, "before": old, "after": new, "exported_from": exported_from, "baseline": ref}
        return ViolationReport.for_clause(clause, evidence)

    reports = [report(s, b, a) for s, (b, a) in diff.changed.items()]
    reports += [report(s, b, None) for s, b in diff.removed.items()]
    if not params.get("allow_additive", True):
        reports += [report(s, None, a) for s, a in diff.added.items()]
    return reports
