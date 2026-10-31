"""`covenant explain`: why was something blocked, and what can be done about it, in plain words."""

from __future__ import annotations

from datetime import datetime
from typing import Any

HUMAN_OPTIONS = [
    "If the agent should be allowed this, change the rule: edit covenant.yaml and run `covenant start` "
    "(or open `covenant dashboard`).",
    "To add a rule without restarting the task: `covenant amend \"...\"`.",
    "To put files back as they were before a risky step: `covenant snapshots`, then `covenant undo`.",
]


def _when(ts: str | None) -> str:
    if not ts:
        return "Recently"
    try:
        return "At " + datetime.fromisoformat(ts).astimezone().strftime("%H:%M on %d %b")
    except ValueError:
        return "Recently"


def _action(event: str) -> str:
    event = event or ""
    if event.startswith("refused:"):
        return "use a tool"
    if event.startswith("check:task-end"):
        return "finish the task"
    if event.startswith("check"):
        return "pass Covenant's checks"
    return {"commit": "commit", "push": "push", "edit": "edit a file", "run": "run a command",
            "test": "run the tests", "effect": "change a protected file", "preview": "commit"}.get(event, event)


def why(report: dict[str, Any]) -> str:
    """One or two sentences on what the evidence shows."""
    ev = report.get("evidence") or {}
    checker = report.get("checker", "")
    if checker == "trace_monitor":
        if ev.get("counts_so_far"):
            counts = ", ".join(f"{n} {a}{'s' if n != 1 else ''}" for a, n in ev["counts_so_far"].items())
            return f"The task's limit for this action is used up ({counts} so far)."
        edits = ev.get("edits_since_last_passing_test") or []
        if ev.get("last_passing_test") is None:
            return "The tests haven't passed yet in this task." + (f" Files edited: {', '.join(edits[:5])}." if edits else "")
        return f"Files were edited after the last passing test run: {', '.join(edits[:5]) or 'some files'}."
    if checker in ("api_surface_diff", "ts_api_surface_diff", "source_api_diff"):
        symbol = ev.get("symbol") or ev.get("name") or "a public name"
        if ev.get("after") is None:
            return f"The public name `{symbol}` was removed."
        return f"The public name `{symbol}` changed from `{ev.get('before')}` to `{ev.get('after')}`."
    if checker == "effect_monitor":
        done = " Covenant put it back from its snapshot." if ev.get("rolled_back") else ""
        return f"`{ev.get('target')}` is protected, and this would {ev.get('op', 'change')} it.{done}"
    if checker == "command_guard":
        what = ev.get("what") or ev.get("category")
        command = ev.get("command") or ""
        if not command or command[:60] in str(what):  # the SQL already quotes the command
            return f"The command counts as {what}."
        return f"The command counts as {what}: `{command[:160]}`."
    if checker == "budget":
        limit = ev.get("limit")
        if limit == "time":
            return f"The task has run {ev.get('minutes_used')} minutes; the limit is {ev.get('max_minutes')}."
        if limit == "actions":
            return f"The agent has taken {ev.get('actions_used')} actions; the limit is {ev.get('max_actions')}."
        return f"The task has used about ${ev.get('estimated_usd')}; the limit is ${ev.get('max_cost_usd')}."
    if checker == "scope_guard":
        return f"`{ev.get('target') or ev.get('path')}` is outside the files this task may change."
    if checker == "path_owner":
        who = ev.get("agent") or "an agent with no name (COVENANT_AGENT isn't set)"
        return f"`{ev.get('target')}` belongs to {', '.join(ev.get('owners', []))}, and {who} isn't one of them."
    if checker == "secret_scan":
        where = f"`{ev.get('target')}`" + (f" line {ev['line']}" if ev.get("line") else "")
        return f"What looks like a {ev.get('kind', 'secret')} ({ev.get('secret', 'masked')}) was added in {where}."
    if checker == "test_guard":
        return f"Tests in `{ev.get('target')}` were {ev.get('change', 'weakened')}" + (
            f": `{ev['code']}`." if ev.get("code") else ".")
    if checker in ("no_debug_code", "no_suppressions"):
        return f"`{ev.get('target')}` line {ev.get('line')} adds {ev.get('kind', 'this')}: `{ev.get('code', '')}`."
    if checker in ("diff_budget", "large_files", "dependency_diff", "schema_guard", "sqlite_guard") and ev.get("target"):
        details = ", ".join(f"{k}: {v}" for k, v in ev.items() if k != "target")
        return f"`{ev['target']}`" + (f" ({details})." if details else ".")
    if checker == "integrity":
        return f"Covenant's own setup changed: `{ev.get('path')}` ({ev.get('problem')})."
    if ev.get("error"):
        return f"The check couldn't run: {ev['error']}."
    shown = ", ".join(f"{k}: {v}" for k, v in list(ev.items())[:4])
    return f"Evidence: {shown}." if shown else "The rule's check failed."


def explain_entry(entry: dict[str, Any]) -> str:
    reports = [r for r in entry.get("reports", []) if r.get("on_violation") != "warn"] or entry.get("reports", [])
    blocked = not entry.get("allowed", True)
    action = _action(entry.get("event", ""))
    verb = "Covenant blocked it" if blocked else "Covenant reported a problem"
    lines = [f"{_when(entry.get('ts'))}, the agent tried to {action}. {verb}."]
    for r in reports:
        lines.append("")
        lines.append(f"Rule {r.get('violation')}: \"{r.get('clause')}\"")
        lines.append(f"  Why: {why(r)}")
        hints = r.get("repair_hints") or []
        if hints:
            lines.append(f"  What the agent can do: {hints[0]}")
        if r.get("escalated") or r.get("on_violation") == "ask_human":
            lines.append("  This has happened repeatedly or needs a person, so the agent was told to ask you.")
    lines += ["", "What you can do:"] + [f"  - {o}" for o in HUMAN_OPTIONS]
    return "\n".join(lines)


def latest(entries: list[dict[str, Any]], include_warnings: bool = False) -> list[dict[str, Any]]:
    """Entries worth explaining, newest last."""
    def serious(e: dict[str, Any]) -> bool:
        return include_warnings or any(r.get("on_violation") != "warn" for r in e.get("reports", []))
    return [e for e in entries if e.get("reports") and serious(e)]


def explain(entries: list[dict[str, Any]], count: int = 1) -> str:
    chosen = latest(entries)[-count:]
    if not chosen:
        return "Nothing has been blocked or reported in this task."
    return "\n\n────────\n\n".join(explain_entry(e) for e in chosen)
