"""TypeScript API checker. Needs node and a typescript package (COVENANT_TYPESCRIPT=<path> or resolvable)."""

import os
import shutil
import subprocess

import pytest

from covenant.ir import Clause
from covenant.verifiers import ts_api_surface

from conftest import git


def _typescript_available() -> bool:
    if not shutil.which("node"):
        return False
    if os.environ.get("COVENANT_TYPESCRIPT"):
        return True
    return subprocess.run(["node", "-e", "require.resolve('typescript')"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _typescript_available(), reason="node + typescript not available")

INDEX = """\
export { parse, type Options } from "./parse";
export class Client {
  constructor(public readonly url: string) {}
  get(path: string): Promise<string> { return Promise.resolve(this.url + path); }
  private secret(): void {}
}
export interface Doc { text: string; strict?: boolean }
export const VERSION = "1.0";
export enum Mode { Lenient, Strict }
"""
PARSE = """\
import type { Doc } from "./index";
export interface Options { strict?: boolean }
export function parse(text: string, options: Options = {}): Doc {
  return { text: text.trim(), strict: options.strict };
}
"""


@pytest.fixture
def ts_repo(tmp_path):
    repo = tmp_path / "tslib"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "index.ts").write_text(INDEX)
    (repo / "src" / "parse.ts").write_text(PARSE)
    (repo / "package.json").write_text('{"name": "tslib", "source": "src/index.ts"}')
    for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "base")):
        assert git(repo, *args).returncode == 0
    return repo


def clause(**params):
    return Clause(id="C1", kind="SemanticInvariant", source_span="don't change public APIs",
                  checker="ts_api_surface_diff", params={"baseline": "HEAD", **params})


def test_surface_extraction(ts_repo):
    surface = ts_api_surface.extract(ts_repo, ["src/index.ts"])
    assert surface["src/index.ts:parse"].startswith("function (text: string, options?: Options) => Doc")
    assert "src/index.ts:Client.get" in surface
    assert "src/index.ts:Client.secret" not in surface
    assert surface["src/index.ts:Doc.strict"].startswith("?")
    assert "src/index.ts:Mode.Strict" in surface


def test_no_change_no_reports(ts_repo):
    assert ts_api_surface.verify(clause(), ts_repo) == []


def test_signature_change_is_reported(ts_repo):
    parse = ts_repo / "src" / "parse.ts"
    parse.write_text(PARSE.replace("text: string, options: Options = {}", "text: string, options: Options"))
    reports = ts_api_surface.verify(clause(), ts_repo)
    assert [r.evidence["symbol"] for r in reports] == ["src/index.ts:parse"]


def test_interface_member_removal_and_additions(ts_repo):
    index = ts_repo / "src" / "index.ts"
    index.write_text(INDEX.replace("; strict?: boolean", "") + "export const extra = 1;\n")
    symbols = {r.evidence["symbol"] for r in ts_api_surface.verify(clause(), ts_repo)}
    assert "src/index.ts:Doc.strict" in symbols
    assert "src/index.ts:extra" not in symbols
    strict = {r.evidence["symbol"] for r in ts_api_surface.verify(clause(allow_additive=False), ts_repo)}
    assert "src/index.ts:extra" in strict


def test_entry_detection(ts_repo):
    assert ts_api_surface.detect_entries(ts_repo) == ["src/index.ts"]


def _edit(repo, rel, old, new):
    path = repo / rel
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


@pytest.mark.parametrize(
    "rel, old, new",
    [
        # a new optional parameter
        ("src/parse.ts", "options: Options = {}): Doc", "options: Options = {}, trim?: boolean): Doc"),
        # a required parameter becomes optional; renaming it doesn't matter to callers
        ("src/parse.ts", "parse(text: string,", "parse(input?: string,"),
        # a method gains an optional parameter
        ("src/index.ts", "get(path: string): Promise<string>", "get(path: string, retries = 0): Promise<string>"),
        # a constructor gains an optional parameter
        ("src/index.ts", "constructor(public readonly url: string)", "constructor(public readonly url: string, timeoutMs?: number)"),
        # a new rest parameter
        ("src/parse.ts", "options: Options = {}): Doc", "options: Options = {}, ...tags: string[]): Doc"),
    ],
)
def test_additive_changes_are_allowed(ts_repo, rel, old, new):
    if "input?: string" in new:
        _edit(ts_repo, rel, "text.trim()", "(input ?? \"\").trim()")
    _edit(ts_repo, rel, old, new)
    assert ts_api_surface.verify(clause(), ts_repo) == []


@pytest.mark.parametrize(
    "rel, old, new",
    [
        ("src/parse.ts", "options: Options = {}): Doc", "options: Options = {}, trim: boolean): Doc"),  # new required
        ("src/parse.ts", "parse(text: string,", "parse(text: number,"),  # type change
        ("src/parse.ts", "): Doc {", "): Doc | null {"),  # return type change
        ("src/index.ts", "get(path: string): Promise<string>", "get(path: string, retries: number): Promise<string>"),
    ],
)
def test_breaking_changes_are_still_reported(ts_repo, rel, old, new):
    _edit(ts_repo, rel, old, new)
    assert ts_api_surface.verify(clause(), ts_repo) != []


def test_additive_ok_can_be_turned_off(ts_repo):
    _edit(ts_repo, "src/parse.ts", "options: Options = {}): Doc", "options: Options = {}, trim?: boolean): Doc")
    assert ts_api_surface.verify(clause(allow_additive=False), ts_repo) != []
