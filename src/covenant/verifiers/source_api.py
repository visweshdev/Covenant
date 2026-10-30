"""source_api_diff: the public API of a Go, Rust or Java project must not change.

Reads source files directly (no compiler needed), so it works on any machine:

    go    exported (capitalized) funcs, methods, types, struct fields, interface methods,
          consts and vars; skips _test.go, vendor/ and internal/ packages
    rust  `pub` fns, structs and their pub fields, enums and variants, traits and their
          methods, impl methods, consts, statics and type aliases (`pub(crate)` is private)
    java  public and protected classes, interfaces, enums, records and their members;
          overloads are tracked separately, so adding one is additive

Signatures are compared with whitespace normalized and parameter names removed, since
renaming a parameter doesn't break callers in these languages. Nested inline modules and
macros are not followed: treat this as a careful approximation of the compiler's view.

Clause params:
    language:       go | rust | java (default: detected)
    roots:          folders to scan (default: per language)
    baseline:       git ref (the runtime pins it to a sha at start)
    allow_additive: new public items are fine (default true)
"""

from __future__ import annotations

import io
import re
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

from ..feedback import ViolationReport
from ..ir import Clause
from .api_surface import diff_surfaces

Files = dict[str, str]  # repo-relative path -> source
EXTS = {"go": ".go", "rust": ".rs", "java": ".java"}
DEFAULT_ROOTS = {"go": ["."], "rust": ["src"], "java": ["src/main/java", "src"]}


class SourceApiError(RuntimeError):
    pass


def detect_language(repo: Path) -> str | None:
    if (repo / "go.mod").is_file():
        return "go"
    if (repo / "Cargo.toml").is_file():
        return "rust"
    if any((repo / f).is_file() for f in ("pom.xml", "build.gradle", "build.gradle.kts")):
        return "java"
    return None


# ----- reading sources -----------------------------------------------------------------


def _wanted(rel: str, language: str, roots: list[str]) -> bool:
    if not rel.endswith(EXTS[language]):
        return False
    parts = rel.split("/")
    if language == "go" and (rel.endswith("_test.go") or "vendor" in parts or "internal" in parts or "testdata" in parts):
        return False
    if language == "rust" and ("tests" in parts or "benches" in parts or "examples" in parts):
        return False
    if language == "java" and "test" in parts:
        return False
    return any(r in (".", "") or rel == r or rel.startswith(r.rstrip("/") + "/") for r in roots)


def working_tree_files(repo: Path, language: str, roots: list[str]) -> Files:
    out: Files = {}
    for path in repo.rglob(f"*{EXTS[language]}"):
        rel = path.relative_to(repo).as_posix()
        if ".git/" in rel or rel.startswith(("target/", "node_modules/", "build/")):
            continue
        if _wanted(rel, language, roots):
            out[rel] = path.read_text(encoding="utf-8", errors="ignore")
    return out


def baseline_files(repo: Path, ref: str, language: str, roots: list[str]) -> Files:
    archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", ref], capture_output=True)
    if archive.returncode != 0:
        raise SourceApiError(f"cannot read baseline {ref!r}: {archive.stderr.decode(errors='replace').strip()}")
    out: Files = {}
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        for member in tar.getmembers():
            if member.isfile() and _wanted(member.name, language, roots):
                f = tar.extractfile(member)
                if f is not None:
                    out[member.name] = f.read().decode("utf-8", errors="ignore")
    return out


# ----- lexical helpers ------------------------------------------------------------------


