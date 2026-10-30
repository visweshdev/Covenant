"""sqlite_guard: inserts survive, lost rows, tables and columns come back."""

import sqlite3
from contextlib import closing

import pytest

from covenant.events import Commit, Run
from covenant.ir import Clause, ClauseKind, Covenant, OnViolation
from covenant.runtime import Runtime
from covenant.validate import validate


def make_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            '''
            CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, nickname TEXT);
            INSERT INTO users VALUES (1, 'a@x.io', 'ann'), (2, 'b@x.io', 'bob'), (3, 'c@x.io', NULL);
            CREATE TABLE notes (body TEXT);
            INSERT INTO notes VALUES ('one'), ('two');
            CREATE TABLE "odd ""name""" (k TEXT PRIMARY KEY, v TEXT);
            INSERT INTO "odd ""name""" VALUES ('x', '1');
            '''
        )
        db.commit()


def sql(path, script):
    with closing(sqlite3.connect(path)) as db:
        db.executescript(script)
        db.commit()


def rows(path, query):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(query).fetchall()


def guard(on_violation=OnViolation.ROLLBACK, **params):
    return Clause(
        id="D1", kind=ClauseKind.SEMANTIC_INVARIANT, source_span="don't delete user data",
        checker="sqlite_guard", params={"databases": ["data/*.db"], **params}, on_violation=on_violation,
    )


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    db = repo / "data" / "app.db"
    make_db(db)

    def start(clause=None):
        rt = Runtime(Covenant(instruction="x", goal={"action": "x"}, clauses=[clause or guard()]), repo, state_dir=tmp_path / "state")
        assert rt.start()["snapshot_databases"] == 1
        return rt

    return repo, db, start


def test_valid_clause(tmp_path):
    assert not [i for i in validate(Covenant(instruction="x", goal={"action": "x"}, clauses=[guard()]), tmp_path) if i.level == "error"]


def test_inserts_and_updates_are_allowed(setup):
    _, db, start = setup
    rt = start()
    sql(db, "INSERT INTO users VALUES (4, 'd@x.io', 'dee'); UPDATE users SET nickname = 'annie' WHERE id = 1;"
            "ALTER TABLE users ADD COLUMN plan TEXT;")
    d = rt.record(Run(cmd="python seed.py", exit_code=0))
    assert d.reports == [] and d.allowed
    assert rt.record(Commit()).allowed


def test_deleted_rows_are_put_back_and_inserts_kept(setup):
    _, db, start = setup
    rt = start()
    sql(db, "DELETE FROM users WHERE id IN (2, 3); INSERT INTO users VALUES (9, 'z@x.io', 'zed'); DELETE FROM notes;")
    d = rt.record(Run(cmd="python cleanup.py", exit_code=0))
    by_table = {r.evidence["table"]: r for r in d.reports}
    assert by_table["users"].evidence["rows"] == 2 and by_table["users"].evidence["sample"] == [[2], [3]]
    assert by_table["notes"].evidence["key"] == ["rowid"]
    assert all(r.evidence["rolled_back"] is True for r in d.reports)
    assert "put the lost data back" in by_table["users"].repair_hints[0]
    assert rows(db, "SELECT id FROM users ORDER BY id") == [(1,), (2,), (3,), (9,)]
    assert rows(db, "SELECT body FROM notes ORDER BY body") == [("one",), ("two",)]
    # Restored, so nothing blocks a commit, and the next event is quiet.
    assert rt.record(Commit()).allowed
    assert rt.record(Run(cmd="ls", exit_code=0)).reports == []


def test_dropped_table_and_column_come_back(setup):
    _, db, start = setup
    rt = start()
    sql(db, 'DROP TABLE "odd ""name"""; ALTER TABLE users DROP COLUMN nickname;')
    d = rt.record(Run(cmd="python migrate.py", exit_code=0))
    assert {r.evidence["operation"] for r in d.reports} == {"drop_table", "drop_column"}
    assert rows(db, 'SELECT * FROM "odd ""name"""') == [("x", "1")]
    assert rows(db, "SELECT id, nickname FROM users ORDER BY id") == [(1, "ann"), (2, "bob"), (3, None)]


def test_deleted_database_file_comes_back(setup):
    _, db, start = setup
    rt = start()
    db.unlink()
    d = rt.record(Run(cmd="rm data/app.db", exit_code=0))
    assert [r.evidence["operation"] for r in d.reports] == ["delete_database"]
    assert rows(db, "SELECT count(*) FROM users") == [(3,)]


def test_forbid_updates_and_table_filter(setup):
    _, db, start = setup
    rt = start(guard(forbid_updates=True, tables=["users"]))
    sql(db, "UPDATE users SET email = 'hacked' WHERE id = 1; DELETE FROM notes;")
    d = rt.record(Run(cmd="python x.py", exit_code=0))
    assert [(r.evidence["table"], r.evidence["operation"]) for r in d.reports] == [("users", "update_rows")]
    assert rows(db, "SELECT email FROM users WHERE id = 1") == [("a@x.io",)]
    assert rows(db, "SELECT count(*) FROM notes") == [(0,)]  # not protected


def test_block_mode_reports_and_gates_commit(setup):
    _, db, start = setup
    rt = start(guard(on_violation=OnViolation.BLOCK))
    sql(db, "DELETE FROM users WHERE id = 1;")
    assert rt.preview().reports[0].evidence["pending"] is True  # preview never repairs
    d = rt.record(Commit())
    assert not d.allowed and d.reports[0].evidence["operation"] == "delete_rows"
    assert rows(db, "SELECT count(*) FROM users") == [(2,)]  # block mode leaves the data alone


def test_unchanged_database_is_skipped_quickly(setup, monkeypatch):
    _, _, start = setup
    rt = start()
    from covenant.verifiers import sqlite_guard

    monkeypatch.setattr(sqlite_guard, "diff", lambda *a, **k: pytest.fail("diff ran on an unchanged database"))
    assert rt.record(Run(cmd="ls", exit_code=0)).reports == []
