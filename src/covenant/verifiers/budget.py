"""budget: how much time, how many actions and how much money a task may use.

Clause params (any of):
    max_minutes:    wall-clock minutes since the task started
    max_actions:    agent actions Covenant sees (edits, shell commands, web and MCP calls;
                    file reads and searches aren't counted)
    max_cost_usd:   estimated spend, from the token counts in Claude Code's session log

Checked before each action; once a limit is reached, further actions are refused and the agent
is told to stop and hand over. Reading files is still allowed, so it can explain where it got to.

The cost is an estimate at Anthropic's API prices (on a Claude subscription nothing is charged
per token; the number is still a fair measure of how much work was done). Subagent sessions
write their own logs and aren't included.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..feedback import ViolationReport
from ..ir import Clause

# $ per million tokens: (input, output, cache read). Cache writes cost 1.25x input.
PRICES: list[tuple[str, tuple[float, float, float]]] = [
    (r"fable|mythos", (10.0, 50.0, 0.25)),
    (r"opus-5-5", (4.0, 20.0, 0.20)),
    (r"opus", (5.0, 25.0, 0.50)),
    (r"sonnet-5", (2.0, 10.0, 0.20)),
    (r"sonnet", (3.0, 15.0, 0.30)),
    (r"haiku-5", (0.10, 0.50, 0.01)),
    (r"haiku", (1.0, 5.0, 0.10)),
]
UNKNOWN = (10.0, 50.0, 0.25)  # an unknown model is priced high, so the budget errs on the safe side


def price(model: str) -> tuple[float, float, float]:
    for pattern, p in PRICES:
        if re.search(pattern, model or "", re.I):
            return p
    return UNKNOWN


def transcript_cost(path: str | Path | None) -> float:
    """Estimated dollars spent in a Claude Code session log (JSONL). Each API message is logged
    once per content block with the same usage, so messages are counted once by id."""
    if not path:
        return 0.0
    usage: dict[str, tuple[str, dict]] = {}
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                    continue
                key = message.get("id") or entry.get("uuid") or str(len(usage))
                usage[key] = (message.get("model", ""), message["usage"])
    except OSError:
        return 0.0
    total = 0.0
    for model, u in usage.values():
        p_in, p_out, p_read = price(model)
        total += (
            (u.get("input_tokens") or 0) * p_in
            + (u.get("cache_creation_input_tokens") or 0) * p_in * 1.25
            + (u.get("cache_read_input_tokens") or 0) * p_read
            + (u.get("output_tokens") or 0) * p_out
        ) / 1_000_000
    return total


def minutes_since(started_at: str | None, now: datetime | None = None) -> float:
    if not started_at:
        return 0.0
    try:
        start = datetime.fromisoformat(started_at)
    except ValueError:
        return 0.0
    return ((now or datetime.now(timezone.utc)) - start).total_seconds() / 60


def check(clause: Clause, started_at: str | None, actions: int, transcript: str | None = None,
          now: datetime | None = None) -> list[ViolationReport]:
    """Reports for limits that are used up. `actions` counts the actions before this one."""
    p = clause.params
    reports = []
    stop = "Stop here: summarise what's done and what's left, and hand over to the human"
    if "max_minutes" in p:
        used = minutes_since(started_at, now)
        if used >= float(p["max_minutes"]):
            reports.append(ViolationReport.for_clause(
                clause, {"limit": "time", "minutes_used": round(used, 1), "max_minutes": p["max_minutes"]}, hints=[stop]))
    if "max_actions" in p and actions >= int(p["max_actions"]):
        reports.append(ViolationReport.for_clause(
            clause, {"limit": "actions", "actions_used": actions, "max_actions": p["max_actions"]}, hints=[stop]))
    if "max_cost_usd" in p:
        cost = transcript_cost(transcript)
        if cost >= float(p["max_cost_usd"]):
            reports.append(ViolationReport.for_clause(
                clause, {"limit": "cost", "estimated_usd": round(cost, 2), "max_cost_usd": p["max_cost_usd"]}, hints=[stop]))
    return reports


def usage(clause: Clause, started_at: str | None, actions: int, transcript: str | None = None) -> dict:
    """How much of each limit is used, for status displays."""
    p, out = clause.params, {}
    if "max_minutes" in p:
        out["minutes"] = [round(minutes_since(started_at), 1), p["max_minutes"]]
    if "max_actions" in p:
        out["actions"] = [actions, p["max_actions"]]
    if "max_cost_usd" in p:
        out["cost_usd"] = [round(transcript_cost(transcript), 2), p["max_cost_usd"]]
    return out
