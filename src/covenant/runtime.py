"""The runtime: judges every event against the approved Covenant. No LLM is involved.

State lives in `state_dir` so separate processes (git hooks, the shell wrapper,
`covenant check`) share one trace:

    session.json   start time and pinned baseline commits
    trace.jsonl    every event that happened, replayed on load
    reports.jsonl  every decision that raised a violation, for audit
    snapshot/      copies of protected files, for rollback

In the full design `state_dir` sits outside the agent's writable sandbox.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import EVENT_ADAPTER, GATED_EVENTS, Effect, Event, TestRun
from .feedback import ViolationReport
from .ir import Clause, ClauseKind, Covenant, OnViolation
from .monitors.effects import EffectMonitor
from .monitors.snapshot import CopySnapshotter
from .monitors.trace import TraceMonitor
from .verifiers import api_surface
from .verifiers.test_gate import run_tests

SemanticVerifier = Callable[[Clause, Path, "str | None"], list[ViolationReport]]
SEMANTIC_VERIFIERS: dict[str, SemanticVerifier] = {"api_surface_diff": api_surface.verify}


@dataclass
class Decision:
    event: str
    allowed: bool
    reports: list[ViolationReport] = field(default_factory=list)

    @property
    def escalated(self) -> bool:
        return any(r.escalated or r.on_violation is OnViolation.ASK_HUMAN for r in self.reports)

    @property
    def exit_code(self) -> int:
        """0 = clean, 1 = violations (agent should revise), 2 = needs a human."""
        if not self.reports:
            return 0
        return 2 if self.escalated else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "allowed": self.allowed,
            "reports": [r.model_dump(mode="json") for r in self.reports],
        }


def _is_resolved(report: ViolationReport) -> bool:
    """An effect that was rolled back successfully no longer needs to block anything."""
    return report.kind is ClauseKind.EFFECT_PROHIBITION and report.evidence.get("rolled_back") is True


class Runtime:
    def __init__(
        self,
        covenant: Covenant,
        repo_root: str | Path,
        state_dir: str | Path | None = None,
        escalate_after: int = 3,
    ):
        self.covenant = covenant
        self.repo_root = Path(repo_root).resolve()
        self.state_dir = Path(state_dir).resolve() if state_dir else self.repo_root / ".covenant"
        self.escalate_after = escalate_after
        self.effect_monitors = [
            EffectMonitor(c, self.repo_root) for c in covenant.enforced(ClauseKind.EFFECT_PROHIBITION)
        ]
        self.semantic_clauses = covenant.enforced(ClauseKind.SEMANTIC_INVARIANT)
        self.snapshotter = CopySnapshotter(self.state_dir, self.repo_root)
        self._load_state()

    # ----- state -------------------------------------------------------------

    @property
    def _trace_path(self) -> Path:
        return self.state_dir / "trace.jsonl"

    @property
    def _reports_path(self) -> Path:
        return self.state_dir / "reports.jsonl"

    @property
    def _session_path(self) -> Path:
        return self.state_dir / "session.json"

    def _fresh_monitors(self) -> None:
        self.trace_monitors = [
            (c, TraceMonitor(c.params["formula"]))
            for c in self.covenant.enforced(ClauseKind.TEMPORAL_OBLIGATION)
        ]
        self.trace: list[Event] = []

    def _load_state(self) -> None:
        self._fresh_monitors()
        self.session: dict[str, Any] = {}
        if self._session_path.exists():
            self.session = json.loads(self._session_path.read_text(encoding="utf-8"))
        if self._trace_path.exists():
            for line in self._trace_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    event = EVENT_ADAPTER.validate_json(line)
                    for _, monitor in self.trace_monitors:
                        monitor.step(event)
                    self.trace.append(event)
        self.block_counts: Counter[str] = Counter()
        if self._reports_path.exists():
            for line in self._reports_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.block_counts.update(json.loads(line).get("counted", []))

    def start(self) -> dict[str, Any]:
        """Begin a task: clear the trace, snapshot protected data, pin baselines."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for path in (self._trace_path, self._reports_path):
            path.unlink(missing_ok=True)
        self._fresh_monitors()
        self.block_counts = Counter()
        baselines = {}
        for clause in self.semantic_clauses:
            ref = clause.params.get("baseline")
            if ref:
                baselines[clause.id] = self._git_rev_parse(ref)
        files = self.snapshotter.take(self.effect_monitors)
        self.session = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "baselines": baselines,
            "snapshot_files": files,
        }
        self._session_path.write_text(json.dumps(self.session, indent=2), encoding="utf-8")
        return self.session

    def _git_rev_parse(self, ref: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo_root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"cannot resolve baseline {ref!r} in {self.repo_root}: {result.stderr.strip()}")
        return result.stdout.strip()

    # ----- judging events ----------------------------------------------------

    def record(self, event: Event) -> Decision:
        """Judge an event. Gated events (commit, push) are refused if anything blocks."""
        reports = self._handle_effects(self.snapshotter.scan())
        own_effect_reports: list[ViolationReport] = []
        if isinstance(event, Effect):
            own_effect_reports = self._handle_effects([event])
            reports += own_effect_reports

        gated = event.type in GATED_EVENTS
        pending = []
        for clause, monitor in self.trace_monitors:
            ok, state = monitor.peek(event)
            pending.append(state)
            if not ok:
                reports.append(self._trace_report(clause, event))
        if gated:
            reports += self._semantic_reports()

        blocking = [r for r in reports if not _is_resolved(r)]
        refused = gated and bool(blocking)
        if refused:
            for r in blocking:
                r.blocked_action = f"git {event.type}"
        else:  # the event happened (or is about to): add it to the trace
            for (_, monitor), state in zip(self.trace_monitors, pending):
                monitor.advance(state)
            self.trace.append(event)
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with self._trace_path.open("a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")

        allowed = not refused and not own_effect_reports
        decision = Decision(event.type, allowed, reports)
        self._log(decision)
        return decision

    def check(self, checkpoint: str = "manual") -> Decision:
        """Run every semantic verifier now, without adding an event. The agent may call this early."""
        reports = self._handle_effects(self.snapshotter.scan()) + self._semantic_reports()
        decision = Decision(f"check:{checkpoint}", not any(not _is_resolved(r) for r in reports), reports)
        self._log(decision)
        return decision

    def run_tests(self) -> list[tuple[Decision, str]]:
        """Run each distinct test_cmd declared by temporal clauses and record the results."""
        commands = list(
            dict.fromkeys(c.params["test_cmd"] for c, _ in self.trace_monitors if "test_cmd" in c.params)
        )
        if not commands:
            raise ValueError("no clause declares a test_cmd")
        results = []
        for cmd in commands:
            event, output = run_tests(cmd, self.repo_root)
            results.append((self.record(event), output))
        return results

    # ----- helpers -----------------------------------------------------------

    def _handle_effects(self, effects: list[Effect]) -> list[ViolationReport]:
        reports = []
        for effect in effects:
            for monitor in self.effect_monitors:
                if not monitor.violates(effect):
                    continue
                evidence: dict[str, Any] = {"op": effect.op, "target": effect.target, "channel": effect.channel}
                if monitor.clause.on_violation is OnViolation.ROLLBACK and effect.channel == "fs":
                    evidence["rolled_back"] = self.snapshotter.restore(effect.target)
                else:
                    self.snapshotter.acknowledge(effect.target)
                reports.append(ViolationReport.for_clause(monitor.clause, evidence))
        return reports

    def _trace_report(self, clause: Clause, event: Event) -> ViolationReport:
        last_pass = max((i for i, e in enumerate(self.trace) if isinstance(e, TestRun) and e.passed), default=None)
        start = 0 if last_pass is None else last_pass + 1
        edits = [e.path for e in self.trace[start:] if e.type == "edit"]
        evidence = {
            "event": event.type,
            "formula": clause.params["formula"],
            "last_passing_test": None if last_pass is None else self.trace[last_pass].ts.isoformat(),
            "edits_since_last_passing_test": edits,
        }
        return ViolationReport.for_clause(clause, evidence)

    def _semantic_reports(self) -> list[ViolationReport]:
        reports = []
        baselines = self.session.get("baselines", {})
        for clause in self.semantic_clauses:
            verifier = SEMANTIC_VERIFIERS.get(clause.checker or "")
            if verifier is None:
                continue
            try:
                reports += verifier(clause, self.repo_root, baselines.get(clause.id))
            except Exception as exc:  # a verifier that cannot run fails closed
                reports.append(
                    ViolationReport.for_clause(
                        clause,
                        {"error": f"{type(exc).__name__}: {exc}"},
                        hints=["The verifier could not run; fix the setup or ask the human"],
                    )
                )
        return reports

    def _log(self, decision: Decision) -> None:
        if not decision.reports:
            return
        counted = sorted({r.violation for r in decision.reports if not _is_resolved(r)})
        self.block_counts.update(counted)
        for r in decision.reports:
            if r.violation in counted and self.block_counts[r.violation] >= self.escalate_after:
                r.escalated = True
                r.options = ["ask_human"]
        self.state_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "counted": counted, **decision.to_dict()}
        with self._reports_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
