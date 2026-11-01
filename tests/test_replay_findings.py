"""False alarms and misses found by replaying real repositories' history (bench/replay)."""

import json

import pytest

from covenant.compiler.bind import suppression_kinds
from covenant.compiler.ground import find_test_commands
from covenant.ir import Clause, ClauseKind
from covenant.verifiers import dependency_diff
from covenant.verifiers import diff_guard as dg
from covenant.verifiers.api_surface import compatible
from covenant.verifiers.ts_api_surface import detect_entries

from conftest import git


def write(repo, rel, text):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def clause(checker, **params):
    return Clause(id="R1", kind=ClauseKind.SEMANTIC_INVARIANT, source_span="rule", checker=checker,
                  params={"baseline": "HEAD", **params})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q")
    return r


def commit(repo):
    git(repo, "add", "-A")
    assert git(repo, "commit", "-q", "-m", "base").returncode == 0


# click: `pip install tox` from CI was picked as the test command.
def test_install_steps_are_not_test_commands(tmp_path):
    write(tmp_path, ".github/workflows/tests.yaml",
          "jobs:\n  t:\n    steps:\n      - run: pip install tox\n      - run: python -m pip install -U nox\n"
          "      - run: tox run -e py\n")
    cmds = [c for c, _ in find_test_commands(tmp_path)]
    assert "pip install tox" not in cmds and "python -m pip install -U nox" not in cmds
    assert cmds[0] == "tox run -e py"


# ky: package.json points at distribution/, built from source/ (tsconfig outDir/rootDir).
def test_ts_entry_maps_build_output_back_to_source(tmp_path):
    write(tmp_path, "package.json", json.dumps({"types": "./distribution/index.d.ts", "main": "./distribution/index.js"}))
    write(tmp_path, "tsconfig.dist.json", json.dumps({"compilerOptions": {"rootDir": "./source", "outDir": "./distribution"}}))
    write(tmp_path, "source/index.ts", "export const a = 1;\n")
    assert detect_entries(tmp_path) == ["source/index.ts"]


def test_ts_entry_from_exports_and_conventional_dirs(tmp_path):
    write(tmp_path, "package.json", json.dumps({"exports": {".": {"types": "./dist/types/index.d.ts", "import": "./dist/esm/index.js"}}}))
    write(tmp_path, "src/index.ts", "export const a = 1;\n")
    assert detect_entries(tmp_path) == ["src/index.ts"]


def test_ts_entry_uses_hand_written_types(tmp_path):
    write(tmp_path, "package.json", json.dumps({"types": "./index.d.ts", "main": "./index.js"}))
    write(tmp_path, "index.d.ts", "export declare function f(): void;\n")
    assert detect_entries(tmp_path) == ["index.d.ts"]


# ky: "don't silence type errors" flagged eslint-disable, and @ts-expect-error in type tests.
@pytest.mark.parametrize("span, kinds", [
    ("don't silence type errors", ["type"]),
    ("no type: ignore", ["type"]),
    ("don't disable lint rules", ["lint"]),
    ("don't suppress warnings", None),
    ("don't silence errors", None),
])
def test_suppression_kinds_follow_the_wording(span, kinds):
    assert suppression_kinds(span) == kinds


def test_type_only_suppressions_and_type_tests(repo):
    write(repo, "src/a.ts", "export const a = 1;\n")
    write(repo, "test-d/a.test-d.ts", "import {a} from '../src/a';\n")
    commit(repo)
    write(repo, "src/a.ts", "export const a = 1;\n// eslint-disable-next-line no-await-in-loop\n// @ts-ignore\nexport const b = 2;\n")
    write(repo, "test-d/a.test-d.ts", "import {a} from '../src/a';\n// @ts-expect-error wrong type\na('x');\n")
    found = dg.no_suppressions(clause("no_suppressions", kinds=["type"]), repo)
    assert [(r.evidence["target"], r.evidence["kind"]) for r in found] == [("src/a.ts", "// @ts-ignore")]
    everything = dg.no_suppressions(clause("no_suppressions"), repo)
    assert {r.evidence["kind"] for r in everything} == {"eslint-disable", "// @ts-ignore"}


