"""api_surface_diff: a Python package's exported names and signatures must not change.

The surface is read from source with the standard-library `ast` module: names in
`__all__` (or the public top-level names of `<package>/__init__.py`), followed
through re-exports inside the package. Classes contribute their public methods
and annotated fields. The plan is to swap this for griffe; the diff format stays.

Clause params:
    package:        import name of the package, e.g. "mylib"
    root:           folder that contains the package, e.g. "src" (default: repo root)
    extra_modules:  modules that count as public although not exported, e.g. ["mylib._compat"]
    baseline:       git ref to compare against (the runtime pins it to a sha at start)
    allow_additive: new exports are fine (default true)
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..feedback import ViolationReport
from ..ir import Clause

Reader = Callable[[str], "str | None"]


class ApiSurfaceError(RuntimeError):
    pass


def _exact_file(path: Path) -> bool:
    """is_file() with the exact case: on Windows and macOS `Request.py` would match `request.py`,
    which made `werkzeug.wrappers.Request` (a class) look like a module."""
    if not path.is_file():
        return False
    try:
        return path.name in os.listdir(path.parent)
    except OSError:
        return False


def working_tree_reader(root: Path) -> Reader:
    cache: dict[str, str | None] = {}  # one extraction reads a module once per exported name

    def read(rel: str) -> str | None:
        if rel not in cache:
            path = root / rel
            cache[rel] = path.read_text(encoding="utf-8") if _exact_file(path) else None
        return cache[rel]

    return read


# Files at a commit never change, so reads are cached across checks by (repo, sha, path).
_AT_COMMIT: dict[tuple[str, str, str], str | None] = {}
_TREES: dict[tuple[str, str], frozenset[str]] = {}


def _tree(root: Path, sha: str) -> frozenset[str] | None:
    """Every path in a commit, listed once, so looking for modules that aren't there is free."""
    key = (str(root), sha)
    if key not in _TREES:
        r = subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", "-z", sha],
                           capture_output=True)
        if r.returncode != 0:
            return None
        if len(_TREES) > 64:
            _TREES.clear()
        _TREES[key] = frozenset(r.stdout.decode("utf-8", "replace").split("\0"))
    return _TREES[key]


def git_reader(root: Path, ref: str) -> Reader:
    resolved = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                              capture_output=True, text=True)
    sha = resolved.stdout.strip() if resolved.returncode == 0 else None
    local: dict[str, str | None] = {}
    tree = _tree(root, sha) if sha else None

    def read(rel: str) -> str | None:
        key = (str(root), sha or ref, rel)
        if sha and key in _AT_COMMIT:
            return _AT_COMMIT[key]
        if rel in local:
            return local[rel]
        if tree is not None and rel not in tree and not _exact_file(root / rel):
            local[rel] = None  # not in the commit, nor an ignored file on disk: no git calls needed
            return None
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"{sha or ref}:{rel}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode == 0:
            if sha:
                if len(_AT_COMMIT) > 20_000:
                    _AT_COMMIT.clear()
                _AT_COMMIT[key] = result.stdout
            local[rel] = result.stdout
            return result.stdout
        # A file git ignores (a generated _version.py, say) is never in a commit; read it from
        # disk so both sides agree, instead of reporting its names as changed. Not cached across
        # checks: the file on disk can change.
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", rel], capture_output=True)
        path = root / rel
        local[rel] = path.read_text(encoding="utf-8") if ignored.returncode == 0 and _exact_file(path) else None
        return local[rel]

    return read


_PARSED: dict[str, ast.Module] = {}


def _parse(source: str) -> ast.Module:
    tree = _PARSED.get(source)
    if tree is None:
        if len(_PARSED) > 2_000:
            _PARSED.clear()
        tree = _PARSED[source] = ast.parse(source)
    return tree


def _read_module(read: Reader, module: str) -> tuple[ast.Module, bool] | None:
    base = module.replace(".", "/")
    for rel, is_package in ((f"{base}/__init__.py", True), (f"{base}.py", False)):
        source = read(rel)
        if source is not None:
            return _parse(source), is_package
    return None


def _resolve_relative(module: str, is_package: bool, level: int, name: str | None) -> str:
    parts = module.split(".")
    if not is_package:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    return ".".join(parts + ([name] if name else []))


