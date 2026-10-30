"""A read-only local dashboard of every repo Covenant enforces on this machine.

    covenant dashboard [--port 8765]

Serves on 127.0.0.1 only, answers GET only, and rejects requests whose Host header isn't
localhost (so a web page can't reach it through DNS rebinding). It reads Covenant's state
folders; it never changes them.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .paths import covenant_home
from .runtime import Runtime
from .status import status_json

PAGE = Path(__file__).with_name("dashboard.html")
_ID = re.compile(r"^[\w.-]+$")


def workspaces_dir() -> Path:
    return covenant_home() / "workspaces"


def _lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def list_workspaces() -> list[dict[str, Any]]:
    out = []
    root = workspaces_dir()
    if not root.is_dir():
        return out
    for folder in sorted(root.iterdir()):
        session_file = folder / "session.json"
        if not session_file.is_file():
            continue
        try:
            session = json.loads(session_file.read_text(encoding="utf-8"))
        except ValueError:
            continue
        reports = [json.loads(line) for line in _lines(folder / "reports.jsonl")]
        blocks: Counter[str] = Counter()
        for entry in reports:
            blocks.update(entry.get("counted", []))
        last = max((_mtime(folder / n) for n in ("session.json", "trace.jsonl", "reports.jsonl")), default=0.0)
        repo = Path(session.get("repo", ""))
        out.append({
            "id": folder.name,
            "repo": str(repo),
            "name": repo.name or folder.name,
            "repo_exists": repo.is_dir(),
            "started_at": session.get("started_at"),
            "events": len(_lines(folder / "trace.jsonl")),
            "decisions_with_violations": len(reports),
            "blocks": dict(blocks),
            "last_activity": datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None,
        })
    out.sort(key=lambda w: w["last_activity"] or "", reverse=True)
    return out


def workspace_detail(workspace_id: str) -> dict[str, Any] | None:
    if not _ID.match(workspace_id):
        return None
    folder = workspaces_dir() / workspace_id
    session_file = folder / "session.json"
    if not session_file.is_file():
        return None
    repo = Path(json.loads(session_file.read_text(encoding="utf-8")).get("repo", ""))
    runtime = Runtime.open(repo, state_dir=folder)
    if runtime is None:
        return None
    data = status_json(runtime)
    data["recent_reports"] = runtime.recent_reports(200)
    data["last_events"] = [e.model_dump(mode="json") for e in runtime.trace[-50:]]
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "covenant-dashboard"

    def log_message(self, *args: Any) -> None:  # keep the terminal quiet
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if not self._host_ok():
            self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
            return
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/workspaces":
                self._json(list_workspaces())
            elif path.startswith("/api/workspaces/"):
                detail = workspace_detail(path.removeprefix("/api/workspaces/"))
                if detail is None:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(detail)
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # report, keep serving
            self._json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"read-only", "text/plain")

    do_PUT = do_DELETE = do_PATCH = do_POST


def serve(port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
