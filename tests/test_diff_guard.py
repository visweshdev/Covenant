"""Checkers that read the change since the task started, on a real git repo."""

import pytest

from covenant.events import Commit
from covenant.ir import Clause, ClauseKind, Covenant
from covenant.runtime import Runtime
from covenant.validate import validate
from covenant.verifiers import diff_guard as dg

from conftest import git


def clause(checker, **params):
    return Clause(id="R1", kind=ClauseKind.SEMANTIC_INVARIANT, source_span="rule", checker=checker,
                  params={"baseline": "HEAD", **params})


def run(checker, repo, **params):
    return dg.VERIFIERS[checker](clause(checker, **params), repo)


def write(repo, rel, text):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_changes_lists_tracked_untracked_and_skips_covenant_files(mylib_repo):
    write(mylib_repo, "mylib/core.py", (mylib_repo / "mylib/core.py").read_text() + "\nX = 1\n")
    write(mylib_repo, "notes.txt", "new\n")
    write(mylib_repo, ".claude/settings.json", "{}")
    (mylib_repo / "README.md").unlink() if (mylib_repo / "README.md").exists() else None
    found = {c.path: c.status for c in dg.changes(mylib_repo, "HEAD")}
    assert found["mylib/core.py"] == "M" and found["notes.txt"] == "A"
    assert ".claude/settings.json" not in found and "covenant.yaml" not in found
    [core] = [c for c in dg.changes(mylib_repo, "HEAD") if c.path == "mylib/core.py"]
    assert core.added_lines()[-1][1] == "X = 1"


def test_scope_guard(mylib_repo):
    write(mylib_repo, "mylib/core.py", (mylib_repo / "mylib/core.py").read_text() + "\n# ok\n")
    assert run("scope_guard", mylib_repo, allow=["mylib/**"]) == []
    write(mylib_repo, "setup.cfg", "[x]\n")
    [r] = run("scope_guard", mylib_repo, allow=["mylib/**"])
    assert r.evidence["target"] == "setup.cfg" and r.evidence["op"] == "add"
    write(mylib_repo, "mylib/extra.py", "")
    assert {r.evidence["target"] for r in run("scope_guard", mylib_repo, allow=["**"], allow_new=False)} == {"setup.cfg", "mylib/extra.py"}
    assert dg.scope_allows(clause("scope_guard", allow=["mylib/**"]), "mylib/a.py")
    assert not dg.scope_allows(clause("scope_guard", allow=["mylib/**"]), "tests/a.py")


@pytest.mark.parametrize("line, kind", [
    ('KEY = "sk-ant-api03-' + "a1B2" * 10 + '"', "Anthropic API key"),
    ("token: sk-or-v1-" + "0123456789abcdef" * 4, "OpenRouter API key"),
    ("aws = 'AKIA" + "ABCDEFGHIJKLMNOP" + "'", "AWS access key"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "private key"),
    ("url = 'postgres://admin:hunter2pass@db.internal/app'", "password in a URL"),
    ('password = "Zx9!kq2#Lm8@pw"', "hard-coded password"),
    ("gh = 'ghp_" + "A" * 36 + "'", "GitHub token"),
])
def test_secret_patterns(line, kind):
    assert kind in [k for k, _ in dg.find_secrets(line)]


@pytest.mark.parametrize("line", [
    'password = "changeme-please"', 'api_key = os.environ["API_KEY"]', 'token = "<your token here>"',
    'secret = "${SECRET_FROM_ENV}"', "sk-ant is the prefix", 'password = "aaaaaaaaaaaa"',
])
def test_secret_scan_ignores_placeholders(line):
    assert dg.find_secrets(line) == []


def test_secret_scan_reports_masked(mylib_repo):
    key = "sk-or-v1-" + "0123456789abcdef" * 4
    write(mylib_repo, "mylib/config.py", f'KEY = "{key}"\n')
    write(mylib_repo, ".env", "X=1\n")
    write(mylib_repo, ".env.example", "X=\n")
    reports = run("secret_scan", mylib_repo)
    kinds = {r.evidence["kind"] for r in reports}
    assert kinds == {"OpenRouter API key", "environment file"}
    [r] = [r for r in reports if r.evidence["kind"] == "OpenRouter API key"]
    assert r.evidence["line"] == 1 and key not in str(r.model_dump()) and "revoked" in r.repair_hints[0]


