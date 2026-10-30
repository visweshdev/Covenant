import sqlite3
from pathlib import Path


def test_migrations_apply():
    db = sqlite3.connect(":memory:")
    for sql in sorted(Path("migrations").glob("*.sql")):
        db.executescript(sql.read_text())
    assert db.execute("select count(*) from users").fetchone()[0] >= 0
