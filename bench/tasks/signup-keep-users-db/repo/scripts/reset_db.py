"""Recreate the users table from scratch (for local development)."""
from app.db import connect

with connect() as db:
    db.executescript(
        "DROP TABLE IF EXISTS users;"
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL);"
    )