def test_test_guard(mylib_repo):
    tests = sorted((mylib_repo / "tests").glob("test_*.py"))[0]
    rel = tests.relative_to(mylib_repo).as_posix()
    original = tests.read_text()
    assert run("test_guard", mylib_repo) == []
    write(mylib_repo, rel, "import pytest\n\n@pytest.mark.skip\n" + original)
    assert [r.evidence["change"] for r in run("test_guard", mylib_repo)] == ["skipped"]
    first = original.index("def test")
    write(mylib_repo, rel, original[:first])  # every test removed
    assert [r.evidence["change"] for r in run("test_guard", mylib_repo)] == ["tests removed"]
    tests.unlink()
    assert [r.evidence["change"] for r in run("test_guard", mylib_repo)] == ["deleted"]
    write(mylib_repo, "web/app.test.ts", "it.only('x', () => {})\n")
    assert "focused with .only" in [r.evidence["change"] for r in run("test_guard", mylib_repo)]


def test_diff_budget(mylib_repo):
    for n in range(3):
        write(mylib_repo, f"mylib/m{n}.py", "a = 1\nb = 2\n")
    assert run("diff_budget", mylib_repo, max_files=5, max_lines=10) == []
    reports = run("diff_budget", mylib_repo, max_files=2, max_lines=5)
    assert {r.evidence.get("files") for r in reports} == {3, None} and len(reports) == 2


def test_debug_code_and_suppressions(mylib_repo):
    write(mylib_repo, "mylib/core.py", (mylib_repo / "mylib/core.py").read_text()
          + "\nimport pdb\nX = 1  # type: ignore\n")
    write(mylib_repo, "web/app.ts", "const a = 1;\ndebugger;\nconsole.log(a) // eslint-disable-line\n")
    write(mylib_repo, "tests/test_dbg.py", "def test_x():\n    breakpoint()\n")
    debug = run("no_debug_code", mylib_repo)
    assert sorted((r.evidence["target"], r.evidence["line"]) for r in debug)[:1] == [("mylib/core.py", debug[0].evidence["line"])]
    assert {r.evidence["target"] for r in debug} == {"mylib/core.py", "web/app.ts"}  # tests are skipped
    assert {r.evidence["kind"] for r in run("no_suppressions", mylib_repo)} == {"# type: ignore", "eslint-disable"}


def test_large_files(mylib_repo):
    write(mylib_repo, "data/dump.bin", "x" * 3000)
    [r] = run("large_files", mylib_repo, max_kb=2)
    assert r.evidence["target"] == "data/dump.bin"


def test_checkers_through_the_runtime(mylib_repo, tmp_path):
    scope = clause("scope_guard", allow=["mylib/**", "tests/**"]).model_copy(update={"id": "R2"})
    cov = Covenant(instruction="x", goal={"action": "x"}, clauses=[clause("secret_scan"), scope])
    assert not [i for i in validate(cov, mylib_repo) if i.level == "error"]
    cov.dump(mylib_repo / "covenant.yaml")
    git(mylib_repo, "commit", "-qam", "rules")
    rt = Runtime.open(mylib_repo, state_dir=tmp_path / "state")
    rt.start()
    assert rt.record(Commit(), dry_run=True).allowed
    write(mylib_repo, "mylib/keys.py", "K = 'ghp_" + "Z" * 36 + "'\n")
    d = rt.record(Commit(), dry_run=True)
    assert not d.allowed and [r.checker for r in d.blocking] == ["secret_scan"]
    assert rt.scope_reports(str(mylib_repo / "setup.py")) and not rt.scope_reports(str(mylib_repo / "mylib" / "x.py"))
    git(mylib_repo, "add", "-A")
    assert run("secret_scan", mylib_repo)  # staged changes count too
