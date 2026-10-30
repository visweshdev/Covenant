"""Read-only status of a repo's Covenant session, shared by the CLI, the editor and the dashboard."""

from __future__ import annotations

from .runtime import Runtime


def status_json(runtime: Runtime) -> dict:
    """Everything an editor or dashboard needs about one repo's Covenant, as plain JSON."""
    return {
        "repo": str(runtime.repo_root),
        "covenant_path": str(runtime.covenant_path),
        "state_dir": str(runtime.state_dir),
        "has_session": runtime.has_session,
        "session": {k: runtime.session.get(k) for k in ("started_at", "baselines", "claude_session_id")},
        "repo_covenant_changed": runtime.repo_covenant_changed(),
        "clauses": [c.model_dump(mode="json") for c in runtime.covenant.clauses],
        "protected": [
            {"clause": m.clause.id, "paths": m.path_patterns, "exclude": m.exclude_patterns}
            for m in runtime.effect_monitors
        ],
        "test_commands": runtime.test_commands,
        "trace_length": len(runtime.trace),
        "last_events": [e.model_dump(mode="json") for e in runtime.trace[-10:]],
        "block_counts": dict(runtime.block_counts),
        "escalate_after": runtime.escalate_after,
        "recent_reports": runtime.recent_reports(),
    }
