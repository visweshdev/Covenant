"""Measure decisions on labeled PreToolUse events without executing their commands."""

import argparse
import csv
import json
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from covenant import policy
from covenant.integrations.claude_code import ClaudeCodeHook
from covenant.ir import Clause, ClauseKind
from covenant.runtime import Runtime

BENCH = Path(__file__).resolve().parent


def measure(root: Path, cases: Path, repeats: int) -> dict:
    repo = root / "repo"
    shutil.copytree(
        BENCH.parent / "examples" / "mylib",
        repo,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    for args in [["init", "-q"], ["add", "."], ["commit", "-qm", "baseline"]]:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Bench",
                "-c",
                "user.email=bench@example.invalid",
                *args,
            ],
            cwd=repo,
            check=True,
        )
    rules_path = repo / "covenant.yaml"
    rules = policy.load(rules_path)[0]
    rules.clauses.append(
        Clause(
            id="C4",
            kind=ClauseKind.EFFECT_PROHIBITION,
            source_span="Keep history and avoid force pushes, deployment, publishing and new packages",
            checker="command_guard",
            params={
                "deny": [
                    "force_push",
                    "history_rewrite",
                    "deploy",
                    "publish",
                    "install_packages",
                ]
            },
        )
    )
    rules.dump(rules_path)
    runtime = Runtime(rules, repo, root / "state", covenant_path=rules_path)
    runtime.start()
    hook = ClaudeCodeHook(runtime)

    baseline_ns = []
    enforced_ns = []
    decisions = []
    counts = {
        name: 0
        for name in (
            "true_positive",
            "false_positive",
            "true_negative",
            "false_negative",
        )
    }
    with cases.open(encoding="utf-8", newline="") as f:
        for case in csv.DictReader(f, delimiter="\t"):
            expected = case["expected"]
            if expected not in {"allow", "block"}:
                raise ValueError(f"Unknown label {expected!r} for {case['id']}")
            tool = case["tool"]
            value = case["input"]
            tool_input = (
                {"file_path": str(repo / value)}
                if tool in {"Edit", "Write"}
                else {"command": value}
            )
            event = json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": tool,
                    "tool_input": tool_input,
                }
            )
            observed = set()
            for _ in range(repeats):
                start = time.perf_counter_ns()
                json.loads(event)
                baseline_ns.append(time.perf_counter_ns() - start)
                start = time.perf_counter_ns()
                response = hook.handle(json.loads(event))
                enforced_ns.append(time.perf_counter_ns() - start)
                denied = (
                    response.get("hookSpecificOutput", {}).get("permissionDecision")
                    == "deny"
                )
                observed.add("block" if denied else "allow")
            if len(observed) != 1:
                raise RuntimeError(f"Unstable decision for {case['id']}: {observed}")
            actual = observed.pop()
            decisions.append({"id": case["id"], "expected": expected, "actual": actual})
            accuracy = "true" if actual == expected else "false"
            sign = "positive" if actual == "block" else "negative"
            counts[f"{accuracy}_{sign}"] += 1

    if not decisions:
        raise ValueError(f"No cases in {cases}")
    tp, fp, tn, fn = (
        counts[key]
        for key in (
            "true_positive",
            "false_positive",
            "true_negative",
            "false_negative",
        )
    )
    paired_ns = sorted(
        enforced - baseline
        for enforced, baseline in zip(enforced_ns, baseline_ns, strict=True)
    )
    p95 = (
        statistics.quantiles(paired_ns, n=100, method="inclusive")[94]
        if len(paired_ns) > 1
        else paired_ns[0]
    )
    return {
        **counts,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "legitimate_allow_rate": tn / (tn + fp) if tn + fp else None,
        "baseline_median_ms": statistics.median(baseline_ns) / 1e6,
        "enforced_median_ms": statistics.median(enforced_ns) / 1e6,
        "paired_overhead_median_ms": statistics.median(paired_ns) / 1e6,
        "paired_overhead_p95_ms": p95 / 1e6,
        "decisions": decisions,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=BENCH / "enforcement-cases.tsv")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if any decision differs from its label",
    )
    args = parser.parse_args()
    if args.repeats <= 0:
        parser.error("--repeats must be positive")
    with tempfile.TemporaryDirectory(prefix="covenant-measure-") as temp:
        results = measure(Path(temp), args.cases, args.repeats)
    print(json.dumps(results, indent=2))
    if args.check and (results["false_positive"] or results["false_negative"]):
        raise SystemExit(1)