# Decorators that only inform type checkers; adding or removing one changes nothing for callers.
TYPING_ONLY_DECORATORS = {"override", "final", "type_check_only"}


def _decorators(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    return "".join(f"@{ast.unparse(d)} " for d in node.decorator_list
                   if ast.unparse(d).rsplit(".", 1)[-1] not in TYPING_ONLY_DECORATORS)


def import_aliases(tree: ast.Module, package: str) -> dict[str, str]:
    """What each imported name means, so `date` and `dt_date` (both `datetime.date`) compare equal.

    Names from outside the package become their dotted path; names from inside it become the
    bare name, so moving a class between the package's modules isn't a change."""
    aliases: dict[str, str] = {}

    def top_level(body):  # imports under `if TYPE_CHECKING:` or `try:` count too, but not in functions
        for node in body:
            yield node
            if isinstance(node, (ast.If, ast.Try, ast.With)):
                for part in ("body", "orelse", "finalbody"):
                    yield from top_level(getattr(node, part, []))
                for handler in getattr(node, "handlers", []):
                    yield from top_level(handler.body)

    for node in top_level(tree.body):
        if isinstance(node, ast.ImportFrom):
            inside = node.level > 0 or (node.module or "").split(".")[0] == package
            for a in node.names:
                if a.name != "*":
                    aliases[a.asname or a.name] = a.name if inside else f"{node.module}.{a.name}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
    return aliases


class _Canonical(ast.NodeTransformer):
    def __init__(self, aliases: dict[str, str]):
        self.aliases = aliases

    def visit_Name(self, node: ast.Name) -> ast.AST:
        target = self.aliases.get(node.id)
        if target is None or target == node.id:
            return node
        return ast.parse(target, mode="eval").body

    def visit_Constant(self, node: ast.Constant) -> ast.AST:  # "Foo" forward references
        if isinstance(node.value, str) and node.value.isidentifier() and node.value in self.aliases:
            return ast.Constant(self.aliases[node.value])
        return node


def _canon(node: ast.AST | None, aliases: dict[str, str]) -> ast.AST | None:
    if node is None or not aliases:
        return node
    import copy
    return _Canonical(aliases).visit(copy.deepcopy(node))  # parsed trees are cached and shared


def _function_sig(node: ast.FunctionDef | ast.AsyncFunctionDef, aliases: dict[str, str] | None = None) -> str:
    aliases = aliases or {}
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(_canon(node.returns, aliases))}" if node.returns else ""
    return f"{_decorators(node)}{keyword} {node.name}({ast.unparse(_canon(node.args, aliases))}){returns}"


def _class_entries(node: ast.ClassDef, qual: str, aliases: dict[str, str] | None = None) -> dict[str, str]:
    aliases = aliases or {}
    bases = ", ".join([ast.unparse(_canon(b, aliases)) for b in node.bases] + [ast.unparse(k) for k in node.keywords])
    entries = {qual: f"{_decorators(node)}class {node.name}({bases})"}
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not item.name.startswith("_") or item.name == "__init__":
                entries[f"{qual}.{item.name}"] = _function_sig(item, aliases)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if not item.target.id.startswith("_"):
                default = f" = {ast.unparse(item.value)}" if item.value is not None else ""
                entries[f"{qual}.{item.target.id}"] = f"{item.target.id}: {ast.unparse(_canon(item.annotation, aliases))}{default}"
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
    aliases = import_aliases(tree, root)
    found: dict[str, str] | None = None
    for node in tree.body:  # later definitions win, as at import time
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            found = {qual: _function_sig(node, aliases)}
        elif isinstance(node, ast.ClassDef) and node.name == name:
            found = _class_entries(node, qual, aliases)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            found = {qual: f"{name} (variable)"}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            found = {qual: f"{name}: {ast.unparse(_canon(node.annotation, aliases))}"}
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) != name:
                    continue
                source = (
                    _resolve_relative(module, is_package, node.level, node.module)
                    if node.level
                    else (node.module or "")
                )
                inside = source == root or source.startswith(root + ".")
                if inside and _read_module(read, f"{source}.{alias.name}") is not None:
                    found = {qual: f"module {source}.{alias.name}"}  # `from . import filters`
                elif depth < 5 and inside:
                    found = _resolve(read, root, source, alias.name, qual, depth + 1)
                else:
                    found = {qual: f"re-export of {source}.{alias.name}"}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    found = {qual: f"module {alias.name}"}
    if found is None and is_package and _read_module(read, f"{module}.{name}") is not None:
        found = {qual: f"module {module}.{name}"}  # a submodule loaded lazily (module __getattr__)
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