# ky: console.log inside a JSDoc example counted as leftover debug code.
def test_debug_calls_in_comments_are_examples(repo):
    write(repo, "src/a.ts", "export const a = 1;\n")
    commit(repo)
    write(repo, "src/a.ts", "/**\n * @example\n * console.log(a);\n */\nexport const a = 1;\n// console.log(a)\nconsole.log(a);\n")
    found = dg.no_debug_code(clause("no_debug_code"), repo)
    assert [r.evidence["line"] for r in found] == [7]


# click: a skipif on a brand-new test isn't skipping anything that ran before.
def test_skip_on_a_new_test_is_fine_but_not_on_an_old_one(repo):
    write(repo, "tests/test_a.py", "def test_old():\n    assert True\n")
    commit(repo)
    write(repo, "tests/test_a.py", "import pytest\n\ndef test_old():\n    assert True\n\n"
          "@pytest.mark.skipif(\n    True,\n    reason='windows',\n)\ndef test_new():\n    assert True\n")
    assert dg.test_guard(clause("test_guard"), repo) == []
    write(repo, "tests/test_a.py", "import pytest\n\n@pytest.mark.skip\ndef test_old():\n    assert True\n")
    assert [r.evidence["change"] for r in dg.test_guard(clause("test_guard"), repo)] == ["skipped"]


# The fast diff path: a line whose content starts with "++ " isn't a file header.
def test_added_lines_from_git_diff(repo):
    write(repo, "a.txt", "one\ntwo\n")
    commit(repo)
    write(repo, "a.txt", "one\n++ plus\ntwo\nthree\n")
    (change,) = dg.changes(repo, "HEAD")
    assert change.added_lines() == [(2, "++ plus"), (4, "three")]
    assert change.changed_line_count() == 2


# click: a typing-only widening and a new base class aren't breaking changes.
@pytest.mark.parametrize("before, after, ok", [
    ("def hint(self, ctx: Context) -> str", "def hint(self, ctx: Context | None) -> str", True),
    ("def f(a: str)", "def f(a: Optional[str])", True),
    ("def f(a: 'Context')", "def f(a: Union[Context, None])", True),
    ("def f(a)", "def f(a: int)", True),
    ("def f(a) -> int | None", "def f(a) -> int", True),
    ("def f(a: int | None)", "def f(a: int)", False),
    ("def f(a) -> int", "def f(a) -> int | None", False),
    ("def f(a: int = 1)", "def f(a: int | None = 2)", False),
    ("class Parameter()", "class Parameter(ABC)", True),
    ("class A(Base)", "class A(Base, Generic[T])", True),
    ("class A(Base)", "class A()", False),
    ("@dataclass class A()", "@dataclass(frozen=True) class A()", False),
    ("x: int = 1", "x: int | None = 1", True),
    ("x: int = 1", "x: int = 2", False),
])
def test_compatible_changes(before, after, ok):
    assert compatible(before, after) is ok


# click: a pip-compile lockfile gained transitive pins; that's not a new dependency.
def test_compiled_requirements_are_lockfiles(repo):
    header = "#\n# This file is autogenerated by pip-compile with Python 3.13\n#\n"
    write(repo, "requirements/dev.in", "pytest\n")
    write(repo, "requirements/dev.txt", header + "pytest==8.0\n    # via -r dev.in\n")
    commit(repo)
    write(repo, "requirements/dev.txt", header + "pytest==8.1\n    # via -r dev.in\nmdurl==0.1\n    # via markdown-it-py\n")
    rule = clause("dependency_diff", allow_new=False, allow_removed=True, deny_major=False, lockfiles=False)
    assert dependency_diff.verify(rule, repo) == []
    write(repo, "requirements/dev.in", "pytest\nrequests\n")
    assert [r.evidence["symbol"] for r in dependency_diff.verify(rule, repo)] == ["requests"]


