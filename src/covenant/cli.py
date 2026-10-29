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
from .integrations import claude_code
from .ir import Covenant
from .paths import default_state_dir
from .runtime import Decision, Runtime
from .validate import validate


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="covenant", description=__doc__.splitlines()[0])
    p.add_argument("--repo", default=".", help="repository root (default: current folder)")
    p.add_argument("--covenant", help="Covenant file (default: <repo>/covenant.yaml)")
    p.add_argument("--state-dir", help="where the trace and snapshots live (default: ~/.covenant/workspaces/...)")
    sub = p.add_subparsers(dest="command", required=True)

    ci = sub.add_parser("ci", help="check a pull request (base..HEAD) against the Covenant")
    ci.add_argument("--base", help="base ref (default: merge-base with the PR target or main)")
    ci.add_argument("--no-tests", action="store_true", help="don't run the test commands")
    ci.add_argument("--format", choices=["text", "github", "json"], default="text")
    comp = sub.add_parser("compile", help="turn a plain-English instruction into covenant.yaml")
    comp.add_argument("instruction", help="the instruction you would give the agent")
    comp.add_argument("--offline", action="store_true", help="use the pattern extractor instead of Claude")
    comp.add_argument("--model", default=None, help="Claude model for extraction (default: claude-opus-5-5)")
    comp.add_argument("--answer", action="append", default=[], metavar="Q1=2", help="answer a question (1-based option)")
    comp.add_argument("--unattended", action="store_true", help="ask nothing; take the conservative reading")
    comp.add_argument("--yes", action="store_true", help="write the file without asking for approval")
    comp.add_argument("--dry-run", action="store_true", help="print the result without writing it")
    init = sub.add_parser("init", help="set up a repo: validate, install git and Claude Code hooks, start")
    init.add_argument("--no-claude", action="store_true", help="skip the Claude Code hooks")
    init.add_argument("--no-git", action="store_true", help="skip the git hooks")
    init.add_argument("--local", action="store_true", help="write Claude hooks to .claude/settings.local.json")
    sub.add_parser("validate", help="check the Covenant file statically")
    sub.add_parser("start", help="approve the Covenant file and begin a task")
    sub.add_parser("status", help="show clauses and the current trace")
    sub.add_parser("test", help="run the declared test command and record the result")
    sub.add_parser("check", help="run every check now")
    sub.add_parser("install-hooks", help="install git pre-commit and pre-push hooks")
    claude = sub.add_parser("install-claude", help="install Claude Code hooks")
    claude.add_argument("--local", action="store_true", help="write to .claude/settings.local.json")
    sub.add_parser("claude-hook", help="called by Claude Code hooks (reads JSON on stdin)")
    sub.add_parser("mcp", help="run the MCP server on stdio (for Cursor, Codex and other agents)")
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


def _install_git_hooks(repo: Path, covenant_path: Path, state_dir: str | None) -> int:
    hooks = repo / ".git" / "hooks"
    if not hooks.is_dir():
        print(f"covenant: {repo} is not a git repository", file=sys.stderr)
        return 3
    src = Path(__file__).resolve().parent.parent
    state = f' --state-dir "{Path(state_dir).resolve().as_posix()}"' if state_dir else ""
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


def _install_claude(repo: Path, state_dir: str | None, local: bool) -> int:
    name = "settings.local.json" if local else "settings.json"
    path = claude_code.install(repo, repo / ".claude" / name, Path(state_dir).resolve() if state_dir else None)
    print(f"installed Claude Code hooks in {path}", file=sys.stderr)
    if not claude_code.importable_outside_source():
        print(
            "covenant: warning: the hooks run `python -m covenant`, but this Python can't import covenant "
            "from another folder. Run `pip install -e .` in the Covenant checkout.",
            file=sys.stderr,
        )
    return 0


def _ci(args: argparse.Namespace, repo: Path, covenant_path: Path) -> int:
    from . import ci

    try:
        result = ci.run_ci(repo, covenant_path, args.base, run_test_commands=not args.no_tests)
    except (ValueError, OSError, ValidationError) as exc:
        print(f"covenant: {exc}", file=sys.stderr)
        return 3
    if args.format == "json":
        print(json.dumps({"base": result.base, "covenant": result.covenant_source, "notes": result.notes,
                          "reports": [r.model_dump(mode="json") for r in result.reports]}, indent=2))
    elif args.format == "github":
        for line in ci.github_annotations(result):
            print(line)
        summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(ci.markdown_summary(result))
    else:
        print(ci.markdown_summary(result))
    return 1 if result.failed else 0


