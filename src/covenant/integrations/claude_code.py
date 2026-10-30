"""Claude Code integration: turns Claude Code hook calls into Covenant events.

    SessionStart        start (or resume) a Covenant session; tell Claude the rules
    PreToolUse          deny commits/pushes that would break a rule, writes to protected
                        files, and anything that touches Covenant's own files
    PostToolUse         record edits, commands and passing test runs; report effects
    PostToolUseFailure  record failing commands and failing test runs
    Stop                task-end checkpoint: Claude can't finish while a rule is broken

The handler fails closed: if it crashes on PreToolUse, the tool call is denied.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..events import Commit, Edit, Effect, Push, Run, TestRun
from ..feedback import ViolationReport
from ..monitors.effects import normalize_path
from ..paths import control_paths, is_under
from ..runtime import Decision, Runtime

FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Claude Code on Windows also has a PowerShell tool; it must get the same checks as Bash.
SHELL_TOOLS = {"Bash", "PowerShell"}
SHELL_MATCHER = "|".join(sorted(SHELL_TOOLS))
TOOL_MATCHER = f"{SHELL_MATCHER}|Write|Edit|MultiEdit|NotebookEdit"
MAX_REASON = 9000

# `git [global options] commit|push`, anywhere in a compound command.
_GIT_ACTION = re.compile(
    r"(?:^|[;&|(`\s])git(?:\s+(?:-[cC]\s+\S+|--git-dir(?:=|\s+)\S+|--work-tree(?:=|\s+)\S+|--?[\w-]+(?:=\S+)?))*\s+(commit|push)\b"
)
_BYPASS = re.compile(r"--no-verify|core\.hooksPath|disableAllHooks|\bgit\b[^;&|]*\bcommit\b[^;&|]*\s-n\b")
_WRITES = re.compile(
    r"(>|\brm\b|\bmv\b|\bcp\b|\bsed\s+-i|\btee\b|\bchmod\b|\btruncate\b|\bdel\b|\bln\b|\bdd\b|"
    r"Remove-Item|Set-Content|Out-File|Move-Item|Copy-Item|\bunlink\b|\.write_text|\.unlink|os\.remove|shutil\.)"
)
_CHAINING = re.compile(r"\|\||(?<!\|)\|(?!\|)|(?<!&)&(?!&)")


def _segments(command: str) -> list[str]:
    return [s.strip() for s in re.split(r"&&|;|\n", command) if s.strip()]


def _same_command(a: str, b: str) -> bool:
    try:
        return shlex.split(a) == shlex.split(b)
    except ValueError:
        return " ".join(a.split()) == " ".join(b.split())


def is_test_run(command: str, test_cmds: list[str]) -> str | None:
    """The test command this Bash call runs, if its exit status reflects the tests.

    Pipes, `||` and background `&` can hide a failure, so those never count.
    """
    if _CHAINING.search(command):
        return None
    for segment in _segments(command):
        for cmd in test_cmds:
            if _same_command(segment, cmd):
                return cmd
    return None


def git_actions(command: str) -> list[str]:
    return [m.group(1) for m in _GIT_ACTION.finditer(command)]


class ClaudeCodeHook:
    def __init__(self, runtime: Runtime):
        self.rt = runtime
        self.controls = control_paths(runtime.repo_root, runtime.covenant_path, runtime.state_dir)
        self.controls += [Path(p) for p in runtime.session.get("policy_files", [])]  # team policies too

    # ----- dispatch ----------------------------------------------------------

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = payload.get("hook_event_name")
        handler = {
            "SessionStart": self.session_start,
            "PreToolUse": self.pre_tool_use,
            "PostToolUse": self.post_tool_use,
            "PostToolUseFailure": self.post_tool_use_failure,
            "Stop": self.stop,
        }.get(event or "")
        return handler(payload) if handler else {}

    # ----- events ------------------------------------------------------------

    def session_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        notes = []
        source = payload.get("source", "startup")
        fresh = source in ("startup", "clear") and self.rt.session.get("claude_session_id") != payload.get("session_id")
        if not self.rt.has_session or fresh:
            if self.rt.repo_covenant_changed():
                notes.append(
                    "covenant.yaml changed since it was last approved, so the previously approved version stays "
                    "in force. Run `covenant start` to approve the new one."
                )
                self.rt.update_session(claude_session_id=payload.get("session_id"))
            else:
                self.rt.start({"claude_session_id": payload.get("session_id")})
        context = self.rules_summary()
        out: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
        rules = len(self.rt.covenant.enforced())
        out["systemMessage"] = " ".join([f"Covenant: {rules} rules enforced."] + notes)
        return out

    def pre_tool_use(self, payload: dict[str, Any]) -> dict[str, Any]:
        tool = payload.get("tool_name", "")
        tool_input = payload.get("tool_input") or {}
        if tool in FILE_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            if path and is_under(path, self.controls):
                return self._deny(f"Covenant: {path} belongs to Covenant's own setup and can't be changed by the agent.")
            if path and Path(path).exists():
                reports = self.rt.would_violate(Effect(op="overwrite", target=path))
                if reports:
                    return self._deny(self._render("Covenant denied this edit", reports))
            return {}
        if tool in SHELL_TOOLS:
            command = tool_input.get("command", "")
            if _BYPASS.search(command):
                return self._deny("Covenant: commands that skip or redirect git hooks or disable hooks are not allowed.")
            if _WRITES.search(command) and self._mentions_control_path(command):
                return self._deny("Covenant: this command would modify Covenant's own files, which only a human can change.")
            for action in git_actions(command):
                decision = self.rt.record(Commit() if action == "commit" else Push(), dry_run=True)
                if not decision.allowed:
                    return self._deny(self._render(f"Covenant blocked git {action}", decision.blocking))
        return {}

    def post_tool_use(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._after_tool(payload, succeeded=True)

    def post_tool_use_failure(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._after_tool(payload, succeeded=False)

    def stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        decision = self.rt.check("task-end")
        blocking = decision.blocking
        if not blocking:
            return {}
        if any(r.escalated or r.on_violation.value == "ask_human" for r in blocking):
            return {"systemMessage": self._render("Covenant: these violations need a human before this task is done", blocking)}
        return {"decision": "block", "reason": self._render("Covenant: the task can't end while rules are broken", blocking)}

    # ----- helpers -----------------------------------------------------------

    def _after_tool(self, payload: dict[str, Any], succeeded: bool) -> dict[str, Any]:
        tool = payload.get("tool_name", "")
        tool_input = payload.get("tool_input") or {}
        if tool in FILE_TOOLS:
            if not succeeded:
                return {}
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            decision = self.rt.record(Edit(path=normalize_path(path, self.rt.repo_root)))
        elif tool in SHELL_TOOLS:
            command = tool_input.get("command", "")
            exit_code = 0 if succeeded else 1
            test_cmd = is_test_run(command, self.rt.test_commands)
            event = TestRun(cmd=test_cmd, exit_code=exit_code) if test_cmd else Run(cmd=command, exit_code=exit_code)
            decision = self.rt.record(event)
        else:
            return {}
        return self._feedback(decision, payload.get("hook_event_name", "PostToolUse"))

    def _feedback(self, decision: Decision, event_name: str) -> dict[str, Any]:
        if not decision.reports:
            return {}
        text = self._render("Covenant found a problem after this step", decision.reports)
        return {"decision": "block", "reason": text}

    def _mentions_control_path(self, command: str) -> bool:
        lowered = command.replace("\\", "/").lower()
        needles = {"covenant.yaml", ".git/hooks", ".git/config", ".claude/settings", ".covenant"}
        needles |= {str(p).replace("\\", "/").lower() for p in self.controls}
        needles.add(self.rt.covenant_path.name.lower())
        return any(n in lowered for n in needles)

    def rules_summary(self) -> str:
        lines = ["This repository is governed by Covenant. These rules are enforced by real checks, not by trust:"]
        for c in self.rt.covenant.enforced():
            lines.append(f"- {c.id}: {c.source_span}")
        for c in self.rt.covenant.clauses:
            if c not in self.rt.covenant.enforced():
                lines.append(f"- {c.id} (not automatically checked; follow it anyway): {c.source_span}")
        if self.rt.test_commands:
            cmds = " or ".join(f"`{c}`" for c in self.rt.test_commands)
            lines.append(f"Run tests with exactly {cmds}, on its own (no pipes or `||`), so Covenant records the result.")
        lines.append("If Covenant blocks something, read its report: it names the rule, the evidence and how to fix it.")
        return "\n".join(lines)

    @staticmethod
    def _deny(reason: str) -> dict[str, Any]:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason[:MAX_REASON],
            }
        }

    @staticmethod
    def _render(header: str, reports: list[ViolationReport]) -> str:
        lines = [header + ":"]
        for r in reports:
            hint = f" Fix: {r.repair_hints[0]}" if r.repair_hints else ""
            esc = " (repeated; ask the human)" if r.escalated else ""
            lines.append(f"- {r.violation} ({r.clause}){esc}.{hint}")
        details = json.dumps([r.model_dump(mode="json", exclude_defaults=True) for r in reports], indent=1)
        text = "\n".join(lines) + "\nDetails:\n" + details
        return text if len(text) <= MAX_REASON else text[: MAX_REASON - 20] + "\n...(truncated)"


def run_hook(stdin_text: str, repo: Path | None, state_dir: str | None, covenant: str | None) -> tuple[dict[str, Any], int]:
    """Entry point for `covenant claude-hook`. Returns (json output, exit code)."""
    try:
        payload = json.loads(stdin_text or "{}")
    except ValueError:
        payload = {}
    event = payload.get("hook_event_name", "")
    try:
        root = repo or Path(payload.get("cwd") or ".")
        runtime = Runtime.open(root, covenant_path=covenant, state_dir=state_dir)
        if runtime is None:
            return {}, 0
        return ClaudeCodeHook(runtime).handle(payload), 0
    except Exception as exc:  # fail closed where it matters
        message = f"Covenant hook error ({type(exc).__name__}: {exc})."
        if event == "PreToolUse":
            return ClaudeCodeHook._deny(message + " Failing closed; ask the human to check Covenant."), 0
        return {"systemMessage": message}, 0


# ----- installation ----------------------------------------------------------


def claude_version() -> tuple[int, ...] | None:
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def importable_outside_source() -> bool:
    """Whether `python -m covenant` works from any folder (i.e. the package is installed)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        result = subprocess.run([sys.executable, "-c", "import covenant"], cwd=tmp, env=env, capture_output=True)
    return result.returncode == 0