# werkzeug: "add error code to bare type ignore" edited 43 existing suppressions; none is new.
def test_edited_or_moved_suppressions_are_not_new(repo):
    write(repo, "src/a.py", "x = f()  # type: ignore\ny = g()  # noqa\n")
    write(repo, "src/b.py", "")
    commit(repo)
    write(repo, "src/a.py", "x = f()  # type: ignore[assignment]\n")
    write(repo, "src/b.py", "y = g()  # noqa\nz = h()  # type: ignore\n")
    found = dg.no_suppressions(clause("no_suppressions"), repo)
    assert [(r.evidence["target"], r.evidence["line"]) for r in found] == [("src/b.py", 2)]


# arrow, tenacity, attrs: typing-only API changes and a lazily loaded submodule.
def _surface(files, package="pkg"):
    from covenant.verifiers.api_surface import extract_surface
    return extract_surface(files.get, package)


def test_import_aliases_compare_by_what_they_name():
    before = {"pkg/__init__.py": "from datetime import date\ndef day(d: date) -> date: ...\n"}
    after = {"pkg/__init__.py": "from datetime import date as dt_date\ndef day(d: dt_date) -> dt_date: ...\n"}
    assert _surface(before)["pkg.day"] == _surface(after)["pkg.day"]


def test_moving_a_class_inside_the_package_keeps_annotations_equal():
    before = {"pkg/__init__.py": "class Doc: ...\ndef load() -> Doc: ...\n"}
    after = {"pkg/__init__.py": "from .doc import Doc\ndef load() -> Doc: ...\n", "pkg/doc.py": "class Doc: ...\n"}
    assert _surface(before)["pkg.load"] == _surface(after)["pkg.load"]


def test_typing_only_decorators_and_classvar():
    before = {"pkg/__init__.py": "class A:\n    NAME: str | None = None\n    def run(self) -> int: ...\n"}
    after = {"pkg/__init__.py": "import typing as t\nfrom typing import override\nclass A:\n"
                                "    NAME: t.ClassVar[str | None] = None\n    @override\n    def run(self) -> int: ...\n"}
    b, a = _surface(before), _surface(after)
    assert b["pkg.A.run"] == a["pkg.A.run"]
    assert compatible(b["pkg.A.NAME"], a["pkg.A.NAME"])


def test_lazily_loaded_submodule_is_still_exported():
    before = {"pkg/__init__.py": "from . import filters\n__all__ = ['filters']\n", "pkg/filters.py": "def f(): ...\n"}
    after = {"pkg/__init__.py": "__all__ = ['filters']\ndef __getattr__(name): ...\n", "pkg/filters.py": "def f(): ...\n"}
    assert _surface(before)["pkg.filters"] == _surface(after)["pkg.filters"] == "module pkg.filters"


# black, tqdm, clsx: suppressions quoted in docs/comments; prints in scripts; test fixtures.
def test_suppressions_in_prose_quotes_and_fixtures_are_not_code(repo):
    write(repo, "src/a.py", "x = 1\n")
    commit(repo)
    write(repo, "CHANGES.md", "- Fix crash on a `# type: ignore` comment\n")
    write(repo, "src/a.py", "x = 1\n# Python only reads a `# type: ignore` at the start of a comment\ny = f()  # type: ignore\n")
    write(repo, "tests/data/cases/case.py", "app  # type: ignore[attr-defined]\n")
    write(repo, "tests/test_a.py", "f('x')  # type: ignore[arg-type]\ng()  # type: ignore\n")
    found = dg.no_suppressions(clause("no_suppressions", kinds=["type"]), repo)
    assert [(r.evidence["target"], r.evidence["line"]) for r in found] == [("src/a.py", 3), ("tests/test_a.py", 2)]


