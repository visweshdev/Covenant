"""Pull request feedback on GitHub: comments on the exact lines that break a rule, plus one
summary comment that is updated on every run (not a new one each push).

Runs inside GitHub Actions with the job's GITHUB_TOKEN (`permissions: pull-requests: write`);
see action.yml at the repo root. Only lines the PR changed can carry a comment; everything
else goes in the summary.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .ci import CiResult, markdown_summary
from .feedback import ViolationReport

MARKER = "<!-- covenant-review -->"
LOGO = "https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/logo.png"
BADGE = f'<img src="{LOGO}" width="18" height="18" alt="" align="top">'
API = "https://api.github.com"
Request = Callable[[str, str, Any], Any]  # (method, path, body) -> parsed JSON


def report_location(report: ViolationReport, repo: Path, changed: list[str] | None = None) -> tuple[str, int | None] | None:
    """(path, line) a report is about, when it is about a file."""
    ev = report.evidence
    path = ev.get("target") or ev.get("file") or ev.get("exported_from") or ev.get("path")
    if not path or not isinstance(path, str) or ":" in path or path.startswith("table "):
        return None
    line = ev.get("line")
    if line is None and ev.get("symbol") and ev.get("after") is not None:
        # A changed Python signature: the package re-exports it, so look where it is defined,
        # starting with the files this PR changed.
        name = str(ev["symbol"]).split(".")[-1]
        for candidate in [f for f in (changed or []) if f.endswith(".py")] + [path]:
            found = _find_definition(repo / candidate, name)
            if found:
                return candidate, found
    return path, int(line) if isinstance(line, int) or (isinstance(line, str) and line.isdigit()) else None


def _find_definition(path: Path, name: str) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    rx = re.compile(rf"^\s*(?:async\s+def|def|class)\s+{re.escape(name)}\b|^\s*{re.escape(name)}\s*[:=]")
    return next((n for n, text in enumerate(lines, 1) if rx.search(text)), None)


def added_lines(repo: Path, base: str) -> dict[str, set[int]]:
    """Line numbers (new side) each file gained or changed in base..HEAD: the lines a review can comment on."""
    out = subprocess.run(["git", "-C", str(repo), "diff", "-U0", "--no-color", f"{base}...HEAD"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    lines: dict[str, set[int]] = {}
    current = None
    for row in out.splitlines():
        if row.startswith("+++ "):
            current = row[6:] if row.startswith("+++ b/") else None
        elif row.startswith("@@") and current:
            m = re.search(r"\+(\d+)(?:,(\d+))?", row)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                lines.setdefault(current, set()).update(range(start, start + count))
    return lines


def comment_body(report: ViolationReport) -> str:
    hint = f"\n\n**Fix:** {report.repair_hints[0]}" if report.repair_hints else ""
    ev = report.evidence
    detail = ev.get("what") or ev.get("kind") or ev.get("change") or ""
    if ev.get("before") or ev.get("after"):
        detail = f"`{ev.get('before')}` → `{ev.get('after')}`"
    return (f"{BADGE} **Covenant {report.violation}**: {report.clause}" + (f"\n\n{detail}" if detail else "") + hint
            + f"\n\n{MARKER}")


def review_plan(result: CiResult, repo: Path) -> tuple[list[dict[str, Any]], list[ViolationReport]]:
    """(inline comments, reports that only fit in the summary)."""
    changed = added_lines(repo, result.base)
    inline, elsewhere = [], []
    for r in result.reports:
        where = report_location(r, repo, sorted(changed))
        if where and where[1] and where[1] in changed.get(where[0], set()):
            inline.append({"path": where[0], "line": where[1], "side": "RIGHT", "body": comment_body(r)})
        else:
            elsewhere.append(r)
    return inline, elsewhere


def _http(token: str) -> Request:
    def call(method: str, path: str, body: Any = None) -> Any:
        req = urllib.request.Request(
            API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310 (fixed https API host)
            text = response.read().decode("utf-8")
            return json.loads(text) if text else None
    return call


def pr_context(env: dict[str, str] | None = None) -> tuple[str, int, str] | None:
    """(owner/repo, PR number, head sha) from the GitHub Actions environment."""
    env = os.environ if env is None else env
    event_path = env.get("GITHUB_EVENT_PATH")
    if not event_path or not env.get("GITHUB_REPOSITORY"):
        return None
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pr = event.get("pull_request")
    if not pr:
        return None
    return env["GITHUB_REPOSITORY"], int(pr["number"]), pr["head"]["sha"]


def post(result: CiResult, repo: Path, request: Request | None = None, context: tuple[str, int, str] | None = None,
         token: str | None = None) -> dict[str, Any]:
    """Comment on the PR. Returns what was done."""
    context = context or pr_context()
    if context is None:
        return {"skipped": "not running on a pull request in GitHub Actions"}
    if request is None:
        token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            return {"skipped": "no GITHUB_TOKEN; give the job `permissions: pull-requests: write`"}
        request = _http(token)
    slug, number, head = context
    inline, elsewhere = review_plan(result, repo)

    # Line comments: skip the ones already posted by an earlier run, so re-runs don't repeat them.
    existing = request("GET", f"/repos/{slug}/pulls/{number}/comments?per_page=100", None) or []
    seen = {(c.get("path"), c.get("line"), c.get("body")) for c in existing if MARKER in (c.get("body") or "")}
    fresh = [c for c in inline if (c["path"], c["line"], c["body"]) not in seen]
    if fresh:
        request("POST", f"/repos/{slug}/pulls/{number}/reviews",
                {"commit_id": head, "event": "COMMENT", "comments": fresh,
                 "body": f"{BADGE} Covenant found {len(result.reports)} broken rule(s); details on the lines below.\n\n{MARKER}"})

    # One summary comment, edited in place.
    summary = markdown_summary(result).replace("### Covenant:", f"### {BADGE} Covenant:", 1)
    if elsewhere and inline:
        summary += f"\n{len(inline)} problem(s) are commented on their lines; the rest are listed above.\n"
    body = summary + f"\n{MARKER}"
    comments = request("GET", f"/repos/{slug}/issues/{number}/comments?per_page=100", None) or []
    mine = next((c for c in comments if MARKER in (c.get("body") or "")), None)
    if mine:
        request("PATCH", f"/repos/{slug}/issues/comments/{mine['id']}", {"body": body})
    else:
        request("POST", f"/repos/{slug}/issues/{number}/comments", {"body": body})
    return {"line_comments": len(fresh), "already_posted": len(inline) - len(fresh), "summary": "updated" if mine else "created"}
