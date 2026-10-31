"""The hook server: Covenant as a long-running process the agent's environment calls over HTTP.

    covenant --repo . serve [--port 8787]

Claude Code's HTTP hooks POST each hook event to /hook, and the git hooks POST to
/git/pre-commit and /git/pre-push. State, snapshots and the approved Covenant stay with this
process, so when the agent runs in a container or VM, the folder that holds them doesn't have to
be mounted where the agent can reach it. The repo itself must be shared (for example, a bind
mount) so the server can check and restore files.

Every request needs `Authorization: Bearer <token>`. The token is created at start and
written to the state folder; it keeps other local processes from posting events, but an
agent that can read the hook configuration can read it too, so it is not a defense against
the agent itself. Isolation against the agent comes from running the agent where neither the
token's file nor the state folder is visible.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .events import Commit, Push
from .integrations.claude_code import ClaudeCodeHook
from .runtime import Runtime

MAX_BODY = 4 * 1024 * 1024


def token_path(state_dir: Path) -> Path:
    return state_dir / "server-token"


def ensure_token(state_dir: Path) -> str:
    path = token_path(state_dir)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    state_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token


class HookServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], repo: Path, covenant: str | None, state_dir: str | None):
        super().__init__(address, Handler)
        runtime = Runtime.open(repo, covenant_path=covenant, state_dir=state_dir)
        if runtime is None:
            raise ValueError(f"no covenant.yaml in {repo}")
        self.repo, self.covenant, self.state_dir_arg = repo, covenant, state_dir
        self.state_dir = runtime.state_dir
        # The sandbox hands the server its token, so it can write the agent's settings beforehand.
        self.token = os.environ.get("COVENANT_HOOK_TOKEN") or ensure_token(self.state_dir)

        self.last_request = time.monotonic()
        self.code_stamp = code_stamp()

    def runtime(self) -> Runtime:
        runtime = Runtime.open(self.repo, covenant_path=self.covenant, state_dir=self.state_dir_arg)
        assert runtime is not None
        return runtime


class Handler(BaseHTTPRequestHandler):
    server: HookServer
    server_version = "covenant-hooks"

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, status: int, data: Any) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        given = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        return bool(given) and hmac.compare_digest(given, self.server.token)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(HTTPStatus.OK, {"ok": True})
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.close_connection = True
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too large"})
            return
        body = self.rfile.read(length)  # read first: replying early makes Windows reset the connection
        self.server.last_request = time.monotonic()
        if getattr(self.server, "daemon_mode", False) and code_stamp() != self.server.code_stamp:
            # Covenant was upgraded under us: let the client fall back to a fresh process, and quit.
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "covenant code changed; restarting"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "missing or wrong token"})
            return
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        try:
            if self.path == "/hook":
                self._send(HTTPStatus.OK, ClaudeCodeHook(self.server.runtime()).handle(payload))
            elif self.path in ("/git/pre-commit", "/git/pre-push"):
                runtime = self.server.runtime()
                decision = runtime.record(Commit() if self.path.endswith("commit") else Push())
                lines = [f"{r.violation} ({r.clause}): {r.repair_hints[0] if r.repair_hints else ''}" for r in decision.blocking]
                self._send(HTTPStatus.OK, {"allowed": decision.allowed, "message": "\n".join(lines)})
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as exc:
            # Fail closed: Claude Code treats a non-2xx response as a hook failure, and the git
            # hook script refuses the commit when the server can't answer.
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})


def code_stamp() -> tuple[int, int]:
    """Changes when Covenant's own code changes (an upgrade or an edit)."""
    root = Path(__file__).parent
    files = list(root.rglob("*.py"))
    return len(files), max((f.stat().st_mtime_ns for f in files), default=0)


def daemon_file(state_dir: Path) -> Path:
    return state_dir / "daemon.json"


