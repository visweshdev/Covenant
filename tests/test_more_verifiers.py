import json

import pytest

from covenant.compiler.extract import HeuristicExtractor
from covenant.compiler.pipeline import compile_instruction
from covenant.ir import Clause
from covenant.verifiers import dependency_diff, schema_guard

from conftest import git


def clause(checker, **params):
    return Clause(id="C9", kind="SemanticInvariant", source_span="x", checker=checker, params={"baseline": "HEAD", **params})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "app"
    (r / "alembic" / "versions").mkdir(parents=True)
    (r / "alembic" / "versions" / "001_init.py").write_text("def upgrade():\n    op.create_table('users')\n")
    (r / "package.json").write_text(json.dumps({"dependencies": {"left-pad": "^1.0.0", "react": "^18.2.0"}}))
    (r / "package-lock.json").write_text(json.dumps({"packages": {
        "node_modules/left-pad": {"version": "1.3.0"}, "node_modules/react": {"version": "18.2.0"}}}))
    (r / "pyproject.toml").write_text('[project]\nname = "app"\ndependencies = ["requests>=2.31", "Flask_Login==0.6"]\n')
    for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "base")):
        assert git(r, *args).returncode == 0
    return r


# ----- schema_guard ----------------------------------------------------------------------


def test_safe_migration_passes(repo):
    (repo / "alembic" / "versions" / "002_add.py").write_text("def upgrade():\n    op.add_column('users', sa.Column('age'))\n")
    assert schema_guard.verify(clause("schema_guard", dirs=["alembic/versions"]), repo) == []


def test_destructive_migrations_are_reported(repo):
    (repo / "alembic" / "versions" / "002_drop.py").write_text(
        "def upgrade():\n    op.drop_column('users', 'email')\n    op.alter_column('users', 'age', type_=sa.SmallInteger)\n")
    (repo / "alembic" / "versions" / "003.sql").write_text("DROP TABLE sessions;\nDELETE FROM audit;\n")
    reports = schema_guard.verify(clause("schema_guard", dirs=["alembic/versions"]), repo)
    ops = sorted((r.evidence["target"].rsplit("/", 1)[1], r.evidence["operation"], r.evidence["line"]) for r in reports)
    assert ops == [("002_drop.py", "change_type", 3), ("002_drop.py", "drop_column", 2),
                   ("003.sql", "delete_rows", 2), ("003.sql", "drop_table", 1)]


def test_allow_list_and_old_migrations(repo):
    (repo / "alembic" / "versions" / "002_drop.py").write_text("op.drop_column('users', 'email')\n")
    assert schema_guard.verify(clause("schema_guard", dirs=["alembic/versions"], allow=["drop_column"]), repo) == []
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "drop")
    # Already in the baseline: not this task's change.
    assert schema_guard.verify(clause("schema_guard", dirs=["alembic/versions"]), repo) == []


# ----- dependency_diff -------------------------------------------------------------------


def _bump(repo, name, version):
    lock = json.loads((repo / "package-lock.json").read_text())
    lock["packages"][f"node_modules/{name}"] = {"version": version}
    (repo / "package-lock.json").write_text(json.dumps(lock))


def test_minor_bump_is_fine_major_is_not(repo):
    _bump(repo, "react", "18.3.1")
    assert dependency_diff.verify(clause("dependency_diff"), repo) == []
    _bump(repo, "react", "19.0.0")
    [r] = dependency_diff.verify(clause("dependency_diff"), repo)
    assert r.evidence["change"] == "major bump" and r.evidence["symbol"] == "react"


def test_only_allowed_packages_change(repo):
    (repo / "pyproject.toml").write_text('[project]\nname = "app"\ndependencies = ["requests>=2.32", "flask-login==0.7"]\n')
    reports = dependency_diff.verify(clause("dependency_diff", allow=["requests"], deny_major=False), repo)
    assert [(r.evidence["symbol"], r.evidence["change"]) for r in reports] == [("flask-login", "changed")]


def test_added_and_removed(repo):
    (repo / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.2.0", "lodash": "^4"}}))
    changes = {(r.evidence["symbol"], r.evidence["change"]) for r in dependency_diff.verify(clause("dependency_diff", allow_new=False), repo)}
    assert changes == {("left-pad", "removed"), ("lodash", "added")}


# ----- the compiler binds to them --------------------------------------------------------


def test_compiler_binds_new_checkers(repo):
    cov = compile_instruction(
        "Upgrade the ORM, but avoid destructive migrations, and only update requests",
        repo, HeuristicExtractor(),
    ).covenant
    checkers = {c.source_span: (c.checker, c.params) for c in cov.clauses}
    assert checkers["avoid destructive migrations"][0] == "schema_guard"
    assert checkers["avoid destructive migrations"][1]["dirs"] == ["alembic/versions"]
    assert checkers["only update requests"][0] == "dependency_diff"
    assert checkers["only update requests"][1]["allow"] == ["requests"]
