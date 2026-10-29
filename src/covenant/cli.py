"""Command-line entry point.

Decisions go to stdout as JSON (for the agent); a short summary goes to stderr
(for humans). Exit codes: 0 clean, 1 violations, 2 needs a human, 3 bad setup.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .events import Commit, Edit, Effect, Push, Run
from .ir import Covenant
from .runtime import Decision, Runtime
from .validate import validate


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="covenant", description=__doc__.splitlines()[0])
    p.add_argument("--repo", default=".", help="repository root (default: current folder)")
    p.add_argument("--covenant", help="Covenant file (default: <repo>/covenant.yaml)")
    p.add_argument("--state-dir", help="where the trace and snapshots live (default: <repo>/.covenant)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="check the Covenant file statically")
    sub.add_parser("start", help="begin a task: reset the trace, snapshot protected data, pin baselines")
    sub.add_parser("status", help="show clauses and the current trace")
    sub.add_parser("test", help="run the declared test command and record the result")
    sub.add_parser("check", help="run the semantic verifiers now")
    sub.add_parser("install-hooks", help="install git pre-commit and pre-push hooks")
    hook = sub.add_parser("hook", help="called by git hooks")
    hook.add_argument("name", choices=["pre-commit", "pre-push"])

    rec = sub.add_parser("record", help="record an event from the harness").add_subparsers(dest="event", required=True)
    rec.add_parser("edit").add_argument("path")
    run = rec.add_parser("run")
    run.add_argument("cmd")
    run.add_argument("--exit-code", type=int, required=True)
    rec.add_parser("commit").add_argument("--sha")
    rec.add_parser("push").add_argument("--remote")
    eff = rec.add_parser("effect")
    eff.add_argument("op")
    eff.add_argument("target")
    eff.add_argument("--db", action="store_true", help="target is a database table")
    return p


def _emit(decision: Decision) -> int:
    print(json.dumps(decision.to_dict(), indent=2))
    if decision.reports:
        status = "refused" if not decision.allowed else "reported"
        print(f"covenant: {decision.event} {status}:", file=sys.stderr)
        for r in decision.reports:
            hint = f" -> {r.repair_hints[0]}" if r.repair_hints else ""
            print(f"  {r.violation} ({r.clause}){hint}", file=sys.stderr)
    return decision.exit_code


def _install_hooks(args: argparse.Namespace, repo: Path, covenant_path: Path) -> int:
    hooks = repo / ".git" / "hooks"
    if not hooks.is_dir():
        print(f"covenant: {repo} is not a git repository", file=sys.stderr)
        return 3
    src = Path(__file__).resolve().parent.parent
    state = f' --state-dir "{Path(args.state_dir).resolve().as_posix()}"' if args.state_dir else ""
    for name in ("pre-commit", "pre-push"):
        script = (
            "#!/bin/sh\n"
            "# Installed by covenant. Blocks the action if the Covenant is violated.\n"
            f'PYTHONPATH="{src.as_posix()}{os.pathsep}$PYTHONPATH" exec "{Path(sys.executable).as_posix()}" '
            f'-m covenant --repo "{repo.as_posix()}" --covenant "{covenant_path.as_posix()}"{state} hook {name}\n'
        )
        path = hooks / name
        path.write_text(script, encoding="utf-8", newline="\n")
        path.chmod(0o755)
        print(f"installed {path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    covenant_path = Path(args.covenant).resolve() if args.covenant else repo / "covenant.yaml"
    try:
        covenant = Covenant.load(covenant_path)
    except (OSError, ValidationError, ValueError) as exc:
        print(f"covenant: cannot load {covenant_path}: {exc}", file=sys.stderr)
        return 3

    if args.command == "validate":
        issues = validate(covenant, repo)
        for issue in issues:
            print(issue)
        errors = [i for i in issues if i.level == "error"]
        print(f"{len(covenant.clauses)} clauses, {len(errors)} errors, {len(issues) - len(errors)} warnings")
        return 3 if errors else 0
    if args.command == "install-hooks":
        return _install_hooks(args, repo, covenant_path)

    runtime = Runtime(covenant, repo, args.state_dir)
    try:
        if args.command == "start":
            print(json.dumps(runtime.start(), indent=2))
            return 0
        if args.command == "status":
            for c in covenant.clauses:
                print(f"{c.id} {c.kind.value} [{c.status.value}] {c.checker}: {c.source_span!r}")
            print(f"trace: {len(runtime.trace)} events; blocks so far: {dict(runtime.block_counts)}")
            return 0
        if args.command == "test":
            code = 0
            for decision, output in runtime.run_tests():
                sys.stderr.write(output)
                code = max(code, _emit(decision))
            return code
        if args.command == "check":
            return _emit(runtime.check())
        if args.command == "hook":
            return _emit(runtime.record(Commit() if args.name == "pre-commit" else Push()))
        if args.command == "record":
            event = {
                "edit": lambda: Edit(path=args.path),
                "run": lambda: Run(cmd=args.cmd, exit_code=args.exit_code),
                "commit": lambda: Commit(sha=args.sha),
                "push": lambda: Push(remote=args.remote),
                "effect": lambda: Effect(op=args.op, target=args.target, channel="db" if args.db else "fs"),
            }[args.event]()
            return _emit(runtime.record(event))
    except ValueError as exc:
        print(f"covenant: {exc}", file=sys.stderr)
        return 3
    return 3
