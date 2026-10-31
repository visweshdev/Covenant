"""A minimal MCP server (stdio, JSON-RPC 2.0) so any MCP-capable agent can work with Covenant.

Agents without Claude Code-style hooks (Cursor, Codex, others) use these tools to see the
rules, run tests in a way Covenant records, and check a commit before making it. The git
hooks still enforce commits and pushes, whatever the agent does.

Run: covenant --repo <path> mcp
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from .. import __version__
from ..events import Commit, Edit, Push
from ..monitors.effects import normalize_path
from ..runtime import Decision, Runtime
from .claude_code import ClaudeCodeHook

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

TOOLS = [
    {
        "name": "covenant_rules",
        "description": "The rules this repository enforces on coding agents, and how to run its tests. Read this before starting work.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "covenant_run_tests",
        "description": "Run the repository's test command and record the result. A passing run is required before committing.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "covenant_record_edit",
        "description": "Tell Covenant you changed a file (for agents whose edits Covenant can't see).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path of the file you changed"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "covenant_preflight",
        "description": "Check whether a commit or push would be allowed right now, without doing it. Returns the reasons if not.",
        "inputSchema": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["commit", "push"]}},
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "covenant_check",
        "description": "Run every Covenant check on the current state (API surface, protected data, integrity).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "covenant_check_command",
        "description": "Check whether a shell command is allowed before running it (force pushes, destructive SQL, "
                       "network access and the other refused commands). Returns the reasons if not.",
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The command you plan to run"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "covenant_explain",
        "description": "Why was something blocked, and what can be done about it, in plain words.",
        "inputSchema": {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 10}},
                        "additionalProperties": False},
    },
]


def _decision_text(decision: Decision, ok: str) -> tuple[str, bool]:
    if not decision.reports:
        return ok, False
    return ClaudeCodeHook._render(f"Covenant: {decision.event} has problems", decision.reports), True


class CovenantMcpServer:
    def __init__(self, repo: Path, covenant: str | None = None, state_dir: str | None = None):
        self.repo, self.covenant, self.state_dir = repo, covenant, state_dir

    def _runtime(self) -> Runtime:
        runtime = Runtime.open(self.repo, covenant_path=self.covenant, state_dir=self.state_dir)
        if runtime is None:
            raise ValueError(f"no covenant.yaml in {self.repo}; run `covenant compile` first")
        if not runtime.has_session:
            runtime.start()
        return runtime

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        rt = self._runtime()
        if name == "covenant_rules":
            return ClaudeCodeHook(rt).rules_summary(), False
        if name == "covenant_run_tests":
            parts, failed = [], False
            for decision, output in rt.run_tests():
                last = rt.trace[-1]  # run_tests just recorded this run
                passed = last.type == "test" and last.exit_code == 0
                parts.append(f"{'PASSED' if passed else 'FAILED'}\n{output[-6000:]}")
                if decision.reports:
                    parts.append(ClaudeCodeHook._render("Covenant", decision.reports))
                failed |= not passed or bool(decision.reports)
            return "\n".join(parts), failed
        if name == "covenant_record_edit":
            decision = rt.record(Edit(path=normalize_path(arguments["path"], rt.repo_root)))
            return _decision_text(decision, "Recorded. Run the tests again before committing.")
        if name == "covenant_preflight":
            event = Commit() if arguments.get("action") == "commit" else Push()
            decision = rt.record(event, dry_run=True)
            return _decision_text(decision, f"A {event.type} would be allowed now.")
        if name == "covenant_check":
            return _decision_text(rt.check("mcp"), "All Covenant checks pass.")
        if name == "covenant_check_command":
            reports = rt.command_reports(arguments["command"]) + rt.budget_reports(count=False)
            if not reports:
                return "Allowed: no rule refuses this command.", False
            rt.log_refusal("refused this command (MCP check)", reports)
            return ClaudeCodeHook._render("Covenant would refuse this command", reports), True
        if name == "covenant_explain":
            from .. import explain

            entries = [json.loads(line) for line in rt._read_lines("reports.jsonl")]
            return explain.explain(entries, int(arguments.get("count", 1))), False
        raise KeyError(name)

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method, msg_id = message.get("method"), message.get("id")
        if msg_id is None:  # a notification, e.g. notifications/initialized
            return None
        try:
            if method == "initialize":
                requested = (message.get("params") or {}).get("protocolVersion")
                version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result: dict[str, Any] = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "covenant", "version": __version__},
                    "instructions": "Call covenant_rules first. Run tests with covenant_run_tests so they count.",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = message.get("params") or {}
                try:
                    text, is_error = self.call_tool(params.get("name", ""), params.get("arguments") or {})
                except KeyError:
                    return _error(msg_id, -32602, f"unknown tool {params.get('name')!r}")
                except Exception as exc:  # report tool failures to the agent, keep serving
                    text, is_error = f"Covenant error: {type(exc).__name__}: {exc}", True
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            else:
                return _error(msg_id, -32601, f"method not found: {method}")
        except Exception as exc:
            return _error(msg_id, -32603, str(exc))
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                reply = _error(None, -32700, "parse error")
            else:
                reply = self.handle(message)
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
