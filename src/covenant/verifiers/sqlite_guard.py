"""sqlite_guard: rows that existed when the task started must still exist.

Inserts are fine. Deleted rows, dropped tables, dropped columns and a deleted database file
are violations; with params.forbid_updates, so are changed values. With on_violation:
rollback, the lost data is put back row by row from a snapshot, so the agent's legitimate
writes survive. Protecting the whole file as an effect would undo those writes too.

The runtime checks after every event (not only at commit), because data loss happens
the moment a script runs.

Clause params:
    databases:      repo-relative paths or globs of SQLite files
    tables:         only these tables (default: every table)
    forbid_updates: changed values count too (default false)
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ..feedback import ViolationReport
from ..ir import Clause
from ..monitors.effects import compile_glob, normalize_path, static_prefix

SAMPLE = 5


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def database_files(clause: Clause, repo: Path) -> list[str]:
    """Repo-relative paths of the databases a clause names that exist now."""
    found: list[str] = []
    for pattern in clause.params.get("databases", []):
        if not any(ch in pattern for ch in "*?["):
            rel = normalize_path(pattern, repo)
            if (repo / rel).is_file():
                found.append(rel)
            continue
        regex, base = compile_glob(pattern), repo / static_prefix(pattern)
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                rel = path.relative_to(repo).as_posix()
                if path.is_file() and regex.match(rel):
                    found.append(rel)
    return list(dict.fromkeys(found))


def _stamp(path: Path) -> list[int]:
    out = []
    for p in (path, Path(f"{path}-wal")):
        st = p.stat() if p.exists() else None
        out += [st.st_mtime_ns, st.st_size] if st else [0, 0]
    return out


class Snapshot:
    """Copies of a clause's databases taken at task start (in state_dir/sqlite/<clause>)."""

    def __init__(self, state_dir: Path, clause: Clause):
        self.dir = state_dir / "sqlite" / clause.id
        self.manifest_path = self.dir / "manifest.json"
        self.manifest: dict[str, Any] = (
            json.loads(self.manifest_path.read_text(encoding="utf-8")) if self.manifest_path.exists() else {}
        )

    def take(self, clause: Clause, repo: Path) -> int:
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self.manifest = {"databases": {}, "seen": {}}
        for n, rel in enumerate(database_files(clause, repo)):
            copy = self.dir / f"{n}.db"
            with closing(sqlite3.connect(repo / rel)) as src, closing(sqlite3.connect(copy)) as dst:
                src.backup(dst)
            self.manifest["databases"][rel] = str(copy)
            self.manifest["seen"][rel] = _stamp(repo / rel)
        self._save()
        return len(self.manifest["databases"])

    def _save(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")

    def unchanged(self, rel: str, repo: Path) -> bool:
        path = repo / rel
        return path.is_file() and self.manifest.get("seen", {}).get(rel) == _stamp(path)

    def mark_seen(self, rel: str, repo: Path) -> None:
        path = repo / rel
        if path.is_file():
            self.manifest.setdefault("seen", {})[rel] = _stamp(path)
            self._save()


# ----- comparing ---------------------------------------------------------------


def _tables(conn: sqlite3.Connection, schema: str) -> dict[str, str]:
    rows = conn.execute(
        f"SELECT name, sql FROM {schema}.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {name: sql for name, sql in rows}


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[tuple[str, str, int]]:
    """(name, declared type, pk position) for each column."""
    return [(r[1], r[2], r[5]) for r in conn.execute(f"PRAGMA {schema}.table_info({q(table)})")]


def _key(conn: sqlite3.Connection, table: str) -> list[str]:
    pk = sorted((c for c in _columns(conn, "snap", table) if c[2]), key=lambda c: c[2])
    return [c[0] for c in pk] or ["rowid"]


def _match(key: list[str]) -> str:
    return " AND ".join(f"m.{q(k)} IS s.{q(k)}" for k in key)


def _open(live: Path, snapshot: str) -> sqlite3.Connection:
    conn = sqlite3.connect(live, timeout=10, isolation_level=None)
    conn.execute("ATTACH DATABASE ? AS snap", (snapshot,))
    return conn


def diff(live: Path, snapshot: str, tables: list[str] | None = None, forbid_updates: bool = False) -> list[dict[str, Any]]:
    """What the snapshot has that the live database lost."""
    if not live.is_file():
        return [{"operation": "delete_database"}]
    out: list[dict[str, Any]] = []
    with closing(_open(live, snapshot)) as conn:
        before, after = _tables(conn, "snap"), _tables(conn, "main")
        for table in before:
            if tables and table not in tables:
                continue
            if table not in after:
                count = conn.execute(f"SELECT count(*) FROM snap.{q(table)}").fetchone()[0]
                out.append({"operation": "drop_table", "table": table, "rows": count})
                continue
            live_cols = {c[0] for c in _columns(conn, "main", table)}
            snap_cols = [c[0] for c in _columns(conn, "snap", table)]
            dropped = [c for c in snap_cols if c not in live_cols]
            if dropped:
                out.append({"operation": "drop_column", "table": table, "columns": dropped})
            key = _key(conn, table)
            if any(k not in live_cols and k != "rowid" for k in key):
                continue  # the key itself is gone; drop_column covers it
            keys = ", ".join(f"s.{q(k)}" for k in key)
            missing = f"FROM snap.{q(table)} s WHERE NOT EXISTS (SELECT 1 FROM main.{q(table)} m WHERE {_match(key)})"
            count = conn.execute(f"SELECT count(*) {missing}").fetchone()[0]
            if count:
                sample = [list(r) for r in conn.execute(f"SELECT {keys} {missing} LIMIT {SAMPLE}")]
                out.append({"operation": "delete_rows", "table": table, "rows": count, "key": key, "sample": sample})
            if forbid_updates:
                common = [c for c in snap_cols if c in live_cols and c not in key]
                if common:
                    changed_where = " OR ".join(f"m.{q(c)} IS NOT s.{q(c)}" for c in common)
                    changed = f"FROM snap.{q(table)} s JOIN main.{q(table)} m ON {_match(key)} WHERE {changed_where}"
                    count = conn.execute(f"SELECT count(*) {changed}").fetchone()[0]
                    if count:
                        sample = [list(r) for r in conn.execute(f"SELECT {keys} {changed} LIMIT {SAMPLE}")]
                        out.append({"operation": "update_rows", "table": table, "rows": count, "key": key, "sample": sample})
    return out


def restore(live: Path, snapshot: str, findings: list[dict[str, Any]]) -> bool:
    """Put lost data back from the snapshot. True when every finding was repaired."""
    if any(f["operation"] == "delete_database" for f in findings):
        live.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{live}{suffix}").unlink(missing_ok=True)
        with closing(sqlite3.connect(snapshot)) as src, closing(sqlite3.connect(live)) as dst:
            src.backup(dst)
        return True
    with closing(_open(live, snapshot)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Order matters: tables, then columns, then rows, then values.
            order = ["drop_table", "drop_column", "delete_rows", "update_rows"]
            for f in sorted(findings, key=lambda f: order.index(f["operation"])):
                table, op = f["table"], f["operation"]
                cols = [c[0] for c in _columns(conn, "snap", table)]
                names = ", ".join(q(c) for c in cols)
                if op == "drop_table":
                    sql = _tables(conn, "snap")[table]
                    conn.execute(sql)
                    conn.execute(f"INSERT INTO main.{q(table)} ({names}) SELECT {names} FROM snap.{q(table)}")
                elif op == "drop_column":
                    types = {c[0]: c[1] for c in _columns(conn, "snap", table)}
                    key = _key(conn, table)
                    for col in f["columns"]:
                        conn.execute(f"ALTER TABLE main.{q(table)} ADD COLUMN {q(col)} {types[col]}")
                        conn.execute(
                            f"UPDATE main.{q(table)} AS m SET {q(col)} = "
                            f"(SELECT s.{q(col)} FROM snap.{q(table)} s WHERE {_match(key)})"
                        )
                elif op == "delete_rows":
                    key = _key(conn, table)
                    live_cols = {c[0] for c in _columns(conn, "main", table)}
                    keep = [c for c in cols if c in live_cols]
                    if "rowid" in key:
                        keep = ["rowid", *keep]
                    names = ", ".join(q(c) if c != "rowid" else "rowid" for c in keep)
                    picks = ", ".join(f"s.{q(c)}" if c != "rowid" else "s.rowid" for c in keep)
                    conn.execute(
                        f"INSERT INTO main.{q(table)} ({names}) SELECT {picks} FROM snap.{q(table)} s "
                        f"WHERE NOT EXISTS (SELECT 1 FROM main.{q(table)} m WHERE {_match(key)})"
                    )
                elif op == "update_rows":
                    key = _key(conn, table)
                    live_cols = {c[0] for c in _columns(conn, "main", table)}
                    for col in (c for c in cols if c in live_cols and c not in key):
                        conn.execute(
                            f"UPDATE main.{q(table)} AS m SET {q(col)} = "
                            f"(SELECT s.{q(col)} FROM snap.{q(table)} s WHERE {_match(key)}) "
                            f"WHERE EXISTS (SELECT 1 FROM snap.{q(table)} s WHERE {_match(key)})"
                        )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
    return True


HINTS = {
    "delete_database": "Don't delete the database file; add a new database or table instead",
    "drop_table": "Keep the table; if it's obsolete, rename the code that uses it and leave the data",
    "drop_column": "Keep the column; stop writing to it instead, and leave removal to a human-reviewed migration",
    "delete_rows": "Don't delete existing rows; mark them (for example a deleted_at column) or filter them in queries",
    "update_rows": "Don't change existing rows; write new rows, or ask the human",
}


def check(clause: Clause, repo: Path, state_dir: Path, rollback: bool, apply: bool = True) -> list[ViolationReport]:
    """Compare every database with its snapshot; optionally put lost data back."""
    snap = Snapshot(state_dir, clause)
    tables = clause.params.get("tables") or None
    forbid_updates = bool(clause.params.get("forbid_updates", False))
    reports: list[ViolationReport] = []
    for rel, copy in snap.manifest.get("databases", {}).items():
        live = repo / rel
        if snap.unchanged(rel, repo):
            continue
        findings = diff(live, copy, tables, forbid_updates)
        if not findings:
            if apply:
                snap.mark_seen(rel, repo)
            continue
        rolled_back: bool | str = False
        if rollback and apply:
            try:
                rolled_back = restore(live, copy, findings)
                if rolled_back and not diff(live, copy, tables, forbid_updates):
                    snap.mark_seen(rel, repo)
                else:
                    rolled_back = False
            except (sqlite3.Error, OSError) as exc:
                rolled_back = f"failed: {exc}"
        for f in findings:
            evidence = {"database": rel, **f}
            if rollback:
                evidence["rolled_back"] = rolled_back if apply else False
            if not apply:
                evidence["pending"] = True
            hints = [HINTS[f["operation"]]]
            if evidence.get("rolled_back") is True:
                hints.insert(0, f"{rel} is protected, so Covenant put the lost data back from its snapshot")
            reports.append(ViolationReport.for_clause(clause, evidence, hints=hints))
    return reports