def _alive(info: dict[str, Any]) -> bool:
    import http.client

    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=2)
        conn.request("GET", "/health")
        ok = conn.getresponse().status == 200
        conn.close()
        return ok
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        return False


def run_daemon(repo: Path, covenant: str | None, state_dir: str | None, idle_seconds: int = 1800) -> int:
    """A per-repo hook server on a random local port, found through <state>/daemon.json.

    Started on demand by the fast hook client; exits after `idle_seconds` without requests,
    or when Covenant's code changes.
    """
    httpd = HookServer(("127.0.0.1", 0), repo, covenant, state_dir)
    httpd.daemon_mode = True
    path = daemon_file(httpd.state_dir)
    try:
        if path.is_file() and _alive(json.loads(path.read_text(encoding="utf-8"))):
            httpd.server_close()
            return 0  # another daemon already serves this repo
    except ValueError:
        pass
    info = {"port": httpd.server_address[1], "token": httpd.token, "pid": os.getpid()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(info), encoding="utf-8")
    os.replace(tmp, path)

    def watchdog() -> None:
        while True:
            time.sleep(min(30, idle_seconds))
            if time.monotonic() - httpd.last_request > idle_seconds:
                httpd.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass
    return 0


def git_hook_script(name: str, url: str, token: str) -> str:
    """A git hook that asks the server; it refuses the action if the server is unreachable."""
    return (
        "#!/bin/sh\n"
        "# Installed by covenant (server mode). Refuses the action if the Covenant server can't be reached.\n"
        f'reply=$(curl -s -f -X POST -H "Authorization: Bearer {token}" -H "Content-Type: application/json" '
        f'-d "{{}}" "{url}/git/{name}") || {{ echo "covenant: server unreachable at {url}; refusing" >&2; exit 1; }}\n'
        'case "$reply" in *\'"allowed": true\'*) exit 0;; esac\n'
        'echo "covenant: refused:" >&2\n'
        'echo "$reply" >&2\n'
        "exit 1\n"
    )


def http_hooks_settings(url: str, token: str) -> dict[str, Any]:
    """Claude Code settings that send every hook event to the server."""
    from .integrations.claude_code import PRE_MATCHER, TOOL_MATCHER

    handler = {"type": "http", "url": f"{url}/hook", "headers": {"Authorization": f"Bearer {token}"}, "timeout": 300}
    matcher = TOOL_MATCHER
    return {
        "hooks": {
            "SessionStart": [{"hooks": [handler]}],
            "PreToolUse": [{"matcher": PRE_MATCHER, "hooks": [handler]}],
            "PostToolUse": [{"matcher": matcher, "hooks": [handler]}],
            "PostToolUseFailure": [{"matcher": "Bash|PowerShell", "hooks": [handler]}],
            "Stop": [{"hooks": [handler]}],
        }
    }


def _ours(group: dict[str, Any]) -> bool:
    from .integrations.claude_code import _is_ours

    if _is_ours(group):
        return True
    return any(h.get("type") == "http" and str(h.get("url", "")).endswith("/hook")
               and str(h.get("headers", {}).get("Authorization", "")).startswith("Bearer ") for h in group.get("hooks", []))


def install(repo: Path, url: str, token: str, settings_file: Path) -> None:
    """Point Claude Code (HTTP hooks) and git (curl hooks) at the server, keeping other hooks."""
    data: dict[str, Any] = json.loads(settings_file.read_text(encoding="utf-8") or "{}") if settings_file.exists() else {}
    hooks = data.setdefault("hooks", {})
    for event, groups in http_hooks_settings(url, token)["hooks"].items():
        hooks[event] = [g for g in hooks.get(event, []) if not _ours(g)] + groups
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    git_hooks = repo / ".git" / "hooks"
    if git_hooks.is_dir():
        for name in ("pre-commit", "pre-push"):
            path = git_hooks / name
            path.write_text(git_hook_script(name, url, token), encoding="utf-8", newline="\n")
            path.chmod(0o755)
