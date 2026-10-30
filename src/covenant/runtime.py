"""The runtime: judges every event against the approved Covenant. No LLM is involved.

State lives in `state_dir` (outside the repo by default, see paths.py) so separate
processes (Claude Code hooks, git hooks, the CLI) share one trace:

    session.json            start time, pinned baselines, hashes of Covenant's own files
    covenant.approved.yaml  the Covenant as approved at start; edits to the repo copy don't count
    trace.jsonl             every event that happened, replayed on load
    reports.jsonl           every decision that raised a violation, for audit
    snapshot/               copies of protected files, for rollback
    lock                    serializes concurrent hooks
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import EVENT_ADAPTER, GATED_EVENTS, Commit, Effect, Event, TestRun
from .feedback import ViolationReport
from .ir import Clause, ClauseKind, Covenant, OnViolation
from .locking import file_lock
from .monitors.effects import EffectMonitor, normalize_path
from .monitors.snapshot import CopySnapshotter
from .monitors.trace import ATOMS, TraceMonitor
from . import policy
from .paths import claude_settings_files, default_state_dir
from .verifiers import api_surface, dependency_diff, schema_guard, source_api, sqlite_guard, ts_api_surface
from .verifiers.test_gate import run_tests

SemanticVerifier = Callable[[Clause, Path, "str | None"], list[ViolationReport]]
SEMANTIC_VERIFIERS: dict[str, SemanticVerifier] = {
    "api_surface_diff": api_surface.verify,
    "ts_api_surface_diff": ts_api_surface.verify,
    "schema_guard": schema_guard.verify,
    "dependency_diff": dependency_diff.verify,
    "source_api_diff": source_api.verify,
}
MISSING = "<missing>"


@dataclass
class Decision:
    event: str
    allowed: bool
    reports: list[ViolationReport] = field(default_factory=list)

    @property
    def escalated(self) -> bool:
        return any(r.escalated or r.on_violation is OnViolation.ASK_HUMAN for r in self.reports)

    @property
    def blocking(self) -> list[ViolationReport]:
        return [r for r in self.reports if not is_resolved(r)]

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


def is_resolved(report: ViolationReport) -> bool:
    """An effect (or lost database rows) rolled back successfully no longer needs to block anything."""
    restorable = report.kind is ClauseKind.EFFECT_PROHIBITION or report.checker == "sqlite_guard"
    return restorable and report.evidence.get("rolled_back") is True


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else MISSING


class Runtime:
    def __init__(
        self,
        covenant: Covenant,
        repo_root: str | Path,
        state_dir: str | Path | None = None,
        escalate_after: int = 5,
        covenant_path: str | Path | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.state_dir = Path(state_dir).resolve() if state_dir else default_state_dir(self.repo_root)
        self.covenant_path = Path(covenant_path).resolve() if covenant_path else self.repo_root / "covenant.yaml"
        self.escalate_after = escalate_after
        self._configure(covenant)
        with self._lock():
            self._load_state()

    @classmethod
    def open(
        cls,
        repo_root: str | Path,
        covenant_path: str | Path | None = None,
        state_dir: str | Path | None = None,
        pinned: bool = True,
        **kwargs: Any,
    ) -> Runtime | None:
        """Load the approved Covenant for a repo (or the repo's file if none is approved yet)."""
        repo = Path(repo_root).resolve()
        cov_path = Path(covenant_path).resolve() if covenant_path else repo / "covenant.yaml"
        sdir = Path(state_dir).resolve() if state_dir else default_state_dir(repo)
        approved = sdir / "covenant.approved.yaml"
        if pinned and approved.exists():
            covenant = Covenant.load(approved)
        elif cov_path.exists():
            covenant = policy.load(cov_path)[0]
        else:
            return None
        return cls(covenant, repo, sdir, covenant_path=cov_path, **kwargs)

    def _configure(self, covenant: Covenant) -> None:
        self.covenant = covenant
        self.effect_monitors = [
            EffectMonitor(c, self.repo_root) for c in covenant.enforced(ClauseKind.EFFECT_PROHIBITION)
        ]
        self.semantic_clauses = covenant.enforced(ClauseKind.SEMANTIC_INVARIANT)
        self.sqlite_clauses = [c for c in self.semantic_clauses if c.checker == "sqlite_guard"]

    # ----- state -------------------------------------------------------------

    def _lock(self):
        return file_lock(self.state_dir / "lock")

    @property
    def has_session(self) -> bool:
        return (self.state_dir / "session.json").exists()

    @property
    def approved_path(self) -> Path:
        return self.state_dir / "covenant.approved.yaml"

    def _fresh_monitors(self) -> None:
        self.trace_monitors = [
            (c, TraceMonitor(c.params["formula"]))
            for c in self.covenant.enforced(ClauseKind.TEMPORAL_OBLIGATION)
        ]
        self.trace: list[Event] = []

    def _read_lines(self, name: str) -> list[str]:
        path = self.state_dir / name
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _load_state(self) -> None:
        self._fresh_monitors()
        self.snapshotter = CopySnapshotter(self.state_dir, self.repo_root)
        session_path = self.state_dir / "session.json"
        self.session: dict[str, Any] = (
            json.loads(session_path.read_text(encoding="utf-8")) if session_path.exists() else {}
        )
        starts = self.session.get("clause_start", {})  # rules added mid-task apply from then on
        for n, line in enumerate(self._read_lines("trace.jsonl")):
            event = EVENT_ADAPTER.validate_json(line)
            for clause, monitor in self.trace_monitors:
                if n >= starts.get(clause.id, 0):
                    monitor.step(event)
            self.trace.append(event)
        self.block_counts: Counter[str] = Counter()
        for line in self._read_lines("reports.jsonl"):
            self.block_counts.update(json.loads(line).get("counted", []))

    def _integrity_files(self) -> list[Path]:
        hooks = self.repo_root / ".git" / "hooks"
        return [self.covenant_path, hooks / "pre-commit", hooks / "pre-push", claude_settings_files(self.repo_root)[0]]

    def start(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Begin a task: approve the Covenant file, reset the trace, snapshot data, pin baselines."""
        with self._lock():
            self.state_dir.mkdir(parents=True, exist_ok=True)
            policy_files: list[Path] = []
            if self.covenant_path.exists():
                merged, policy_files = policy.load(self.covenant_path)
                self._configure(merged)
                merged.dump(self.approved_path)  # team policies merged in; edits to the sources don't count
            for name in ("trace.jsonl", "reports.jsonl"):
                (self.state_dir / name).unlink(missing_ok=True)
            self._fresh_monitors()
            self.block_counts = Counter()
            baselines = {}
            for clause in self.semantic_clauses:
                ref = clause.params.get("baseline")
                if ref:
                    baselines[clause.id] = self._git_rev_parse(ref)
            self.snapshotter = CopySnapshotter(self.state_dir, self.repo_root)
            files = self.snapshotter.take(self.effect_monitors)
            shutil.rmtree(self.state_dir / "sqlite", ignore_errors=True)
            databases = sum(sqlite_guard.Snapshot(self.state_dir, c).take(c, self.repo_root) for c in self.sqlite_clauses)
            self.session = {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "repo": str(self.repo_root),
                "baselines": baselines,
                "snapshot_files": files,
                "snapshot_databases": databases,
                "integrity": {str(p): _file_hash(p) for p in self._integrity_files() + policy_files},
                "sources": {str(p): _file_hash(p) for p in [self.covenant_path, *policy_files]},
                "policy_files": [str(p) for p in policy_files],
                **(extra or {}),
            }
            self._write_session()
            return self.session

    def amend(self) -> list[str]:
        """Approve new rules mid-task. They apply from now on; the trace, snapshots and
        baselines of the rules already approved stay as they are.

        Only additions are accepted: a changed or removed rule, or any other tampering
        with Covenant's setup, needs `covenant start`, which a human runs.
        """
        with self._lock():
            self._load_state()
            if not self.session:
                raise ValueError("no task in progress; run `covenant start` instead")
            sources = set(self.session.get("sources", {}))
            other = [r for r in self._integrity_reports() if r.evidence.get("path") not in sources]
            if other:
                raise ValueError("Covenant's setup was changed (" + "; ".join(
                    f"{r.evidence['path']}: {r.evidence['problem']}" for r in other) + "); only `covenant start` can approve that")
            old = {c.id: c for c in self.covenant.clauses}
            merged, policy_files = policy.load(self.covenant_path)
            new_ids = {c.id for c in merged.clauses}
            touched = sorted([i for i in old if i not in new_ids] + [c.id for c in merged.clauses if c.id in old and c != old[c.id]])
            if touched:
                raise ValueError(f"amend only adds rules, but {', '.join(touched)} changed or disappeared; "
                                 "run `covenant start` to re-approve from scratch")
            added = [c for c in merged.clauses if c.id not in old]
            if not added:
                return []
            self._configure(merged)
            merged.dump(self.approved_path)
            ids = {c.id for c in added}
            starts = self.session.setdefault("clause_start", {})
            baselines = self.session.setdefault("baselines", {})
            for clause in added:
                starts[clause.id] = len(self.trace)
                ref = clause.params.get("baseline") if clause.kind is ClauseKind.SEMANTIC_INVARIANT else None
                if ref:
                    baselines[clause.id] = self._git_rev_parse(ref)
            self.session["snapshot_files"] = self.session.get("snapshot_files", 0) + self.snapshotter.extend(
                [m for m in self.effect_monitors if m.clause.id in ids])
            self.session["snapshot_databases"] = self.session.get("snapshot_databases", 0) + sum(
                sqlite_guard.Snapshot(self.state_dir, c).take(c, self.repo_root) for c in self.sqlite_clauses if c.id in ids)
            self.session["integrity"] = {str(p): _file_hash(p) for p in self._integrity_files() + policy_files}
            self.session["sources"] = {str(p): _file_hash(p) for p in [self.covenant_path, *policy_files]}
            self.session["policy_files"] = [str(p) for p in policy_files]
            self.session.setdefault("amendments", []).append(
                {"at": datetime.now(timezone.utc).isoformat(), "clauses": sorted(ids), "trace_index": len(self.trace)})
            self._write_session()
            self._load_state()
            return [c.id for c in added]

    def _write_session(self) -> None:
        (self.state_dir / "session.json").write_text(json.dumps(self.session, indent=2), encoding="utf-8")

    def update_session(self, **values: Any) -> None:
        with self._lock():
            self._load_state()
            self.session.update(values)
            self._write_session()

    def repo_covenant_changed(self) -> bool:
        """True when covenant.yaml or a policy it uses changed since approval."""
        if not self.approved_path.exists():
            return False
        sources = self.session.get("sources")
        if not sources:
            return _file_hash(self.covenant_path) != _file_hash(self.approved_path)
        return any(_file_hash(Path(path)) != digest for path, digest in sources.items())

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

    def record(self, event: Event, dry_run: bool = False) -> Decision:
        """Judge an event. Gated events (commit, push) are refused if anything blocks.

        With dry_run, the event is judged but not added to the trace: used to check a
        commit before it runs, leaving the authoritative record to the git hook.
        """
        with self._lock():
            self._load_state()
            reports = self._integrity_reports()
            reports += self._handle_effects(self.snapshotter.scan())
            reports += self._sqlite_reports()
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

            blocking = [r for r in reports if not is_resolved(r)]
            refused = gated and bool(blocking)
            if refused:
                for r in blocking:
                    r.blocked_action = f"git {event.type}"
            elif not dry_run:
                for (_, monitor), state in zip(self.trace_monitors, pending):
                    monitor.advance(state)
                self.trace.append(event)
                self.state_dir.mkdir(parents=True, exist_ok=True)
                with (self.state_dir / "trace.jsonl").open("a", encoding="utf-8") as f:
                    f.write(event.model_dump_json() + "\n")

            decision = Decision(event.type, not refused and not own_effect_reports, reports)
            self._log(decision)
            return decision

    def integrity_reports(self) -> list[ViolationReport]:
        """Tampering checks only (Covenant's own files, hook bypass settings)."""
        with self._lock():
            self._load_state()
            return self._integrity_reports()

    def would_violate(self, effect: Effect) -> list[ViolationReport]:
        """Reports for an effect that has not happened yet, so it can be denied up front."""
        target = normalize_path(effect.target, self.repo_root) if effect.channel == "fs" else effect.target
        return [
            ViolationReport.for_clause(m.clause, {"op": effect.op, "target": target, "channel": effect.channel})
            for m in self.effect_monitors
            if m.violates(effect)
        ]

    def preview(self) -> Decision:
        """What a commit would face right now, with no side effects: nothing is logged, nothing
        counts toward escalation, and changed protected files are reported, not rolled back.
        Used by editors, which check often and must not undo the human's own edits."""
        with self._lock():
            self._load_state()
            reports = self._integrity_reports() + self._semantic_reports() + self._sqlite_reports(apply=False)
            for effect in self.snapshotter.scan():
                for monitor in self.effect_monitors:
                    if monitor.violates(effect):
                        evidence = {"op": effect.op, "target": effect.target, "channel": effect.channel, "pending": True}
                        reports.append(ViolationReport.for_clause(monitor.clause, evidence))
            for clause, monitor in self.trace_monitors:
                ok, _ = monitor.peek(Commit())
                if not ok:
                    reports.append(self._trace_report(clause, Commit()))
            return Decision("preview", not reports, reports)

    def recent_reports(self, limit: int = 30) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self._read_lines("reports.jsonl")[-limit:]]

    def check(self, checkpoint: str = "manual") -> Decision:
        """Run every check now without adding an event. Used at task end, and by the agent early."""
        with self._lock():
            self._load_state()
            reports = (
                self._integrity_reports()
                + self._handle_effects(self.snapshotter.scan())
                + self._sqlite_reports()
                + self._semantic_reports()
            )
            decision = Decision(f"check:{checkpoint}", not any(not is_resolved(r) for r in reports), reports)
            self._log(decision)
            return decision

    @property
    def test_commands(self) -> list[str]:
        return list(
            dict.fromkeys(
                c.params["test_cmd"]
                for c in self.covenant.enforced(ClauseKind.TEMPORAL_OBLIGATION)
                if "test_cmd" in c.params
            )
        )

    def run_tests(self) -> list[tuple[Decision, str]]:
        """Run each distinct test_cmd declared by temporal clauses and record the results."""
        if not self.test_commands:
            raise ValueError("no clause declares a test_cmd")
        results = []
        for cmd in self.test_commands:
            event, output = run_tests(cmd, self.repo_root)
            results.append((self.record(event), output))
        return results

    # ----- helpers -----------------------------------------------------------

    def _integrity_reports(self) -> list[ViolationReport]:
        problems: list[dict[str, Any]] = []
        for path, expected in self.session.get("integrity", {}).items():
            found = _file_hash(Path(path))
            if found != expected:
                problems.append({"path": path, "problem": "changed since approval" if found != MISSING else "deleted"})
        for settings in claude_settings_files(self.repo_root):
            try:
                if json.loads(settings.read_text(encoding="utf-8")).get("disableAllHooks"):
                    problems.append({"path": str(settings), "problem": "disableAllHooks is set"})
            except (OSError, ValueError, AttributeError):
                pass
        git_config = self.repo_root / ".git" / "config"
        if git_config.is_file() and "hookspath" in git_config.read_text(encoding="utf-8", errors="ignore").lower():
            problems.append({"path": str(git_config), "problem": "core.hooksPath redirects git hooks"})
        return [
            ViolationReport(
                violation="INTEGRITY",
                clause="Covenant's own files must not change during a task",
                checker="integrity",
                on_violation=OnViolation.ASK_HUMAN,
                evidence=p,
                repair_hints=["Restore the file. Only a human can change Covenant's setup (`covenant start` re-approves it)"],
                options=["ask_human"],
            )
            for p in problems
        ]

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

    def _sqlite_reports(self, apply: bool = True) -> list[ViolationReport]:
        """Row-level checks of protected SQLite databases; lost data is put back when apply is set."""
        reports = []
        for clause in self.sqlite_clauses:
            try:
                reports += sqlite_guard.check(
                    clause, self.repo_root, self.state_dir, clause.on_violation is OnViolation.ROLLBACK, apply=apply)
            except Exception as exc:  # fail closed
                reports.append(ViolationReport.for_clause(
                    clause, {"error": f"{type(exc).__name__}: {exc}"},
                    hints=["The database check could not run; fix the setup or ask the human"]))
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
        formula = clause.params["formula"]
        hints = None
        counted = re.findall(r"#\s*([a-z_]+)", formula)
        if counted:
            since = self.trace[self.session.get("clause_start", {}).get(clause.id, 0):]
            evidence["counts_so_far"] = {a: sum(1 for e in since if ATOMS[a](e)) for a in counted}
            hints = [f"This task allows only a limited number of these actions ({formula}) and the limit is reached. "
                     "Finish without it, or ask the human to raise the limit with `covenant amend`"]
        elif "S[" in formula and "test" in formula:
            cmd = clause.params.get("test_cmd")
            hints = [f"Run the tests now{f' (`{cmd}`)' if cmd else ''}: edits must be followed by a test run "
                     "within the number of steps this task allows"]
        return ViolationReport.for_clause(clause, evidence, hints=hints)

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
        counted = sorted({r.violation for r in decision.blocking})
        self.block_counts.update(counted)
        for r in decision.reports:
            if r.violation in counted and self.block_counts[r.violation] >= self.escalate_after:
                r.escalated = True
                r.options = ["ask_human"]
        self.state_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "counted": counted, **decision.to_dict()}
        with (self.state_dir / "reports.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