def _is_ours(group: dict[str, Any]) -> bool:
    def ours(h: dict[str, Any]) -> bool:
        text = " ".join([str(h.get("command", ""))] + [str(a) for a in h.get("args", [])])
        return "claude-hook" in text and "covenant" in text

    return any(ours(h) for h in group.get("hooks", []))


def hook_args(repo: Path, state_dir: Path | None) -> list[str]:
    args = ["-m", "covenant", "--repo", repo.as_posix()]
    if state_dir:
        args += ["--state-dir", state_dir.as_posix()]
    return args + ["claude-hook"]


def hook_handler(repo: Path, state_dir: Path | None) -> dict[str, Any]:
    """The hook entry. Claude Code runs shell-form hooks in Git Bash or PowerShell, and a
    command that starts with a quoted path is a syntax error in PowerShell, so the
    interpreter path goes first unquoted. Paths with spaces need exec form instead."""
    python = Path(sys.executable).as_posix()
    args = hook_args(repo, state_dir)
    if " " in python:
        return {"type": "command", "command": python, "args": args}
    quoted = " ".join(a if a.startswith("-") or a == "claude-hook" else f'"{a}"' for a in args)
    return {"type": "command", "command": f"{python} {quoted}"}


def install(repo: Path, settings_file: Path, state_dir: Path | None = None, on_failure_block: bool | None = None) -> Path:
    """Add Covenant's hooks to a Claude Code settings file, keeping everything else."""
    if on_failure_block is None:
        version = claude_version()
        on_failure_block = version is not None and version >= (2, 1, 295)
    data: dict[str, Any] = {}
    if settings_file.exists():
        data = json.loads(settings_file.read_text(encoding="utf-8") or "{}")
    hooks = data.setdefault("hooks", {})
    plan = [
        ("SessionStart", None),
        ("PreToolUse", TOOL_MATCHER),
        ("PostToolUse", TOOL_MATCHER),
        ("PostToolUseFailure", SHELL_MATCHER),
        ("Stop", None),
    ]
    for event, matcher in plan:
        groups = [g for g in hooks.get(event, []) if not _is_ours(g)]
        handler: dict[str, Any] = {**hook_handler(repo, state_dir), "timeout": 300}
        if on_failure_block and event == "PreToolUse":
            handler["onFailure"] = "block"
        group: dict[str, Any] = {"hooks": [handler]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        hooks[event] = groups
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return settings_file
