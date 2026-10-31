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
from . import __version__, policy
from .ir import Covenant
from .paths import default_state_dir
from .runtime import Decision, Runtime
from .status import status_json
from .validate import validate


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="covenant", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"covenant {__version__}")
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
    comp.add_argument("--no-review", action="store_true",
                      help="mark rules no checker covers as unenforceable instead of having Claude review commits against them")
    comp.add_argument("--backend", choices=["auto", "api", "claude-code"], default="auto",
                      help="how to reach Claude: the API key, or your Claude Code login (auto: API key if set)")
    comp.add_argument("--model", default=None, help="Claude model for extraction (default: claude-opus-5-5)")
    comp.add_argument("--answer", action="append", default=[], metavar="Q1=2", help="answer a question (1-based option)")
    comp.add_argument("--unattended", action="store_true", help="ask nothing; take the conservative reading")
    comp.add_argument("--yes", action="store_true", help="write the file without asking for approval")
    comp.add_argument("--dry-run", action="store_true", help="print the result without writing it")
    comp.add_argument("--format", choices=["text", "json"], default="text", help="json: machine-readable dry run (for editors)")
    amend = sub.add_parser("amend", help="add rules to the Covenant mid-task, keeping the task's history")
    amend.add_argument("instruction", help="the extra instruction, e.g. \"also don't touch config/\"")
    amend.add_argument("--offline", action="store_true", help="use the pattern extractor instead of Claude")
    amend.add_argument("--no-review", action="store_true",
                      help="mark rules no checker covers as unenforceable instead of having Claude review commits against them")
    amend.add_argument("--backend", choices=["auto", "api", "claude-code"], default="auto",
                       help="how to reach Claude: the API key, or your Claude Code login (auto: API key if set)")
    amend.add_argument("--model", default=None, help="Claude model for extraction (default: claude-opus-5-5)")
    amend.add_argument("--answer", action="append", default=[], metavar="Q1=2", help="answer a question (1-based option)")
    amend.add_argument("--unattended", action="store_true", help="ask nothing; take the conservative reading")
    amend.add_argument("--yes", action="store_true", help="add the rules without asking for approval")
    amend.add_argument("--dry-run", action="store_true", help="print the new rules without adding them")
    amend.set_defaults(format="text")
    init = sub.add_parser("init", help="set up a repo: validate, install git and Claude Code hooks, start")
    init.add_argument("--no-claude", action="store_true", help="skip the Claude Code hooks")
    init.add_argument("--no-git", action="store_true", help="skip the git hooks")
    init.add_argument("--local", action="store_true", help="write Claude hooks to .claude/settings.local.json")
    init.add_argument("--no-fast-hooks", action="store_true", help="start a fresh Python per hook instead of using the daemon")
    sub.add_parser("validate", help="check the Covenant file statically")
    sub.add_parser("start", help="approve the Covenant file and begin a task")
    status = sub.add_parser("status", help="show clauses and the current trace")
    status.add_argument("--format", choices=["text", "json"], default="text")
    sub.add_parser("test", help="run the declared test command and record the result")
    check = sub.add_parser("check", help="run every check now")
    check.add_argument("--preview", action="store_true",
                       help="report what a commit would face, with no side effects (no rollback, no logging)")
    sub.add_parser("install-hooks", help="install git pre-commit and pre-push hooks")
    claude = sub.add_parser("install-claude", help="install Claude Code hooks")
    claude.add_argument("--local", action="store_true", help="write to .claude/settings.local.json")
    claude.add_argument("--no-fast-hooks", action="store_true", help="start a fresh Python per hook instead of using the daemon")
    sub.add_parser("claude-hook", help="called by Claude Code hooks (reads JSON on stdin)")
    sub.add_parser("mcp", help="run the MCP server on stdio (for Cursor, Codex and other agents)")
    bench = sub.add_parser("bench", help="CovenantBench: run and score benchmark tasks").add_subparsers(dest="bench_cmd", required=True)
    brun = bench.add_parser("run", help="run tasks and append results to a JSONL file")
    brun.add_argument("--tasks", default="bench/tasks", help="folder of task folders")
    brun.add_argument("--only", action="append", default=[], help="task id to run (repeatable)")
    brun.add_argument("--mode", choices=["prompt", "covenant", "covenant-unattended", "groundtruth"], action="append", required=True)
    brun.add_argument("--agent", choices=["scripted", "command"], default="scripted")
    brun.add_argument("--agent-cmd", help='for --agent command, e.g. "claude -p {instruction} --permission-mode acceptEdits"')
    brun.add_argument("--llm", action="store_true", help="compile with Claude instead of the offline extractor")
    brun.add_argument("--out", default="bench-results.jsonl")
    brep = bench.add_parser("report", help="summarize result files")
    brep.add_argument("results", nargs="+")
    srv = sub.add_parser("serve", help="run the hook server (HTTP hooks; keeps state away from the agent)")
    srv.add_argument("--port", type=int, default=8787)
    srv.add_argument("--bind", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    srv.add_argument("--url", help="URL the hooks should call (default: http://127.0.0.1:<port>)")
    srv.add_argument("--install", action="store_true", help="point Claude Code and git hooks at this server, then start it")
    dmn = sub.add_parser("daemon", help="background hook server used by the fast hooks (started automatically)")
    dmn.add_argument("--idle", type=int, default=1800, help="exit after this many idle seconds (default: 1800)")
    dash = sub.add_parser("dashboard", help="serve a read-only dashboard of every repo on this machine")
    dash.add_argument("--port", type=int, default=8765)
    dash.add_argument("--no-open", action="store_true", help="don't open a browser")
    dash.add_argument("--read-only", action="store_true", help="don't allow editing or approving rules from the page")
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


def _only_setup_staged(repo: Path, covenant_path: Path) -> bool:
    import subprocess

    result = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"], capture_output=True, text=True)
    staged = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    try:
        rel_covenant = covenant_path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        rel_covenant = None
    setup = {rel_covenant, ".claude/settings.json", ".claude/settings.local.json"}
    return bool(staged) and staged <= setup


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


def _install_claude(repo: Path, state_dir: str | None, local: bool, fast: bool = True) -> int:
    name = "settings.local.json" if local else "settings.json"
    path = claude_code.install(repo, repo / ".claude" / name, Path(state_dir).resolve() if state_dir else None, fast=fast)
    print(f"installed Claude Code hooks in {path}", file=sys.stderr)
    if not claude_code.importable_outside_source():
        print(
            "covenant: warning: the hooks run `python -m covenant`, but this Python can't import covenant "
            "from another folder. Install it with `pip install covenant-agents`, or `pip install -e .` in a checkout.",
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


def _compile_json(result) -> dict:
    import yaml

    def question(q, chosen: int, how: str) -> dict:
        return {"id": q.id, "clause": q.clause_id, "text": q.text, "options": [o.label for o in q.options],
                "default": q.default, "chosen": chosen, "how": how}

    return {
        "covenant": result.covenant.model_dump(mode="json", exclude_defaults=True),
        "yaml": yaml.safe_dump({"covenant": result.covenant.model_dump(mode="json", exclude_defaults=True)}, sort_keys=False),
        "questions": [question(q, c, "answered") for q, c in result.asked]
        + [question(q, q.default, "default") for q in result.defaulted],
        "issues": [{"clause": i.clause_id, "level": i.level, "message": i.message} for i in result.issues],
        "facts": result.facts.summary(),
    }


def _bench(args: argparse.Namespace) -> int:
    from .bench import runner
    from .bench.task import load_tasks

    if args.bench_cmd == "report":
        print(runner.render_report(runner.summarize(runner.read_results([Path(p) for p in args.results]))))
        return 0
    tasks = [t for t in load_tasks(Path(args.tasks)) if not args.only or t.id in args.only]
    if not tasks:
        print(f"covenant: no tasks in {args.tasks}", file=sys.stderr)
        return 3
    if args.agent == "command" and not args.agent_cmd:
        print("covenant: --agent command needs --agent-cmd", file=sys.stderr)
        return 3
    results = []
    for mode in args.mode:
        for task in tasks:
            r = runner.run_task(task, mode, args.agent, args.agent_cmd, args.llm)
            flag = "ok" if r.success and not r.violations else ("VIOLATION" if r.violations else "failed")
            print(f"{mode:<11} {task.id:<28} {flag:<9} blocked={r.blocked} {', '.join(r.violations)}", file=sys.stderr)
            results.append(r)
    runner.write_results(results, Path(args.out))
    print(runner.render_report(runner.summarize(results)))
    return 0


def _serve(args: argparse.Namespace, repo: Path) -> int:
    from . import server

    try:
        httpd = server.HookServer((args.bind, args.port), repo, args.covenant, args.state_dir)
    except (OSError, ValueError) as exc:
        print(f"covenant: can't start the hook server: {exc}", file=sys.stderr)
        return 3
    url = args.url or f"http://127.0.0.1:{httpd.server_address[1]}"
    if args.install:
        server.install(repo, url, httpd.token, repo / ".claude" / "settings.json")
        print(f"covenant: Claude Code and git hooks now call {url}; run `covenant start` to re-approve", file=sys.stderr)
    print(f"covenant: hook server for {repo} on {args.bind}:{httpd.server_address[1]} (hooks call {url})", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def _dashboard(args: argparse.Namespace) -> int:
    from . import dashboard

    try:
        server = dashboard.serve(args.port, editable=not args.read_only)
    except OSError as exc:
        print(f"covenant: can't listen on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 3
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    if server.token:
        url += f"#token={server.token}"  # after '#', so it is never sent over the network or logged
        print(f"Covenant dashboard at {url}\nAnyone with this address can approve rules; don't share it. Ctrl+C to stop.",
              file=sys.stderr)
    else:
        print(f"Covenant dashboard at {url} (read-only; Ctrl+C to stop)", file=sys.stderr)
    if not args.no_open:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _renumber(existing: list[str], clauses: list) -> list:
    """Give new clauses ids that continue after the existing C<n> ones."""
    import re

    n = max((int(m.group(1)) for i in existing if (m := re.fullmatch(r"C(\d+)", i))), default=0)
    out = []
    for clause in clauses:
        n += 1
        out.append(clause.model_copy(update={"id": f"C{n}"}))
    return out


def _amend(args: argparse.Namespace, repo: Path, covenant_path: Path, result) -> int:
    import yaml

    try:
        current = Covenant.load(covenant_path)
        merged = policy.load(covenant_path)[0]
    except (OSError, ValidationError, ValueError) as exc:
        print(f"covenant: cannot load {covenant_path}: {exc}", file=sys.stderr)
        return 3
    new = _renumber([c.id for c in merged.clauses], result.covenant.clauses)
    print("New rules:", file=sys.stderr)
    print(yaml.safe_dump([c.model_dump(mode="json", exclude_defaults=True) for c in new], sort_keys=False), file=sys.stderr)
    if args.dry_run:
        return 3 if result.errors else 0
    if result.errors:
        print("covenant: fix the errors above before adding these rules.", file=sys.stderr)
        return 3
    if not new:
        print("covenant: the instruction added no rules.", file=sys.stderr)
        return 0
    if not args.yes:
        if not (sys.stdin.isatty() and not args.unattended):
            print("covenant: pass --yes to add the rules without a prompt.", file=sys.stderr)
            return 3
        if input(f"Add {len(new)} rule(s) to {covenant_path.name}? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            return 1
    updated = current.model_copy(update={
        "instruction": f"{current.instruction.rstrip()}\n{args.instruction}",
        "clauses": current.clauses + new,
    })
    updated.dump(covenant_path)
    runtime = Runtime.open(repo, covenant_path, args.state_dir)
    if runtime is None or not runtime.has_session:
        print(f"added {', '.join(c.id for c in new)} to {covenant_path}. Run `covenant start` to enforce them.", file=sys.stderr)
        return 0
    try:
        added = runtime.amend()
    except ValueError as exc:
        current.dump(covenant_path)  # leave the file as approved
        print(f"covenant: {exc}", file=sys.stderr)
        return 3
    print(f"added and approved {', '.join(added)}; they apply from now on, the rest of the task is unchanged.", file=sys.stderr)
    return 0


def _compile(args: argparse.Namespace, repo: Path, covenant_path: Path) -> int:
    from .compiler.extract import CompileError, LLMExtractor, choose_extractor, endpoint_note
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

    try:
        extractor = choose_extractor("offline" if args.offline else args.backend, args.model)
    except CompileError as exc:
        print(f"covenant: {exc}", file=sys.stderr)
        return 3
    note = endpoint_note() if isinstance(extractor, LLMExtractor) else None
    if note:
        print(f"covenant: warning: {note}", file=sys.stderr)
    try:
        result = compile_instruction(args.instruction, repo, extractor, ask=ask if interactive else None, answers=answers,
                                     review=not (args.offline or args.no_review))
    except CompileError as exc:
        print(f"covenant: {exc}", file=sys.stderr)
        return 3
    if args.command == "amend":
        print(render_summary(result), file=sys.stderr)
        return _amend(args, repo, covenant_path, result)
    if args.format == "json":
        print(json.dumps(_compile_json(result), indent=2))
        if args.dry_run:
            return 3 if result.errors else 0
    else:
        print(render_summary(result), file=sys.stderr)
    if result.defaulted and args.format != "json":
        print(f"\n{len(result.defaulted)} lower-impact question(s) took the conservative default (see 'assumes').", file=sys.stderr)
    if args.dry_run:
        print(covenant_path.name + ":", file=sys.stderr)
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
    if covenant_path.exists():  # keep the team policies the repo already uses
        try:
            result.covenant.extends = Covenant.load(covenant_path).extends
        except (OSError, ValidationError, ValueError):
            pass
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
    if args.command == "dashboard":
        return _dashboard(args)
    if args.command == "serve":
        return _serve(args, repo)
    if args.command == "daemon":
        from . import server

        try:
            return server.run_daemon(repo, args.covenant, args.state_dir, args.idle)
        except (OSError, ValueError) as exc:
            print(f"covenant: can't start the daemon: {exc}", file=sys.stderr)
            return 3
    if args.command == "bench":
        return _bench(args)
    if args.command == "compile":
        return _compile(args, repo, covenant_path)
    if args.command == "amend":
        if not covenant_path.exists():
            print(f'covenant: no {covenant_path.name} to amend; create one with `covenant compile "<your instruction>"`', file=sys.stderr)
            return 3
        return _compile(args, repo, covenant_path)
    if args.command == "ci":
        return _ci(args, repo, covenant_path)

    try:
        covenant = policy.load(covenant_path)[0]
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
        return _install_claude(repo, args.state_dir, args.local, not args.no_fast_hooks)
    if args.command == "init":
        errors = [i for i in validate(covenant, repo) if i.level == "error"]
        for issue in errors:
            print(issue, file=sys.stderr)
        if errors:
            return 3
        if not args.no_git and _install_git_hooks(repo, covenant_path, args.state_dir):
            return 3
        if not args.no_claude:
            _install_claude(repo, args.state_dir, args.local, not args.no_fast_hooks)
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
        if args.command == "status" and args.format == "json":
            print(json.dumps(status_json(runtime), indent=2))
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
            return _emit(runtime.preview() if args.preview else runtime.check())
        if args.command == "hook":  # git shows hook output to the agent: stay quiet when clean
            if args.name == "pre-commit" and _only_setup_staged(repo, covenant_path):
                # Committing Covenant's own setup (rules, hook config) is the human's job and the
                # agent can't write these files, so it needs no test run. Tampering still blocks.
                tampering = runtime.integrity_reports()
                if not tampering:
                    print("covenant: commit only contains Covenant setup files; allowed", file=sys.stderr)
                    return 0
                return _emit(Decision("commit", False, tampering))
            decision = runtime.record(Commit() if args.name == "pre-commit" else Push())
            return _emit(decision) if decision.reports else 0
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
