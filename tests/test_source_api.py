import sys
from pathlib import Path

import pytest

from covenant.compiler.extract import HeuristicExtractor
from covenant.compiler.pipeline import compile_instruction
from covenant.ir import Clause
from covenant.verifiers import source_api

from conftest import git

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import api_samples  # noqa: E402

MARKERS = {"go": ("go.mod", "module example.com/store\n"), "rust": ("Cargo.toml", '[package]\nname = "doc"\n'),
           "java": ("pom.xml", "<project/>\n")}
SAMPLES = {"go": api_samples.GO, "rust": api_samples.RUST, "java": api_samples.JAVA}


def make_repo(tmp_path, language):
    repo = tmp_path / language
    for rel, text in SAMPLES[language].items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    marker, content = MARKERS[language]
    (repo / marker).write_text(content)
    for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "base")):
        assert git(repo, *args).returncode == 0
    return repo


def clause(**params):
    return Clause(id="C1", kind="SemanticInvariant", source_span="don't change public APIs",
                  checker="source_api_diff", params={"baseline": "HEAD", **params})


def edit(repo, rel, old, new):
    path = repo / rel
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


def symbols(repo, **params):
    return sorted(r.evidence["symbol"] for r in source_api.verify(clause(**params), repo))


@pytest.mark.parametrize("language", ["go", "rust", "java"])
def test_unchanged_repo_has_no_reports(tmp_path, language):
    assert symbols(make_repo(tmp_path, language)) == []


def test_go(tmp_path):
    repo = make_repo(tmp_path, "go")
    assert source_api.detect_language(repo) == "go"
    edit(repo, "store/store.go", "func (s *Store) Get(key, fallback string)", "func (s *Store) Get(k, f string)")
    edit(repo, "store/store.go", "func helper() {}", "func helper() {}\n\nfunc Extra() {}")  # additive
    edit(repo, "store/store.go", "func (s *Store) put() {}", "func (s *Store) put(x int) {}")  # private
    edit(repo, "internal/x/x.go", "func Exported() {}", "func Exported(x int) {}")  # internal package
    assert symbols(repo) == []  # param renames, additions and private changes are fine
    edit(repo, "store/store.go", "Size, Cap int", "Size int64\n\tCap int")
    edit(repo, "store/store.go", "\tRead(key string) ([]byte, error)\n", "")
    assert symbols(repo) == ["store:Reader.Read", "store:Store.Size"]
    assert "store:Extra" in symbols(repo, allow_additive=False)


def test_rust(tmp_path):
    repo = make_repo(tmp_path, "rust")
    edit(repo, "src/lib.rs", "pub fn new(text: &str)", "pub fn new(input: &str)")  # rename only
    edit(repo, "src/lib.rs", "    fn private(&self) {}", "    fn private(&self, x: u8) {}")
    edit(repo, "src/lib.rs", "pub(crate) fn internal() {}", "pub(crate) fn internal(x: u8) {}")
    assert symbols(repo) == []
    edit(repo, "src/lib.rs", "pub fn len(&self) -> usize", "pub fn len(&self) -> u64")
    edit(repo, "src/lib.rs", "    Custom(String),\n", "")
    edit(repo, "src/parse.rs", "pub async fn run(&mut self, src: &str)", "pub async fn run(&mut self, src: &str, strict: bool)")
    assert symbols(repo) == ["crate::Doc::len", "crate::Mode::Custom", "crate::parse::Parser::run"]


def test_java(tmp_path):
    repo = make_repo(tmp_path, "java")
    client = "src/main/java/com/acme/Client.java"
    edit(repo, client, "public String get(String path, int timeoutMs)", "public String get(String p, int t)")  # rename
    edit(repo, client, "    void packagePrivate() {}", "    void packagePrivate(int x) {}")
    edit(repo, client, "    @Override", "    public String get(String path, boolean cached) { return \"\"; }\n\n    @Override")  # new overload
    assert symbols(repo) == []
    edit(repo, client, "public static final int TIMEOUT = 30;", "public static final long TIMEOUT = 30;")
    edit(repo, client, "public String get(String path) throws java.io.IOException", "public String get(String path)")
    edit(repo, "src/main/java/com/acme/Api.java", "    String fetch(String id);", "    String fetch(String id, int version);")
    assert symbols(repo) == ["com.acme.Api.fetch(String)", "com.acme.Client.TIMEOUT", "com.acme.Client.get(String)"]


@pytest.mark.parametrize("language, test_cmd", [("go", "go test ./..."), ("rust", "cargo test"), ("java", "mvn -q test")])
def test_compiler_binds_source_api(tmp_path, language, test_cmd):
    repo = make_repo(tmp_path, language)
    cov = compile_instruction("Refactor, but don't change public APIs, and run tests before committing",
                              repo, HeuristicExtractor()).covenant
    api, tests = cov.clauses
    assert api.checker == "source_api_diff" and api.params["language"] == language
    assert tests.params["test_cmd"] == test_cmd
