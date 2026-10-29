# Smoke test: python scripts/hook_replay.py <repo set up with `covenant init`>. Windows (calls PowerShell).
"""Replay an agent's tidy-up task through the hooks installed in .claude/settings.json.

Each step calls the installed hook command through PowerShell (as Claude Code did on this
machine), performs the tool action only if PreToolUse allows it, then calls PostToolUse.
"""

import json
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
settings = json.loads((repo / ".claude" / "settings.json").read_text())


def hook(event, payload):
    group = settings["hooks"][event][0]["hooks"][0]
    payload = {"hook_event_name": event, "session_id": "replay-1", "cwd": str(repo), **payload}
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", group["command"]],
                       input=json.dumps(payload), capture_output=True, text=True, cwd=repo)
    if r.returncode != 0:
        print(f"  !! {event} hook exited {r.returncode}: {r.stderr[:300]}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def short(out):
    if not out:
        return "allowed, no feedback"
    hso = out.get("hookSpecificOutput", {})
    if hso.get("permissionDecision") == "deny":
        return "DENIED: " + hso["permissionDecisionReason"].split("\nDetails:")[0]
    if out.get("decision") == "block":
        return "FEEDBACK TO AGENT: " + out["reason"].split("\nDetails:")[0]
    if out.get("systemMessage"):
        return "MESSAGE: " + out["systemMessage"].split("\nDetails:")[0]
    if hso.get("additionalContext"):
        return "CONTEXT FOR AGENT: " + hso["additionalContext"].splitlines()[0]
    return json.dumps(out)[:200]


def bash(n, command):
    print(f"\n[{n}] Bash: {command}")
    pre = hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": command}})
    print("  pre :", short(pre))
    if pre.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
        return
    r = subprocess.run(["bash", "-c", command], cwd=repo, capture_output=True, text=True)
    print(f"  ran : exit {r.returncode} {(r.stdout + r.stderr).strip()[-200:]}")
    event = "PostToolUse" if r.returncode == 0 else "PostToolUseFailure"
    print("  post:", short(hook(event, {"tool_name": "Bash", "tool_input": {"command": command}})))


def edit(n, rel, old, new):
    path = repo / rel
    print(f"\n[{n}] Edit: {rel}")
    pre = hook("PreToolUse", {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": old, "new_string": new}})
    print("  pre :", short(pre))
    if pre.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
        return
    path.write_text(path.read_text().replace(old, new))
    print("  post:", short(hook("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": str(path)}})))


print("[0] SessionStart:", short(hook("SessionStart", {"source": "startup"})))
OLD = "def parse(text: str, strict: bool = False) -> Doc:\n    return Doc(text=text.strip(), strict=strict)"
NEW = "def parse(text: str, *, mode: str = \"lenient\") -> Doc:\n    return Doc(text=text.strip(), strict=mode == \"strict\")"
edit(1, "mylib/core.py", OLD, NEW)
bash(2, "rm data/users.json")
edit(3, "data/users.json", "Ada", "Grace")
bash(4, "git add -A && git -c user.name=agent -c user.email=a@example.com commit -m tidy")
bash(5, "git -c user.name=agent -c user.email=a@example.com commit --no-verify -am tidy")
bash(6, "python -m pytest -q")
bash(7, "git add -A && git -c user.name=agent -c user.email=a@example.com commit -m tidy")
print("\n[8] Stop:", short(hook("Stop", {"stop_hook_active": False})))
edit(9, "mylib/core.py", NEW, OLD)
bash(10, "python -m pytest -q")
bash(11, "git add -A && git -c user.name=agent -c user.email=a@example.com commit -m tidy")
print("\n[12] Stop:", short(hook("Stop", {"stop_hook_active": False})))
print("\ndata/users.json exists:", (repo / "data" / "users.json").exists())
print(subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True).stdout)