def strip_comments(src: str, language: str) -> str:
    """Remove comments, keep string literals (their contents are blanked to keep braces honest)."""
    out, i, n = [], 0, len(src)
    while i < n:
        c, nxt = src[i], src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif c == '"' or (c == "`" and language == "go"):
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" and c == '"' else 1
            out.append(c + c)
            i = j + 1
        elif c == "'" and language != "rust":
            j = i + 1
            while j < n and src[j] != "'":
                j += 2 if src[j] == "\\" else 1
            out.append("''")
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def norm(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s*([(),<>\[\]{}:;*&=])\s*", r"\1", text).replace(",", ", ")


def split_top(text: str, sep: str = ",") -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def blocks(src: str) -> list[tuple[str, str | None]]:
    """Split source into top-level (header, body) pairs: header up to `{` or `;`, body inside braces."""
    items, i, n = [], 0, len(src)
    start = 0
    paren = 0
    while i < n:
        c = src[i]
        if c in "([":
            paren += 1
        elif c in ")]":
            paren -= 1
        elif c == ";" and paren == 0:
            items.append((src[start:i].strip(), None))
            start = i + 1
        elif c == "{" and paren == 0:
            depth, j = 1, i + 1
            while j < n and depth:
                depth += {"{": 1, "}": -1}.get(src[j], 0)
                j += 1
            items.append((src[start:i].strip(), src[i + 1 : j - 1]))
            i = j
            start = j
            continue
        elif c == "\n" and paren == 0 and not src[start:i].strip():
            start = i + 1
        i += 1
    if src[start:].strip():
        items.append((src[start:].strip(), None))
    return items


def _paren_group(text: str, open_at: int) -> tuple[str, int]:
    depth, j = 0, open_at
    while j < len(text):
        depth += {"(": 1, ")": -1}.get(text[j], 0)
        if depth == 0:
            return text[open_at + 1 : j], j + 1
        j += 1
    return text[open_at + 1 :], len(text)


# ----- Go -------------------------------------------------------------------------------


def _go_param_types(params: str) -> list[str]:
    parts = split_top(params)
    named = any(len(p.split(None, 1)) == 2 and not p.startswith(("...", "*", "[", "func", "map", "chan")) for p in parts)
    if not named:
        return [norm(p) for p in parts]
    types: list[str] = []
    pending = 0
    for p in parts:
        bits = p.split(None, 1)
        if len(bits) == 1:
            pending += 1
        else:
            types += [norm(bits[1])] * (pending + 1)
            pending = 0
    return types


def _go_sig(rest: str) -> str:
    """'(a, b int) (int, error)' -> '(int, int) (int, error)'."""
    rest = rest.strip()
    if not rest.startswith("("):
        return norm(rest)
    params, end = _paren_group(rest, 0)
    results = rest[end:].strip()
    if results.startswith("("):
        inner, _ = _paren_group(results, 0)
        results = "(" + ", ".join(_go_param_types(inner)) + ")"
    return f"({', '.join(_go_param_types(params))}) {norm(results)}".strip()


def go_surface(files: Files) -> dict[str, str]:
    out: dict[str, str] = {}
    exported = lambda name: bool(name) and name[0].isupper()  # noqa: E731
    for rel, raw in sorted(files.items()):
        src = strip_comments(raw, "go")
        pkg_dir = rel.rsplit("/", 1)[0] if "/" in rel else "."
        prefix = f"{pkg_dir}:"
        for line_block in re.split(r"\n(?=[^\s})])", src):  # a new top-level declaration starts at column 0
            text = line_block.strip()
            m = re.match(r"func\s+\((\w+\s+)?\*?(\w+)(?:\[[^\]]*\])?\)\s+(\w+)\s*(?:\[[^\]]*\])?(\(.*)$", text, re.S)
            if m:
                recv, name, rest = m.group(2), m.group(3), m.group(4).split("{", 1)[0]
                if exported(recv) and exported(name):
                    out[f"{prefix}{recv}.{name}"] = "method " + _go_sig(rest)
                continue
            m = re.match(r"func\s+(\w+)\s*(\[[^\]]*\])?(\(.*)$", text, re.S)
            if m:
                if exported(m.group(1)):
                    out[f"{prefix}{m.group(1)}"] = f"func{norm(m.group(2) or '')} " + _go_sig(m.group(3).split("{", 1)[0])
                continue
            m = re.match(r"type\s+(\w+)(\[[^\]]*\])?\s+(struct|interface)\s*\{(.*)\}\s*$", text, re.S)
            if m:
                name, kind, body = m.group(1), m.group(3), m.group(4)
                if not exported(name):
                    continue
                out[f"{prefix}{name}"] = f"type {kind}{norm(m.group(2) or '')}"
                for member in body.split("\n"):
                    member = member.strip().rstrip(";")
                    if not member:
                        continue
                    if kind == "interface":
                        mm = re.match(r"(\w+)\s*(\(.*)$", member)
                        if mm and exported(mm.group(1)):
                            out[f"{prefix}{name}.{mm.group(1)}"] = _go_sig(mm.group(2))
                        elif re.fullmatch(r"[\w.]+", member):
                            out[f"{prefix}{name}.<embeds {member}>"] = "embedded"
                    elif re.fullmatch(r"\*?[\w.]+", member):
                        out[f"{prefix}{name}.<embeds {member}>"] = "embedded"
                    else:
                        fm = re.match(r"^(\w+(?:\s*,\s*\w+)*)\s+(.+?)\s*(`.*`)?$", member)
                        if fm:
                            for f in (x.strip() for x in fm.group(1).split(",")):
                                if exported(f):
                                    out[f"{prefix}{name}.{f}"] = norm(fm.group(2))
                continue
            m = re.match(r"type\s+(\w+)(\[[^\]]*\])?\s*(=?)\s*(.+)$", text, re.S)
            if m and exported(m.group(1)) and not text.startswith("type ("):
                out[f"{prefix}{m.group(1)}"] = f"type{norm(m.group(2) or '')} {'= ' if m.group(3) else ''}{norm(m.group(4))}"
                continue
            m = re.match(r"(const|var|type)\s*\((.*)\)\s*$", text, re.S)
            if m:
                kind = m.group(1)
                for entry in m.group(2).split("\n"):
                    e = entry.strip()
                    em = re.match(r"(\w+)\b\s*(.*)$", e)
                    if em and exported(em.group(1)):
                        detail = em.group(2).split("=", 1)[0].strip() if kind != "type" else em.group(2)
                        out[f"{prefix}{em.group(1)}"] = f"{kind} {norm(detail)}".strip()
                continue
            m = re.match(r"(const|var)\s+(\w+)\b\s*(.*)$", text, re.S)
            if m and exported(m.group(2)):
                out[f"{prefix}{m.group(2)}"] = f"{m.group(1)} {norm(m.group(3).split('=', 1)[0])}".strip()
    return out


# ----- Rust -----------------------------------------------------------------------------


def _rust_params(params: str) -> str:
    types = []
    for p in split_top(params):
        if re.fullmatch(r"&?\s*(mut\s+)?(?:'\w+\s+)?(mut\s+)?self", p):
            types.append(norm(p))
        elif ":" in p:
            types.append(norm(p.split(":", 1)[1]))
        else:
            types.append(norm(p))
    return ", ".join(types)


def _rust_fn(header: str) -> tuple[str, str] | None:
    m = re.search(r"\bfn\s+(\w+)\s*(<.*?>)?\s*\(", header, re.S)
    if not m:
        return None
    params, end = _paren_group(header, m.end() - 1)
    rest = norm(header[end:])
    quals = norm(header[: m.start()].replace("pub", "", 1))
    return m.group(1), f"{quals + ' ' if quals else ''}fn{norm(m.group(2) or '')}({_rust_params(params)}){(' ' + rest) if rest else ''}"


def _rust_module(rel: str) -> str:
    parts = rel.split("/")
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts[-1] in ("lib.rs", "main.rs", "mod.rs"):
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return "::".join(["crate", *parts])


def rust_surface(files: Files) -> dict[str, str]:
    out: dict[str, str] = {}
    pub = re.compile(r"^(?:#\[[^\]]*\]\s*)*pub\s+(?!\()")
    for rel, raw in sorted(files.items()):
        module = _rust_module(rel)
        for header, body in blocks(strip_comments(raw, "rust")):
            h = re.sub(r"^(?:#\[[^\]]*\]\s*)+", "", header)
            if h.startswith("impl"):
                m = re.match(r"impl\s*(<.*?>)?\s*(?:([\w:<>, ]+?)\s+for\s+)?([\w:]+)", h, re.S)
                if not m or m.group(2) or body is None:
                    continue  # trait impls follow the trait; skip
                target = m.group(3).split("::")[-1]
                for mh, _ in blocks(body):
                    if pub.match(mh.strip()):
                        fn = _rust_fn(mh)
                        if fn:
                            out[f"{module}::{target}::{fn[0]}"] = fn[1]
                continue
            if not pub.match(h):
                continue
            fn = _rust_fn(h)
            if fn and re.match(r"^(?:#\[[^\]]*\]\s*)*pub\s+(?:const\s+|async\s+|unsafe\s+|extern\s+\S+\s+)*fn\b", h):
                out[f"{module}::{fn[0]}"] = fn[1]
                continue
            m = re.match(r"pub\s+(struct|enum|trait|union)\s+(\w+)\s*(.*)$", h, re.S)
            if m:
                kind, name, rest = m.groups()
                key = f"{module}::{name}"
                out[key] = f"{kind} {norm(rest)}".strip()
                if body is None:
                    continue
                if kind in ("struct", "union"):
                    for field in split_top(body):
                        fm = re.match(r"pub\s+(?!\()(\w+)\s*:\s*(.+)$", field.strip(), re.S)
                        if fm:
                            out[f"{key}::{fm.group(1)}"] = norm(fm.group(2))
                elif kind == "enum":
                    for variant in split_top(body):
                        vm = re.match(r"(?:#\[[^\]]*\]\s*)*(\w+)\s*(.*)$", variant.strip(), re.S)
                        if vm:
                            out[f"{key}::{vm.group(1)}"] = norm(vm.group(2)) or "variant"
                elif kind == "trait":
                    for th, _ in blocks(body):
                        fn = _rust_fn(th)
                        if fn:
                            out[f"{key}::{fn[0]}"] = fn[1]
                continue
            m = re.match(r"pub\s+(const|static|type)\s+(?:mut\s+)?(\w+)\s*(.*)$", h, re.S)
            if m:
                detail = m.group(3).split("=", 1)[0] if m.group(1) != "type" else m.group(3)
                out[f"{module}::{m.group(2)}"] = f"{m.group(1)} {norm(detail)}".strip()
                continue
            m = re.match(r"pub\s+(?:use)\s+(.+)$", h, re.S)
            if m:
                out[f"{module}::<use {norm(m.group(1))}>"] = "re-export"
    return out


# ----- Java -----------------------------------------------------------------------------

_JAVA_TYPE = re.compile(r"\b(class|interface|enum|record|@interface)\s+(\w+)")
_JAVA_MODS = {"public", "protected", "private", "static", "final", "abstract", "default", "synchronized", "native", "strictfp", "transient", "volatile", "sealed", "non-sealed"}


def _java_params(params: str) -> str:
    types = []
    for p in split_top(params):
        p = re.sub(r"@\w+(\([^)]*\))?\s*", "", p).replace("final ", "").strip()
        bits = p.rsplit(None, 1)
        types.append(norm(bits[0] if len(bits) == 2 else p))
    return ", ".join(types)


def java_surface(files: Files) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel, raw in sorted(files.items()):
        src = strip_comments(raw, "java")
        pm = re.search(r"\bpackage\s+([\w.]+)\s*;", src)
        package = pm.group(1) if pm else ""
        for header, body in blocks(src):
            h = re.sub(r"@\w+(\([^)]*\))?\s*", "", header)
            tm = _JAVA_TYPE.search(h)
            if not tm or body is None:
                continue
            mods = set(h[: tm.start()].split())
            if "public" not in mods:
                continue
            kind, name = tm.groups()
            key = f"{package}.{name}" if package else name
            out[key] = f"{' '.join(sorted(mods - {'public'}))} {kind} {norm(h[tm.end():])}".strip()
            in_interface = kind in ("interface", "@interface")
            members = body
            if kind == "enum":  # constants come first, up to the first ';'
                constants, _, members = body.partition(";")
                for const in split_top(constants):
                    cm = re.match(r"(\w+)", const.strip())
                    if cm:
                        out[f"{key}.{cm.group(1)}"] = "enum constant"
            for mh, _ in blocks(members):
                m_h = re.sub(r"@\w+(\([^)]*\))?\s*", "", mh).strip()
                if not m_h or _JAVA_TYPE.search(m_h.split("(", 1)[0]) or m_h.startswith(("static {", "{")) or m_h == "static":
                    continue
                words = m_h.split("(", 1)[0].split()
                visible = "public" in words or "protected" in words or (in_interface and "private" not in words)
                if not visible:
                    continue
                if "(" in m_h:
                    before, after = m_h.split("(", 1)
                    names = before.split()
                    member = names[-1]
                    params, end = _paren_group("(" + after, 0)
                    tail = norm(("(" + after)[end:])
                    ret = " ".join(w for w in names[:-1] if w not in _JAVA_MODS) or "<init>"
                    mods_kept = " ".join(w for w in names[:-1] if w in ("static", "abstract", "final", "default", "protected"))
                    plist = _java_params(params)
                    out[f"{key}.{member}({plist})"] = f"{mods_kept + ' ' if mods_kept else ''}{norm(ret)}{(' ' + tail) if tail else ''}"
                else:
                    decl = m_h.split("=", 1)[0].split()
                    if len(decl) >= 2:
                        field = decl[-1]
                        ftype = " ".join(w for w in decl[:-1] if w not in _JAVA_MODS)
                        mods_kept = " ".join(w for w in decl[:-1] if w in ("static", "final", "protected"))
                        out[f"{key}.{field}"] = f"{mods_kept + ' ' if mods_kept else ''}{norm(ftype)}"
    return out


SURFACES: dict[str, Callable[[Files], dict[str, str]]] = {"go": go_surface, "rust": rust_surface, "java": java_surface}


def verify(clause: Clause, repo_root: Path, baseline: str | None = None) -> list[ViolationReport]:
    params = clause.params
    language = params.get("language") or detect_language(repo_root)
    if language not in SURFACES:
        raise SourceApiError(f"unsupported or undetected language {language!r} (go, rust, java)")
    roots = list(params.get("roots") or DEFAULT_ROOTS[language])
    ref = baseline or params.get("baseline", "HEAD")
    surface = SURFACES[language]
    before = surface(baseline_files(repo_root, ref, language, roots))
    after = surface(working_tree_files(repo_root, language, roots))
    diff = diff_surfaces(before, after)

    def report(symbol: str, old: str | None, new: str | None) -> ViolationReport:
        evidence = {"symbol": symbol, "before": old, "after": new, "language": language, "baseline": ref}
        return ViolationReport.for_clause(clause, evidence)

    reports = [report(s, b, a) for s, (b, a) in diff.changed.items()]
    reports += [report(s, b, None) for s, b in diff.removed.items()]
    if not params.get("allow_additive", True):
        reports += [report(s, None, a) for s, a in diff.added.items()]
    return reports
