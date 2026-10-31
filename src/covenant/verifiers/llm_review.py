"""llm_review: Claude reads the change and says whether it follows rules no checker can verify.

For instructions like "keep the code readable" or "follow the existing error-handling style".
Its findings are advisory: clauses use on_violation: warn, so they are reported to the agent
and the human but never block a commit. One request covers every review rule; answers are
cached per change, so a commit that is retried doesn't ask again.

Clause params:
    rule:     what to check (default: the clause's source_span)
    baseline: git ref (pinned to a sha at start)

Reaches Claude through ANTHROPIC_API_KEY if set, otherwise the Claude Code login (`claude -p`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..feedback import ViolationReport
from ..ir import Clause
from .diff_guard import changes

MAX_DIFF = 60_000
SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "follows": {"type": "boolean"},
                    "explanation": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                },
                "required": ["rule_id", "follows", "explanation", "file", "line"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}
SYSTEM = (
    "You review a code change against rules a developer gave a coding agent. For each rule, decide "
    "whether the change follows it. Judge only what the change does, not the code that was already "
    "there. Be strict about clear violations and lenient about matters of taste. When a rule is "
    "broken, point at the file and line in the new version (use an empty file and line 0 otherwise), "
    "and explain in one or two sentences what to change."
)


def diff_text(repo: Path, base: str) -> str:
    """A unified diff of the change, new files included, cut to MAX_DIFF characters."""
    parts = [subprocess.run(["git", "-C", str(repo), "diff", base, "--"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace").stdout]
    for c in changes(repo, base):
        if c.status == "A":
            text = c.new_text()
            if text is not None and not _tracked(repo, c.path):
                parts.append(f"--- /dev/null\n+++ b/{c.path}\n" + "".join(f"+{line}\n" for line in text.splitlines()))
    text = "\n".join(p for p in parts if p)
    return text if len(text) <= MAX_DIFF else text[:MAX_DIFF] + "\n...(diff truncated)"


def _tracked(repo: Path, path: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", path], capture_output=True).returncode == 0


def ask_claude(system: str, user: str, schema: dict[str, Any], runner: Any = None) -> dict[str, Any]:
    """Structured answer from Claude: the API when a key is set, else the Claude Code login."""
    if os.environ.get("ANTHROPIC_API_KEY") and runner is None:
        import anthropic

        client = anthropic.Anthropic(**({"base_url": os.environ["COVENANT_ANTHROPIC_BASE_URL"]}
                                        if os.environ.get("COVENANT_ANTHROPIC_BASE_URL") else {}))
        response = client.messages.create(
            model=os.environ.get("COVENANT_REVIEW_MODEL", "claude-sonnet-5-5"), max_tokens=4000, system=system,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
        )
        text = next(b.text for b in response.content if getattr(b, "type", "") == "text")
        return json.loads(text)
    exe = shutil.which("claude")
    if not exe and runner is None:
        raise RuntimeError("no way to reach Claude (set ANTHROPIC_API_KEY or log in to Claude Code)")
    cmd = [exe or "claude", "-p", "--restricted", "--strict-mcp-config", "--tools", "", "--no-session-persistence",
           "--output-format", "json", "--json-schema", json.dumps(schema)]
    with tempfile.TemporaryDirectory(prefix="covenant-review-") as empty:
        r = (runner or subprocess.run)(cmd, input=f"{system}\n\n{user}", capture_output=True, text=True,
                                       encoding="utf-8", timeout=300, cwd=empty)
    out = json.loads(r.stdout)
    if out.get("is_error") or out.get("structured_output") is None:
        raise RuntimeError(str(out.get("result") or "Claude Code returned no answer"))
    return out["structured_output"]


def review(clauses: list[Clause], repo: Path, baselines: dict[str, str], state_dir: Path,
           runner: Any = None) -> list[ViolationReport]:
    if not clauses:
        return []
    base = baselines.get(clauses[0].id) or clauses[0].params.get("baseline", "HEAD")
    diff = diff_text(repo, base)
    if not diff.strip():
        return []
    rules = {c.id: c.params.get("rule") or c.source_span for c in clauses}
    key = hashlib.sha256(json.dumps([rules, diff], sort_keys=True).encode()).hexdigest()
    cache_file = state_dir / "review-cache.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.is_file() else {}
    except ValueError:
        cache = {}
    if key not in cache:
        user = "Rules:\n" + "\n".join(f"- {cid}: {text}" for cid, text in rules.items()) + f"\n\nThe change:\n```diff\n{diff}\n```"
        try:
            answer = ask_claude(SYSTEM, user, SCHEMA, runner)
        except Exception as exc:  # advisory: say it couldn't run, don't block
            return [ViolationReport.for_clause(c, {"error": f"review didn't run: {exc}"[:300], "advisory": True},
                                               hints=["The review is advisory; check this rule yourself"]) for c in clauses]
        cache = {key: answer}  # only the latest change matters
        state_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache), encoding="utf-8")
    by_id = {c.id: c for c in clauses}
    reports = []
    for v in cache[key].get("verdicts", []):
        clause = by_id.get(v.get("rule_id"))
        if clause is None or v.get("follows"):
            continue
        evidence = {"advisory": True, "explanation": v.get("explanation", "")}
        if v.get("file"):
            evidence.update(target=v["file"], line=int(v.get("line") or 0) or None)
        reports.append(ViolationReport.for_clause(clause, evidence, hints=[v.get("explanation") or "Revisit this rule"]))
    return reports
