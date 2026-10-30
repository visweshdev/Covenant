"""Run benchmark tasks under a mode, with an agent, and score them.

Modes
    prompt       the instruction only; no Covenant (the baseline)
    covenant     compile the instruction (offline extractor, or Claude with --llm), with a
                 simulated developer answering the clarification questions, then enforce
    covenant-unattended
                 the same, but nobody answers: every ambiguity takes the conservative default
    groundtruth  enforce the task's hand-written Covenant (upper bound: enforcement without
                 compilation errors)

Agents
    scripted     replays the task's "naive" plan through Claude Code's hook protocol; when
                 Covenant blocks or reports a step, it reverts and follows the "careful" plan
                 (a stand-in for an agent repairing after feedback). Deterministic and free.
    command      any real agent: `--agent-cmd "claude -p {instruction} ..."` run in the repo.
"""

from __future__ import annotations

import json
import shlex
import sys
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..compiler.extract import HeuristicExtractor, LLMExtractor
from ..compiler.pipeline import compile_instruction
from ..integrations.claude_code import ClaudeCodeHook
from ..ir import ClauseStatus, Covenant
from ..runtime import Runtime
from .oracle import evaluate
from .task import GroundTruthClause, Step, Task

GIT_ID = ["-c", "user.name=bench", "-c", "user.email=bench@example.com"]


@dataclass
class TaskResult:
    task: str
    mode: str
    agent: str
    success: bool
    violations: list[str]
    blocked: int = 0
    recovered: bool = False
    over_restricted: bool = False
    fidelity: dict[str, float] | None = None
    questions: list[str] = field(default_factory=list)
    asked_expected: bool | None = None
    seconds: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *GIT_ID, "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")


def _matches(gt: GroundTruthClause, clause) -> bool:
    if clause.checker != gt.checker:
        return False
    for key, want in gt.params.items():
        have = clause.params.get(key)
        if isinstance(want, list):
            if not isinstance(have, list) or not set(map(str, want)) <= set(map(str, have)):
                return False
        elif have != want:
            return False
    return True


def fidelity(compiled: Covenant, truth: list[GroundTruthClause]) -> dict[str, float]:
    enforced = [c for c in compiled.clauses if c.status is ClauseStatus.ENFORCED]
    matched_truth = sum(any(_matches(gt, c) for c in enforced) for gt in truth)
    matched_compiled = sum(any(_matches(gt, c) for gt in truth) for c in enforced)
    precision = matched_compiled / len(enforced) if enforced else (1.0 if not truth else 0.0)
    recall = matched_truth / len(truth) if truth else 1.0
    return {"precision": round(precision, 3), "recall": round(recall, 3)}