def _compile(args: argparse.Namespace, repo: Path, covenant_path: Path) -> int:
    from .compiler.extract import CompileError, HeuristicExtractor, LLMExtractor
    from .compiler.pipeline import compile_instruction, render_summary

    answers: dict[str, int] = {}
    for item in args.answer:
        qid, _, choice = item.partition("=")
        if not choice.isdigit():
            print(f"covenant: --answer must look like Q1=2, got {item!r}", file=sys.stderr)
            return 3
        answers[qid.strip().upper()] = int(choice) - 1

    interactive = sys.stdin.isatty() and not args.unattended

    def ask(question) -> int:
        print(f"\n{question.id}. {question.text}", file=sys.stderr)
        for n, option in enumerate(question.options, 1):
            mark = " (default)" if n - 1 == question.default else ""
            print(f"  {n}. {option.label}{mark}", file=sys.stderr)
        while True:
            reply = input(f"Choose 1-{len(question.options)} [{question.default + 1}]: ").strip()
            if not reply:
                return question.default
            if reply.isdigit() and 1 <= int(reply) <= len(question.options):
                return int(reply) - 1

    extractor = HeuristicExtractor() if args.offline else LLMExtractor(**({"model": args.model} if args.model else {}))
    try:
        result = compile_instruction(args.instruction, repo, extractor, ask=ask if interactive else None, answers=answers)
    except CompileError as exc:
        print(f"covenant: {exc}", file=sys.stderr)
        return 3
    print(render_summary(result), file=sys.stderr)
    if result.defaulted:
        print(f"\n{len(result.defaulted)} lower-impact question(s) took the conservative default (see 'assumes').", file=sys.stderr)
    if args.dry_run:
        print(Path(covenant_path).name + ":", file=sys.stderr)
        import yaml

        print(yaml.safe_dump({"covenant": result.covenant.model_dump(mode="json", exclude_defaults=True)}, sort_keys=False))
        return 3 if result.errors else 0
    if result.errors:
        print("\ncovenant: fix the errors above before using this Covenant.", file=sys.stderr)
        return 3
    if covenant_path.exists() and not args.yes:
        print(f"\nnote: {covenant_path} exists and will be replaced.", file=sys.stderr)
    if not args.yes:
        if not interactive:
            print("\ncovenant: pass --yes to write the file without a prompt.", file=sys.stderr)
            return 3
        if input(f"\nWrite {covenant_path}? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            return 1
    result.covenant.dump(covenant_path)
    print(f"wrote {covenant_path}. Next: `covenant init` to install hooks and start enforcing.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    covenant_path = Path(args.covenant).resolve() if args.covenant else repo / "covenant.yaml"

    if args.command == "claude-hook":
        output, code = claude_code.run_hook(sys.stdin.read(), repo, args.state_dir, args.covenant)
        if output:
            print(json.dumps(output))
        return code

    if args.command == "mcp":
        from .integrations.mcp_server import CovenantMcpServer

        CovenantMcpServer(repo, args.covenant, args.state_dir).serve()
        return 0
    if args.command == "compile":
        return _compile(args, repo, covenant_path)
    if args.command == "ci":
        return _ci(args, repo, covenant_path)

    try:
        covenant = Covenant.load(covenant_path)
    except (OSError, ValidationError, ValueError) as exc:
        print(f"covenant: cannot load {covenant_path}: {exc}", file=sys.stderr)
        if not covenant_path.exists():
            print('covenant: create one with `covenant compile "<your instruction>"`', file=sys.stderr)
        return 3

    if args.command == "validate":
        issues = validate(covenant, repo)
        for issue in issues:
            print(issue)
        errors = [i for i in issues if i.level == "error"]
        print(f"{len(covenant.clauses)} clauses, {len(errors)} errors, {len(issues) - len(errors)} warnings")
        return 3 if errors else 0
    if args.command == "install-hooks":
        return _install_git_hooks(repo, covenant_path, args.state_dir)
    if args.command == "install-claude":
        return _install_claude(repo, args.state_dir, args.local)
    if args.command == "init":
        errors = [i for i in validate(covenant, repo) if i.level == "error"]
        for issue in errors:
            print(issue, file=sys.stderr)
        if errors:
            return 3
        if not args.no_git and _install_git_hooks(repo, covenant_path, args.state_dir):
            return 3
        if not args.no_claude:
            _install_claude(repo, args.state_dir, args.local)
        session = Runtime(covenant, repo, args.state_dir, covenant_path=covenant_path).start()
        state = args.state_dir or default_state_dir(repo)
        print(f"covenant: {len(covenant.enforced())} rules enforced; state in {state}", file=sys.stderr)
        print(json.dumps(session, indent=2))
        return 0

    if args.command == "start":
        runtime = Runtime(covenant, repo, args.state_dir, covenant_path=covenant_path)
    else:
        runtime = Runtime.open(repo, covenant_path, args.state_dir)
        assert runtime is not None  # the file loaded above
    try:
        if args.command == "start":
            print(json.dumps(runtime.start(), indent=2))
            return 0
        if args.command == "status":
            for c in runtime.covenant.clauses:
                print(f"{c.id} {c.kind.value} [{c.status.value}] {c.checker}: {c.source_span!r}")
            print(f"state: {runtime.state_dir}")
            print(f"trace: {len(runtime.trace)} events; blocks so far: {dict(runtime.block_counts)}")
            if runtime.repo_covenant_changed():
                print("note: covenant.yaml differs from the approved copy; run `covenant start` to approve it")
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