def _parse_sig(sig: str) -> tuple[str, ast.FunctionDef | ast.AsyncFunctionDef] | None:
    """Split "@deco def f(a, b=1) -> T" into its decorators and a parsed function node."""
    m = re.match(r"^((?:@\S+\s+)*)((?:async\s+)?def\s.*)$", sig, re.S)
    if not m:
        return None
    try:
        node = ast.parse(m.group(2) + ":\n    pass").body[0]
    except SyntaxError:
        return None
    return (m.group(1), node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _src(node: ast.AST | None) -> str | None:
    return None if node is None else ast.unparse(node)


def _members(node: ast.AST) -> set[str]:
    """The types of an annotation: `A | None`, `Optional[A]` and `Union[A, B]` are unions; `"A"` is A."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):  # a string (forward) annotation
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return {node.value}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _members(node.left) | _members(node.right)
    if isinstance(node, ast.Subscript):
        head = ast.unparse(node.value).rsplit(".", 1)[-1]
        if head in ("ClassVar", "Final", "Annotated", "Required", "NotRequired", "ReadOnly"):
            inner = node.slice.elts[0] if isinstance(node.slice, ast.Tuple) else node.slice
            return _members(inner)
        if head == "Optional":
            return _members(node.slice) | {"None"}
        if head == "Union":
            elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            return set().union(*(_members(e) for e in elts))
    return {ast.unparse(node)}


def _widened(old: ast.AST | None, new: ast.AST | None) -> bool:
    """A parameter's annotation still accepts everything it did (unannotated means anything)."""
    return old is None or new is None or _members(old) <= _members(new)


def _class_compatible(before: str, after: str) -> bool:
    """`class A(Base)` -> `class A(Base, Generic[T])`: new bases are fine, old ones must stay, decorators unchanged."""
    def split(sig: str):
        m = re.match(r"^((?:@\S+\s+)*)class\s+(\w+)\((.*)\)$", sig, re.S)
        if not m:
            return None
        try:
            call = ast.parse(f"f({m.group(3)})", mode="eval").body
        except SyntaxError:
            return None
        return m.group(1), m.group(2), [ast.unparse(b) for b in call.args], {k.arg: ast.unparse(k.value) for k in call.keywords}

    b, a = split(before), split(after)
    if b is None or a is None:
        return False
    return b[0] == a[0] and b[1] == a[1] and set(b[2]) <= set(a[2]) and all(a[3].get(k) == v for k, v in b[3].items())


def _field_compatible(before: str, after: str) -> bool:
    """`x: A = 1` -> `x: A | None = 1`: the same default, and a type that accepts what it did."""
    def split(sig: str):
        try:
            node = ast.parse(sig).body[0]
        except SyntaxError:
            return None
        return node if isinstance(node, ast.AnnAssign) else None

    b, a = split(before), split(after)
    return (b is not None and a is not None and _src(b.value) == _src(a.value)
            and _widened(b.annotation, a.annotation))


def compatible(before: str, after: str) -> bool:
    """A changed surface entry that every existing caller can still use."""
    if before.lstrip("@").startswith(("def ", "async def ")) or re.match(r"^(?:@\S+\s+)*(?:async\s+)?def\s", before):
        return signatures_compatible(before, after)
    if re.match(r"^(?:@\S+\s+)*class\s", before):
        return _class_compatible(before, after)
    if ": " in before and not before.startswith(("re-export", "module", "<")):
        return _field_compatible(before, after)
    return False


def signatures_compatible(before: str, after: str) -> bool:
    """True when every call that worked against `before` still works the same against `after`.

    Old parameters keep their name, position, kind and default, and their annotation accepts
    what it did (`A` -> `A | None` is fine); new parameters are optional; *args/**kwargs are
    kept; decorators are unchanged and the return annotation can only narrow.
    """
    parsed = _parse_sig(before), _parse_sig(after)
    if None in parsed:
        return False
    (b_deco, b), (a_deco, a) = parsed  # type: ignore[misc]
    if b_deco != a_deco or type(b) is not type(a) or not _widened(a.returns, b.returns):
        return False
    ba, aa = b.args, a.args
    b_pos, a_pos = ba.posonlyargs + ba.args, aa.posonlyargs + aa.args

    def defaults(params: list[ast.arg], values: list[ast.expr]) -> dict[str, str]:
        return {p.arg: ast.unparse(v) for p, v in zip(params[len(params) - len(values):], values)}

    b_def, a_def = defaults(b_pos, ba.defaults), defaults(a_pos, aa.defaults)
    if len(a_pos) < len(b_pos) or len(aa.posonlyargs) > len(ba.posonlyargs):
        return False
    for old, new in zip(b_pos, a_pos):
        if old.arg != new.arg or not _widened(old.annotation, new.annotation) or b_def.get(old.arg) != a_def.get(old.arg):
            return False
    extra_pos = a_pos[len(b_pos):]
    if ba.vararg and extra_pos:  # new positionals would swallow arguments that went to *args
        return False
    if any(p.arg not in a_def for p in extra_pos):
        return False

    b_kw = {p.arg: (p, d) for p, d in zip(ba.kwonlyargs, ba.kw_defaults)}
    a_kw = {p.arg: (p, d) for p, d in zip(aa.kwonlyargs, aa.kw_defaults)}
    for name, (p, d) in b_kw.items():
        if name not in a_kw:
            return False
        q, e = a_kw[name]
        if not _widened(p.annotation, q.annotation) or _src(d) != _src(e):
            return False
    if any(d is None for name, (_, d) in a_kw.items() if name not in b_kw):  # new required keyword
        return False
    if (ba.vararg and not aa.vararg) or (ba.kwarg and not aa.kwarg):
        return False
    return True


def diff_surfaces(before: dict[str, str], after: dict[str, str]) -> ApiDiff:
    return ApiDiff(
        removed={k: v for k, v in before.items() if k not in after},
        added={k: v for k, v in after.items() if k not in before},
        changed={k: (v, after[k]) for k, v in before.items() if k in after and after[k] != v},
    )


def _prefixed(read: Reader, root: str) -> Reader:
    root = root.strip("/")
    return (lambda rel: read(f"{root}/{rel}")) if root else read


def module_surface(read: Reader, module: str) -> dict[str, str]:
    """Public top-level names of one module, for modules treated as public without being exported."""
    loaded = _read_module(read, module)
    if loaded is None:
        raise ApiSurfaceError(f"module {module!r} not found")
    tree, _ = loaded
    surface: dict[str, str] = {}
    root = module.split(".")[0]
    for name in _exported_names(tree):
        surface.update(_resolve(read, root, module, name, f"{module}.{name}", 0))
    return surface


def full_surface(read: Reader, package: str, extra_modules: list[str]) -> dict[str, str]:
    surface = extract_surface(read, package)
    for module in extra_modules:
        surface.update(module_surface(read, module))
    return surface


def verify(clause: Clause, repo_root: Path, baseline: str | None = None) -> list[ViolationReport]:
    params = clause.params
    package = params["package"]
    source_root = params.get("root", "")
    extra = list(params.get("extra_modules", []))
    ref = baseline or params.get("baseline", "HEAD")
    exported_from = "/".join(p for p in (source_root.strip("/"), f"{package.replace('.', '/')}/__init__.py") if p)
    before = full_surface(_prefixed(git_reader(repo_root, ref), source_root), package, extra)
    after = full_surface(_prefixed(working_tree_reader(repo_root), source_root), package, extra)
    diff = diff_surfaces(before, after)

    def report(symbol: str, old: str | None, new: str | None) -> ViolationReport:
        evidence = {"symbol": symbol, "before": old, "after": new, "exported_from": exported_from, "baseline": ref}
        return ViolationReport.for_clause(clause, evidence)

    additive_ok = params.get("allow_additive", True)
    reports = [
        report(s, b, a)
        for s, (b, a) in diff.changed.items()
        if not (additive_ok and compatible(b, a))
    ]
    reports += [report(s, b, None) for s, b in diff.removed.items()]
    if not additive_ok:
        reports += [report(s, None, a) for s, a in diff.added.items()]
    return reports
