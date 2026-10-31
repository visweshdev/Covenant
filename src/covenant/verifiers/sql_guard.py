"""Destructive SQL, found in shell commands and database tool calls before they run.

Works for Postgres, MySQL/MariaDB, SQLite, SQL Server, DuckDB and the like, because it reads
the SQL itself rather than talking to the database:

- inline SQL: `psql -c "DROP TABLE users"`, `mysql -e "..."`, `sqlite3 app.db "..."`,
  `echo "TRUNCATE x" | psql`, here-strings;
- SQL files the command feeds to a client: `psql -f wipe.sql`, `mysql < wipe.sql`, `\\i x.sql`;
- MCP database tools (any tool input field named sql, query or statement);
- MongoDB and Redis shells: `db.users.drop()`, `deleteMany({})`, `FLUSHALL`.

Destructive means data or schema is lost: DROP TABLE/DATABASE/SCHEMA/VIEW, TRUNCATE,
DELETE or UPDATE with no WHERE, ALTER TABLE ... DROP. A DELETE with a WHERE is allowed;
the agent is expected to change rows, just not wipe them.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

CLIENTS = re.compile(
    r"\b(?:psql|pg_restore|mysql|mariadb|sqlite3|sqlcmd|duckdb|cockroach\s+sql|clickhouse(?:-client)?|"
    r"mongo(?:sh)?|redis-cli|snowsql|bq\s+query|trino|presto|usql|osql|supabase\s+db|prisma\s+db\s+execute)\b",
    re.I,
)
# Things that run SQL from code: `python -c "...execute('DELETE FROM x')"`, `node -e`.
RUNNERS = re.compile(r"\b(?:python3?|node|ruby|php)\b[^\n]*\s-[ce]\s", re.I)

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("drop", re.compile(r"\bdrop\s+(?:table|database|schema|view|materialized\s+view|collection|keyspace)\b", re.I)),
    ("truncate", re.compile(r"\btruncate\b(?:\s+table)?\s+[\w.\"`\[]", re.I)),
    ("alter_drop", re.compile(r"\balter\s+table\b[^;]*\bdrop\b", re.I)),
    ("delete_all", re.compile(r"\bdelete\s+from\s+[\w.\"`\[\]]+(?![^;]*\bwhere\b)", re.I)),
    ("update_all", re.compile(r"\bupdate\s+[\w.\"`\[\]]+\s+set\b(?![^;]*\bwhere\b)", re.I)),
    ("mongo_drop", re.compile(r"\.\s*(?:drop|dropDatabase)\s*\(\s*\)|\.\s*(?:deleteMany|remove)\s*\(\s*\{\s*\}\s*\)", re.I)),
    ("redis_flush", re.compile(r"\bflush(?:all|db)\b", re.I)),
]
WHAT = {
    "drop": "drops a table, database or schema",
    "truncate": "empties a table (TRUNCATE)",
    "alter_drop": "drops a column or constraint (ALTER TABLE ... DROP)",
    "delete_all": "deletes every row (DELETE with no WHERE)",
    "update_all": "overwrites every row (UPDATE with no WHERE)",
    "mongo_drop": "drops or empties a MongoDB collection",
    "redis_flush": "wipes a Redis database",
}
SQL_FIELDS = ("sql", "query", "statement", "statements", "command")
_FILE_ARGS = re.compile(r"(?:\s-f\s*|\s--file[= ]|<\s*|\\i\s+|\bsource\s+|\.read\s+)([^\s;|&<>]+\.sql)\b", re.I)


def destructive(sql: str) -> list[tuple[str, str]]:
    """(kind, statement) for each destructive statement in some SQL text."""
    found = []
    # Comments don't run. A `--` comment needs a space after it, so CLI flags like --eval stay.
    text = re.sub(r"(?:^|(?<=[\s'\"]))--(?=\s|$)[^\n]*|/\*.*?\*/", " ", sql, flags=re.S)
    for statement in re.split(r";", text):
        statement = " ".join(statement.split())
        if not statement:
            continue
        for kind, rx in _RULES:
            if rx.search(statement):
                found.append((kind, statement[:200]))
                break
    return found


def _sql_files(command: str, repo: Path | None) -> list[str]:
    texts = []
    for name in _FILE_ARGS.findall(command):
        path = Path(name.strip("'\""))
        if repo is not None and not path.is_absolute():
            path = repo / path
        try:
            if path.is_file() and path.stat().st_size < 5_000_000:
                texts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return texts


def in_command(command: str, repo: Path | None = None) -> list[tuple[str, str]]:
    """Destructive SQL a shell command would run. Only commands that reach a database
    count, so `git commit -m "drop table support"` is fine."""
    if not (CLIENTS.search(command) or (RUNNERS.search(command) and re.search(r"execute|query|sql", command, re.I))):
        return []
    hits = destructive(command)
    for text in _sql_files(command, repo):
        hits += destructive(text)
    return hits


def in_tool_input(tool_input: dict) -> list[tuple[str, str]]:
    """Destructive SQL in a database tool call (an MCP server's query tool, say)."""
    hits = []
    for key, value in tool_input.items():
        if key.lower() in SQL_FIELDS and isinstance(value, str):
            hits += destructive(value)
        elif key.lower() in SQL_FIELDS and isinstance(value, list):
            hits += [h for v in value if isinstance(v, str) for h in destructive(v)]
    return hits


def split_words(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()