def test_script_output_is_not_debug_code(repo):
    write(repo, "src/a.py", "x = 1\n")
    commit(repo)
    write(repo, ".meta/mkdocs.py", "print('pandoc not found')\n")
    write(repo, "bin/build.js", "console.log('built');\n")
    write(repo, "src/a.py", "x = 1\nprint('oops', file=sys.stderr)\nprint(x)\n")
    found = dg.no_debug_code(clause("no_debug_code", include_print=True), repo)
    assert [(r.evidence["target"], r.evidence["line"]) for r in found] == [("src/a.py", 3)]


def test_secret_heuristic_ignores_plain_words():
    assert dg.find_secrets("const secret = 'private-token';") == []
    assert dg.find_secrets("password = 'SuperSecretPass'") != []
    assert dg.find_secrets("api_key = 'f9a8b7c6d5e4f3a2b1c0'") != []


def test_skip_call_inside_a_new_test(repo):
    write(repo, "tests/test_a.py", "def test_old():\n    assert True\n")
    commit(repo)
    write(repo, "tests/test_a.py", "import pytest, sys\n\ndef test_old():\n    assert True\n\n"
          "def test_new():\n    if sys.version_info < (3, 13):\n        pytest.skip('needs 3.13')\n")
    assert dg.test_guard(clause("test_guard"), repo) == []


def test_shell_plumbing_is_not_a_test_command(tmp_path):
    write(tmp_path, ".github/workflows/t.yml", "steps:\n  - run: sed -i~ 's/x/y/' vitest.config.ts\n"
          "  - run: xargs -0 uv run pytest\n  - run: npx vitest run\n")
    assert [c for c, _ in find_test_commands(tmp_path)] == ["npx vitest run"]


# serde_json, log, anyhow, thiserror, reqwest: a Rust tests/ folder made `python -m pytest` the test command.
def test_rust_tests_folder_is_not_pytest(tmp_path):
    write(tmp_path, "Cargo.toml", "[package]\nname = 'x'\n")
    write(tmp_path, "tests/it.rs", "#[test]\nfn t() {}\n")
    assert [c for c, _ in find_test_commands(tmp_path)] == ["cargo test"]


# chi, testify: `interface{}` is `any`, and its braces cut the signature short.
def test_go_interface_braces_and_any():
    from covenant.verifiers.source_api import go_surface, compatible as src_compatible
    before = go_surface({"a.go": "package a\nfunc IsType(t T, expected interface{}, object interface{}, msg ...interface{}) bool {\n\treturn true\n}\n"})
    after = go_surface({"a.go": "package a\nfunc IsType(t T, expected any, object any, msg ...any) bool {\n\treturn true\n}\n"})
    assert before == after
    assert before[".:IsType"] == "func (T, any, any, ...any) bool"
    assert src_compatible("func (string) int", "func (string, ...string) int", "go")
    assert not src_compatible("func (string) int", "func (string, int) int", "go")


# serde_json, log: doc(hidden) items aren't API; dropping a bound accepts more types.
def test_rust_hidden_items_and_relaxed_bounds():
    from covenant.verifiers.source_api import rust_surface, compatible as src_compatible
    s = rust_surface({"src/lib.rs": "#[doc(hidden)]\npub fn internal() {}\npub fn api() {}\n"})
    assert "crate::internal" not in s and "crate::api" in s
    assert src_compatible("fn<T>(&T) ->Self where T:Fill + 'static, ", "fn<T>(&T) ->Self where T:Fill, ", "rust")
    assert not src_compatible("fn<T>(&T) ->Self where T:Fill, ", "fn<T>(&T) ->Self where T:Fill + Send, ", "rust")


