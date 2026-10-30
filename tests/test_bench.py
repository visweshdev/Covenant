from pathlib import Path

import pytest

from covenant.bench import runner
from covenant.bench.task import GroundTruthClause, load_tasks
from covenant.ir import Covenant

TASKS = Path(__file__).resolve().parent.parent / "bench" / "tasks"


@pytest.fixture(scope="module")
def tasks():
    return {t.id: t for t in load_tasks(TASKS)}


def test_all_tasks_load(tasks):
    assert {"parse-mode-api", "migration-nickname", "deps-only-requests", "timeout-protected-config",
            "cache-cleanup-ambiguous"} <= set(tasks)
    for t in tasks.values():
        assert t.oracle and t.acceptance and t.ground_truth and {"naive", "careful"} <= set(t.scripted)


def test_prompt_only_baseline_violates(tasks, tmp_path):
    r = runner.run_task(tasks["parse-mode-api"], "prompt", workdir=tmp_path)
    assert r.success and set(r.violations) == {"user data intact", "parse keeps its strict parameter"}


def test_covenant_prevents_and_agent_recovers(tasks, tmp_path):
    r = runner.run_task(tasks["parse-mode-api"], "covenant", workdir=tmp_path)
    assert r.violations == [] and r.success and r.blocked == 1 and r.recovered
    assert r.fidelity == {"precision": 1.0, "recall": 1.0}


def test_unanswered_ambiguity_over_restricts(tasks, tmp_path):
    t = tasks["cache-cleanup-ambiguous"]
    unattended = runner.run_task(t, "covenant-unattended", workdir=tmp_path / "a")
    assert unattended.violations == [] and not unattended.success and unattended.over_restricted
    assert unattended.asked_expected is True
    answered = runner.run_task(t, "covenant", workdir=tmp_path / "b")
    assert answered.violations == [] and answered.success


def test_fidelity_scoring():
    cov = Covenant.model_validate({"instruction": "x", "goal": {"action": "x"}, "clauses": [
        {"id": "C1", "kind": "EffectProhibition", "source_span": "x", "checker": "effect_monitor", "params": {"paths": ["data/**", "x/**"]}},
        {"id": "C2", "kind": "TemporalObligation", "source_span": "y", "checker": "trace_monitor", "params": {"formula": "G(!push)"}},
    ]})
    truth = [GroundTruthClause(checker="effect_monitor", params={"paths": ["data/**"]}),
             GroundTruthClause(checker="api_surface_diff", params={"package": "p"})]
    assert runner.fidelity(cov, truth) == {"precision": 0.5, "recall": 0.5}


def test_summary_metrics():
    rows = [
        {"task": "a", "mode": "m", "agent": "s", "success": True, "violations": [], "blocked": 1, "recovered": True,
         "over_restricted": False, "fidelity": {"precision": 1, "recall": 1}, "asked_expected": None, "seconds": 2},
        {"task": "b", "mode": "m", "agent": "s", "success": False, "violations": [], "blocked": 2, "recovered": False,
         "over_restricted": True, "fidelity": {"precision": 0.5, "recall": 1}, "asked_expected": True, "seconds": 4},
    ]
    s = runner.summarize(rows)["m/s"]
    assert s["task_success"] == 0.5 and s["over_restriction"] == 1 and s["feedback_recovery"] == 0.5
    assert s["fidelity_precision"] == 0.75 and s["asked_the_right_question"] == 1.0
    assert "| m/s |" in runner.render_report(runner.summarize(rows))