class ScriptedAgent:
    """Acts through the same JSON hook protocol Claude Code uses."""

    name = "scripted"

    def __init__(self, repo: Path, hook: ClaudeCodeHook | None):
        self.repo, self.hook = repo, hook
        self.blocked = 0
        self.log: list[str] = []

    def _hook(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.hook.handle({"session_id": "bench", "cwd": str(self.repo), **payload}) if self.hook else {}

    @staticmethod
    def _denied(out: dict[str, Any]) -> bool:
        return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def _shell(self, command: str) -> bool:
        """Returns False when Covenant objected (denied, or reported a problem afterwards)."""
        if self._denied(self._hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}})):
            self.log.append(f"denied: {command}")
            return False
        r = subprocess.run(["bash", "-c", command], cwd=self.repo, capture_output=True, text=True)
        event = "PostToolUse" if r.returncode == 0 else "PostToolUseFailure"
        out = self._hook({"hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": command}})
        self.log.append(f"ran ({r.returncode}): {command}")
        if r.returncode != 0 and "git commit" in command:  # a git hook refused it
            return False
        return out.get("decision") != "block"

    def _edit(self, path: str, apply) -> bool:
        target = self.repo / path
        tool = "Edit" if target.exists() else "Write"
        if self._denied(self._hook({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"file_path": str(target)}})):
            self.log.append(f"denied edit: {path}")
            return False
        apply(target)
        out = self._hook({"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"file_path": str(target)}})
        self.log.append(f"edited: {path}")
        return out.get("decision") != "block"

    def step(self, s: Step) -> bool:
        if s.replace:
            def apply(p: Path) -> None:
                p.write_text(p.read_text(encoding="utf-8").replace(s.old or "", s.new or ""), encoding="utf-8")
            return self._edit(s.replace, apply)
        if s.write:
            def write(p: Path) -> None:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(s.content or "", encoding="utf-8")
            return self._edit(s.write, write)
        if s.run:
            return self._shell(s.run)
        if s.commit:
            return self._shell(f"git {' '.join(GIT_ID)} add -A && git {' '.join(GIT_ID)} commit -q -m {shlex.quote(s.commit)}")
        return True

    def run(self, plan: list[Step]) -> bool:
        """Runs a plan; stops at the first step Covenant objects to."""
        for s in plan:
            if not self.step(s):
                self.blocked += 1
                return False
        if self.hook:
            stop = self._hook({"hook_event_name": "Stop", "stop_hook_active": False})
            if stop.get("decision") == "block":
                self.blocked += 1
                self.log.append("stop blocked")
                return False
        return True


def _prepare(task: Task, workdir: Path) -> tuple[Path, str]:
    repo = workdir / task.id
    shutil.copytree(task.root / task.repo, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    _git(repo, "init", "-q", "-b", "main")
    # Keep files byte-identical across resets: line-ending conversion would look like an overwrite.
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def run_task(task: Task, mode: str, agent: str = "scripted", agent_cmd: str | None = None, llm: bool = False,
             workdir: Path | None = None) -> TaskResult:
    workdir = workdir or Path(tempfile.mkdtemp(prefix="covenant-bench-"))
    repo, base = _prepare(task, workdir)
    original = workdir / f"{task.id}.original"
    shutil.copytree(repo, original, ignore=shutil.ignore_patterns(".git"))
    state = workdir / f"{task.id}.state"
    started = time.monotonic()
    result = TaskResult(task=task.id, mode=mode, agent=agent, success=False, violations=[])

    hook = None
    if mode in ("covenant", "covenant-unattended", "groundtruth"):
        if mode.startswith("covenant"):
            import re

            extractor = LLMExtractor() if llm else HeuristicExtractor()

            def developer(question) -> int:  # answers like the task's author would
                for pattern, choice in task.answers.items():
                    if re.search(pattern, question.text, re.I):
                        return choice
                return question.default

            ask = developer if mode == "covenant" and task.answers else None
            compiled = compile_instruction(task.instruction, repo, extractor, ask=ask)
            covenant = compiled.covenant
            result.questions = [q.text for q, _ in compiled.asked] + [q.text for q in compiled.defaulted]
            if task.should_ask:
                result.asked_expected = any(re.search(task.should_ask, q, re.I) for q in result.questions)
        else:
            covenant = Covenant.model_validate(task.covenant)
        result.fidelity = fidelity(covenant, task.ground_truth)
        covenant.dump(repo / "covenant.yaml")
        _git(repo, "add", "covenant.yaml")
        _git(repo, "commit", "-qm", "covenant", "--no-verify")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        cli = [sys.executable, "-m", "covenant", "--repo", str(repo), "--state-dir", str(state)]
        subprocess.run([*cli, "install-hooks"], capture_output=True)
        runtime = Runtime.open(repo, state_dir=state)
        runtime.start()
        hook = ClaudeCodeHook(runtime)
        hook.handle({"hook_event_name": "SessionStart", "source": "startup", "session_id": "bench"})

    if agent == "scripted":
        bot = ScriptedAgent(repo, hook)
        ok = bot.run(task.scripted.get("naive", []))
        if not ok:
            _git(repo, "reset", "-q", "--hard", "HEAD")  # back to the last commit, then do it properly
            ok = bot.run(task.scripted.get("careful", []))
            result.recovered = ok
        result.blocked = bot.blocked
        result.detail["log"] = bot.log
    else:
        command = (agent_cmd or "").format(instruction=shlex.quote(task.instruction), repo=shlex.quote(str(repo)))
        r = subprocess.run(command, shell=True, cwd=repo, capture_output=True, text=True, timeout=3600)
        result.detail["agent_exit"] = r.returncode
        result.detail["agent_tail"] = (r.stdout + r.stderr)[-2000:]
        if mode != "prompt":
            log = state / "reports.jsonl"
            result.blocked = sum(1 for line in log.read_text().splitlines() if line.strip()) if log.exists() else 0

    oracle = [evaluate(c, repo, original, base) for c in task.oracle]
    acceptance = [evaluate(c, repo, original, base) for c in task.acceptance]
    result.violations = [c.check.description or c.check.type for c in oracle if not c.passed]
    result.success = all(c.passed for c in acceptance)
    result.over_restricted = not result.success and not result.violations and result.blocked > 0
    result.detail["oracle"] = [{"check": c.check.description or c.check.type, "passed": c.passed, "detail": c.detail} for c in oracle]
    result.detail["acceptance"] = [{"check": c.check.description or c.check.type, "passed": c.passed, "detail": c.detail} for c in acceptance]
    result.seconds = round(time.monotonic() - started, 2)
    return result


def summarize(results: list[TaskResult | dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows = [r if isinstance(r, dict) else asdict(r) for r in results]
    by_mode: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_mode.setdefault(f"{r['mode']}/{r['agent']}", []).append(r)
    out = {}
    for key, rs in by_mode.items():
        n = len(rs)
        fid = [r["fidelity"] for r in rs if r.get("fidelity")]
        blocked = [r for r in rs if r["blocked"]]
        asked = [r["asked_expected"] for r in rs if r.get("asked_expected") is not None]
        out[key] = {
            "tasks": n,
            "violations_per_task": round(sum(len(r["violations"]) for r in rs) / n, 3),
            "tasks_with_violations": sum(1 for r in rs if r["violations"]),
            "task_success": round(sum(r["success"] for r in rs) / n, 3),
            "compliant_success": round(sum(r["success"] and not r["violations"] for r in rs) / n, 3),
            "over_restriction": sum(r["over_restricted"] for r in rs),
            "feedback_recovery": round(sum(r["recovered"] for r in blocked) / len(blocked), 3) if blocked else None,
            "fidelity_precision": round(sum(f["precision"] for f in fid) / len(fid), 3) if fid else None,
            "fidelity_recall": round(sum(f["recall"] for f in fid) / len(fid), 3) if fid else None,
            "asked_the_right_question": round(sum(asked) / len(asked), 3) if asked else None,
            "seconds_per_task": round(sum(r["seconds"] for r in rs) / n, 2),
        }
    return out


def write_results(results: list[TaskResult], path: Path) -> None:
    with path.open("a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(asdict(r)) + "\n")


def read_results(paths: list[Path]) -> list[dict[str, Any]]:
    rows = []
    for p in paths:
        rows += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def render_report(summary: dict[str, dict[str, Any]]) -> str:
    cols = ["tasks", "violations_per_task", "task_success", "compliant_success", "over_restriction",
            "feedback_recovery", "fidelity_precision", "fidelity_recall", "asked_the_right_question", "seconds_per_task"]
    lines = ["| mode/agent | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for key, s in summary.items():
        lines.append(f"| {key} | " + " | ".join("—" if s[c] is None else str(s[c]) for c in cols) + " |")
    return "\n".join(lines)