# zod: a monorepo; the published package named like the repo lives in packages/zod.
def test_ts_entry_in_a_workspace_package(tmp_path):
    root = tmp_path / "zod"
    write(root, "package.json", json.dumps({"private": True, "workspaces": ["packages/*"]}))
    write(root, "packages/docs/package.json", json.dumps({"name": "docs", "private": True}))
    write(root, "packages/zod/package.json", json.dumps({"name": "zod", "exports": {".": {"@zod/source": "./src/index.ts", "types": "./index.d.cts"}}}))
    write(root, "packages/zod/src/index.ts", "export const z = 1;\n")
    assert detect_entries(root) == ["packages/zod/src/index.ts"]


# werkzeug (found by the second replay run): on Windows, `from .wrappers import Request` found
# wrappers/request.py, because file names ignore case, and the class looked like a module.
def test_class_import_isnt_mistaken_for_a_submodule_with_another_case(tmp_path):
    from covenant.verifiers.api_surface import extract_surface, working_tree_reader
    write(tmp_path, "pkg/__init__.py", "from .wrappers import Request\n")
    write(tmp_path, "pkg/wrappers/__init__.py", "from .request import Request\n")
    write(tmp_path, "pkg/wrappers/request.py", "class Request:\n    def get(self): ...\n")
    surface = extract_surface(working_tree_reader(tmp_path), "pkg")
    assert surface["pkg.Request"] == "class Request()"


# requests: `from . import _types as _t` moved out of `if TYPE_CHECKING:`; `_t.X` is `_types.X`.
def test_aliases_under_type_checking_blocks():
    before = {"pkg/__init__.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import _types as _t\n"
                                 "def send(body: '_t.Body') -> None: ...\n", "pkg/_types.py": "Body = bytes\n"}
    after = {"pkg/__init__.py": "from . import _types as _t\ndef send(body: '_t.Body') -> None: ...\n", "pkg/_types.py": "Body = bytes\n"}
    assert _surface(before)["pkg.send"] == _surface(after)["pkg.send"]


# ky: JSDoc examples written without a leading `*`.
def test_debug_calls_in_jsdoc_without_asterisks(repo):
    write(repo, "src/a.ts", "export const a = 1;\n")
    commit(repo)
    write(repo, "src/a.ts", "/**\nExample:\n```\nconsole.log(a);\n```\n*/\nexport const a = 1;\nconsole.log(a);\n")
    assert [r.evidence["line"] for r in dg.no_debug_code(clause("no_debug_code"), repo)] == [8]


# click, filelock, platformdirs, fastapi: the skip sits above a long parametrize table on a new test.
def test_skip_above_a_long_parametrize_on_a_new_test(repo):
    write(repo, "tests/test_a.py", "def test_old():\n    assert True\n")
    commit(repo)
    table = "".join(f"        ({i}, {i}),\n" for i in range(15))
    write(repo, "tests/test_a.py", "import pytest, sys\n\ndef test_old():\n    assert True\n\n"
          "@pytest.mark.skipif(\n    sys.platform == 'win32', reason='no'\n)  # pragma: no cover\n"
          f"@pytest.mark.parametrize(\n    ('a', 'b'),\n    [\n{table}    ],\n)\ndef test_new(a, b):\n    assert a == b\n")
    assert dg.test_guard(clause("test_guard"), repo) == []
    write(repo, "tests/test_a.py", "import pytest\n\n@pytest.mark.skipif(\n    True, reason='x'\n)\n"
          "@pytest.mark.parametrize('a', [1, 2])\ndef test_old(a):\n    assert True\n")
    assert [r.evidence["change"] for r in dg.test_guard(clause("test_guard"), repo)] == ["skipped"]


# structlog: a skipif on a new test class.
def test_skip_on_a_new_test_class(repo):
    write(repo, "tests/test_a.py", "def test_old():\n    assert True\n")
    commit(repo)
    write(repo, "tests/test_a.py", "import pytest, sys\n\ndef test_old():\n    assert True\n\n"
          "@pytest.mark.skipif(\n    sys.version_info < (3, 15), reason='3.15'\n)\nclass TestLazy:\n    def test_x(self):\n        assert True\n")
    assert dg.test_guard(clause("test_guard"), repo) == []
