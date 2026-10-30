import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "app.db"


def connect() -> sqlite3.Connection:
    return sqlite3.connect(DB)


def emails() -> list[str]:
    with connect() as db:
        return [r[0] for r in db.execute("SELECT email FROM users ORDER BY id")]
