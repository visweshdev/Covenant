"""A local dashboard of every repo Covenant enforces on this machine.

    covenant dashboard [--port 8765] [--read-only]

Serves on 127.0.0.1 only and rejects requests whose Host header isn't localhost (so a web page
can't reach it through DNS rebinding). Reading needs nothing more.

Editing rules (checking, drafting from plain English, saving and approving) needs the token
created when the dashboard starts. The browser gets it in the part of the address after `#`,
which is never sent over the network; every change request must carry it in a header, and
requests from other web pages (a foreign Origin) are refused. The token lives only in this
process and the browser tab, so the agent can't read it from a file. An agent that can read
your screen or your terminal could; for isolation from the agent, run the agent elsewhere.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from . import policy
from .ir import Covenant
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


# ----- history: past sessions, replayed step by step ---------------------------------------

def _session_folder(workspace_id: str, session_id: str) -> Path | None:
    if not _ID.match(workspace_id) or not (session_id == "current" or _ID.match(session_id)):
        return None
    folder = workspaces_dir() / workspace_id
    folder = folder if session_id == "current" else folder / "history" / session_id
    return folder if (folder / "session.json").is_file() else None


def sessions(workspace_id: str) -> list[dict[str, Any]] | None:
    """The current task and the archived ones, newest first."""
    if not _ID.match(workspace_id) or not (workspaces_dir() / workspace_id / "session.json").is_file():
        return None
    folder = workspaces_dir() / workspace_id
    found = [("current", folder)]
    history = folder / "history"
    if history.is_dir():
        found += [(f.name, f) for f in sorted(history.iterdir(), reverse=True) if (f / "session.json").is_file()]
    out = []
    for sid, f in found:
        try:
            started = json.loads((f / "session.json").read_text(encoding="utf-8")).get("started_at")
        except ValueError:
            started = None
        reports = [json.loads(line) for line in _lines(f / "reports.jsonl")]
        out.append({"id": sid, "started_at": started, "events": len(_lines(f / "trace.jsonl")),
                    "problems": len(reports), "refused": sum(1 for r in reports if not r.get("allowed", True))})
    return out


def timeline(workspace_id: str, session_id: str) -> dict[str, Any] | None:
    """Every step of a session in order: what the agent did, and every decision Covenant made."""
    from . import explain

    folder = _session_folder(workspace_id, session_id)
    if folder is None:
        return None
    steps: list[dict[str, Any]] = []
    for line in _lines(folder / "trace.jsonl"):
        e = json.loads(line)
        detail = e.get("path") or e.get("cmd") or e.get("target") or e.get("sha") or e.get("remote") or ""
        if e.get("type") == "test":
            detail += " (passed)" if e.get("exit_code") == 0 else " (failed)"
        steps.append({"ts": e.get("ts"), "kind": "event", "type": e.get("type"), "detail": detail})
    for line in _lines(folder / "reports.jsonl"):
        d = json.loads(line)
        steps.append({
            "ts": d.get("ts"), "kind": "decision", "type": d.get("event"), "allowed": d.get("allowed", True),
            "rules": [{"id": r.get("violation"), "rule": r.get("clause"), "checker": r.get("checker"),
                       "why": explain.why(r), "fix": (r.get("repair_hints") or [""])[0],
                       "warn": r.get("on_violation") == "warn", "escalated": bool(r.get("escalated"))}
                      for r in d.get("reports", [])],
        })
    steps.sort(key=lambda s: s.get("ts") or "")
    session = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    rules = []
    approved = folder / "covenant.approved.yaml"
    if approved.is_file():
        try:
            rules = [{"id": c.id, "rule": c.source_span, "checker": c.checker}
                     for c in Covenant.load(approved).clauses]
        except (OSError, ValidationError, ValueError):
            rules = []
    return {"id": session_id, "started_at": session.get("started_at"), "steps": steps, "rules": rules}


# ----- editing ---------------------------------------------------------------------------

MAX_BODY = 1_000_000


def _workspace(workspace_id: str) -> tuple[Path, Path] | None:
    """(state folder, repo) for a workspace id."""
    if not _ID.match(workspace_id):
        return None
    folder = workspaces_dir() / workspace_id
    session_file = folder / "session.json"
    if not session_file.is_file():
        return None
    return folder, Path(json.loads(session_file.read_text(encoding="utf-8")).get("repo", ""))


def parse_rules(text: str) -> Covenant:
    data = yaml.safe_load(text)
    if isinstance(data, dict) and list(data) == ["covenant"]:
        data = data["covenant"]
    return Covenant.model_validate(data)


def rules_text(workspace_id: str) -> dict[str, Any] | None:
    found = _workspace(workspace_id)
    if not found:
        return None
    _, repo = found
    path = repo / "covenant.yaml"
    return {"path": str(path), "text": path.read_text(encoding="utf-8") if path.is_file() else ""}


def check_rules(workspace_id: str, text: str) -> dict[str, Any] | None:
    """Parse and validate edited rules, and say how they differ from the approved ones."""
    from .validate import validate

    found = _workspace(workspace_id)
    if not found:
        return None
    folder, repo = found
    try:
        cov = parse_rules(text)
    except (yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
        return {"ok": False, "issues": [{"clause_id": "", "level": "error", "message": str(exc)[:2000]}],
                "added": [], "changed": [], "removed": []}
    issues = [{"clause_id": i.clause_id, "level": i.level, "message": i.message} for i in validate(cov, repo)]
    approved_file = folder / "covenant.approved.yaml"
    approved = {c.id: c for c in Covenant.load(approved_file).clauses if not c.origin} if approved_file.is_file() else {}
    mine = {c.id: c for c in cov.clauses}
    return {
        "ok": not any(i["level"] == "error" for i in issues),
        "issues": issues,
        "added": [i for i in mine if i not in approved],
        "changed": [i for i in mine if i in approved and mine[i] != approved[i]],
        "removed": [i for i in approved if i not in mine],
    }


def save_rules(workspace_id: str, text: str, restart: bool) -> tuple[int, dict[str, Any]]:
    """Write covenant.yaml and approve it. Added rules apply from now on (like `covenant amend`);
    changed or removed rules need `restart`, which re-approves everything and starts the task over."""
    found = _workspace(workspace_id)
    report = check_rules(workspace_id, text)
    if not found or report is None:
        return HTTPStatus.NOT_FOUND, {"error": "not found"}
    if not report["ok"]:
        return HTTPStatus.BAD_REQUEST, {"error": "fix the errors first", **report}
    folder, repo = found
    if (report["changed"] or report["removed"]) and not restart:
        return HTTPStatus.CONFLICT, {"error": "changed or removed rules need a restart of the task", **report}
    path = repo / "covenant.yaml"
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    parse_rules(text).dump(path)
    runtime = Runtime.open(repo, path, folder)
    try:
        if runtime is None:
            raise ValueError("covenant.yaml could not be loaded")
        if restart or not runtime.has_session:
            runtime.start()
            return HTTPStatus.OK, {"approved": "all", **report}
        added = runtime.amend() if report["added"] else []
        return HTTPStatus.OK, {"approved": added, **report}
    except ValueError as exc:
        if before is not None:
            path.write_text(before, encoding="utf-8")  # leave the approved file in place
        return HTTPStatus.CONFLICT, {"error": str(exc), **report}


def draft_rules(workspace_id: str, instruction: str, offline: bool = False) -> dict[str, Any] | None:
    """Compile plain English into new rules, numbered after the existing ones. Nothing is saved."""
    from .cli import _renumber
    from .compiler.extract import CompileError, HeuristicExtractor, choose_extractor
    from .compiler.pipeline import compile_instruction

    found = _workspace(workspace_id)
    if not found:
        return None
    _, repo = found
    if not instruction:
        return {"error": "write the rule you want first"}
    try:
        extractor = HeuristicExtractor() if offline else choose_extractor()
        result = compile_instruction(instruction, repo, extractor, review=not offline)
    except CompileError as exc:
        return {"error": str(exc)}
    path = repo / "covenant.yaml"
    existing = [c.id for c in policy.load(path)[0].clauses] if path.is_file() else []
    new = _renumber(existing, result.covenant.clauses)
    if path.is_file():
        current = Covenant.load(path)
        combined = current.model_copy(update={"instruction": f"{current.instruction.rstrip()}\n{instruction}",
                                             "clauses": current.clauses + new})
    else:
        combined = result.covenant.model_copy(update={"clauses": new})
    text = yaml.safe_dump({"covenant": combined.model_dump(mode="json", exclude_defaults=True)},
                          sort_keys=False, allow_unicode=True)
    return {"text": text, "added": [c.id for c in new], "errors": [str(e) for e in result.errors],
            "summary": [{"id": c.id, "checker": c.checker, "status": c.status.value, "span": c.source_span} for c in new]}


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
            elif path == "/api/capabilities":
                self._json({"editable": bool(getattr(self.server, "token", None))})
            elif path.startswith("/api/workspaces/") and "/sessions" in path:
                ws, _, rest = path.removeprefix("/api/workspaces/").partition("/sessions")
                out = sessions(ws) if rest in ("", "/") else timeline(ws, rest.strip("/"))
                if out is None:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(out)
            elif path.startswith("/api/workspaces/") and path.endswith("/rules"):
                out = rules_text(path.removeprefix("/api/workspaces/").removesuffix("/rules"))
                if out is None:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(out)
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

    def _write_ok(self) -> str | None:
        """Why a change request is refused, or None when it may proceed."""
        token = getattr(self.server, "token", None)
        if not token:
            return "this dashboard is read-only"
        if not self._host_ok():
            return "forbidden host"
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return "requests from other pages are refused"
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return "JSON only"
        if not hmac.compare_digest(self.headers.get("X-Covenant-Token", ""), token):
            return "missing or wrong token; open the address the dashboard printed"
        return None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, MAX_BODY + 1))  # read first: replying early resets the connection on Windows
        refused = self._write_ok()
        if refused:
            self._json({"error": refused}, HTTPStatus.FORBIDDEN)
            return
        if length > MAX_BODY:
            self._json({"error": "too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            data = json.loads(body or b"{}")
            m = re.fullmatch(r"/api/workspaces/([\w.-]+)/(check|save|draft)", self.path)
            if not m or not isinstance(data, dict):
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            workspace, action = m.groups()
            if action == "save":
                status, out = save_rules(workspace, str(data.get("text", "")), bool(data.get("restart")))
                self._json(out, status)
                return
            if action == "check":
                result = check_rules(workspace, str(data.get("text", "")))
            else:
                result = draft_rules(workspace, str(data.get("instruction", "")).strip(), bool(data.get("offline")))
            if result is None:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            else:
                self._json(result)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:  # noqa: N802
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"not allowed", "text/plain")

    do_DELETE = do_PATCH = do_PUT


def serve(port: int = 8765, editable: bool = True) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.token = secrets.token_urlsafe(24) if editable else None  # type: ignore[attr-defined]
    return server
