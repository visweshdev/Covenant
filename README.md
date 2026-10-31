<p align="center">
  <img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/logo.png" width="112" alt="Covenant logo: a C closed by a short bar">
</p>

<h1 align="center">Covenant</h1>

<p align="center">
  <b>Rules for coding agents that actually hold.</b><br>
  Write them in plain English. Covenant turns them into checks and enforces them<br>
  while the agent works, in git, and in CI.
</p>

<p align="center">
  <a href="https://pypi.org/project/covenant-agents/"><img src="https://img.shields.io/pypi/v/covenant-agents?color=1f6feb&label=PyPI" alt="PyPI version"></a>
  <a href="https://pypi.org/project/covenant-agents/"><img src="https://img.shields.io/pypi/pyversions/covenant-agents?color=1f6feb" alt="Python versions"></a>
  <a href="https://github.com/visweshdev/Covenant/actions/workflows/ci.yml"><img src="https://github.com/visweshdev/Covenant/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/visweshdev/Covenant/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-1f6feb" alt="MIT license"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#what-it-can-enforce">What it enforces</a> ·
  <a href="https://github.com/visweshdev/Covenant/blob/main/docs/README.md">Docs</a> ·
  <a href="https://github.com/visweshdev/Covenant/blob/main/CHANGELOG.md">Changelog</a>
</p>

<br>

You tell an agent *"don't change public APIs, don't delete user data, run tests before
committing"*. It usually listens. Covenant makes sure it does.

It checks **outcomes, not tool calls**: the API surface after the edits, what happened to
protected files however they were touched, and the order of events (tests passed *after* the
last edit, *before* the commit).

## Quick start

```bash
pipx install covenant-agents        # or: pip install covenant-agents
```

```bash
cd your-repo
covenant compile "Don't change public APIs, don't delete user data, run tests before committing."
covenant init        # installs git + Claude Code hooks and starts enforcing
```

That's it. Works on Windows, macOS and Linux with Python 3.11+.

> 💡 **Tip:** start from a ready-made rule set with `covenant compile --pack hotfix "fix the login crash"`
> (`covenant packs` lists them), or let `covenant suggest` propose rules from what went wrong in
> your repo's own history.

## What it looks like

The agent tries something a rule forbids. Covenant refuses it before it runs, and the agent
gets the rule, the evidence and a fix:

```text
Covenant refused this command:
- C2 (never force push). Fix: Push normally, or ask the human; a force push rewrites shared history
```

You can ask what happened at any time:

```text
$ covenant explain
At 01:23 on 01 Nov, the agent tried to use a tool. Covenant blocked it.

Rule C3: "never run destructive SQL"
  Why: The command counts as SQL that drops a table, database or schema: psql -c "DROP TABLE users".
  What the agent can do: Don't drop, truncate or wipe tables; change rows with a WHERE, or ask the human
```

## How it works

```text
  "don't change public APIs,          ┌──────────────┐         ┌───────────────────────────┐
   run tests before committing"  ──▶  │   compile    │  ──▶    │  covenant.yaml            │
                                      │ Claude or    │         │  typed rules you approve  │
                                      │ offline      │         └─────────────┬─────────────┘
                                      └──────────────┘                       │
                    ┌───────────────────────────┬────────────────────────────┤
                    ▼                           ▼                            ▼
          while the agent works            at commit / push             on every PR
          Claude Code hooks, MCP           git hooks                    covenant ci, GitHub Action
```

1. **Compile.** Claude (or the offline extractor) splits your instruction into rules, grounds each
   one in the repo (the real exports, data folders, tables and test command), binds it to a
   checker, and asks at most three questions. You approve the result.
2. **Enforce locally.** Hooks turn what the agent does into events. A violation goes back to the
   agent as a report naming the rule, the evidence and a fix.
3. **Enforce in CI.** Every pull request is judged against the rules from its **base** commit, so
   a PR can't weaken them.

Rules no checker covers ("keep the code readable") are reviewed by Claude at each commit as
warnings. Nothing is ever silently dropped.

## What it can enforce

| Say | Covenant checks |
|---|---|
| "don't change public APIs" | Exported names and signatures in Python, TypeScript, Go, Rust and Java |
| "don't delete user data", "don't touch config/" | Protected files and SQLite rows, put back from a snapshot if lost |
| "no destructive migrations" | No dropped tables or columns, truncation or type changes |
| "only update requests", "no new dependencies" | Only allowed packages change, majors stay put |
| "only change files in src/" | The change stays inside the folders you name |
| "don't commit secrets", "don't skip tests" | Keys, tokens and `.env` files; deleted, skipped or `.only` tests |
| "keep the change under 200 lines" | Files and lines changed |
| "no debug prints", "don't silence type errors" | Leftover breakpoints and logs; new `noqa`, `@ts-ignore` and friends |
| "run tests before committing", "push at most once" | The order and count of the agent's steps |
| "never force push", "don't deploy", "never run destructive SQL" | Refused before they run (16 categories) |
| "only reach pypi.org and github.com" | A network allowlist for commands, git remotes, installs and fetches |
| "only the payments agent may edit billing/" | Folder ownership between agents |
| "stop after 20 minutes", "spend at most $2" | Time, action and cost budgets |

**[Every checker, phrase and rule pack →](https://github.com/visweshdev/Covenant/blob/main/docs/rules.md)**

## Works with

| | |
|---|---|
| **Claude Code** | Hooks on every tool call: refuses bad actions, records test runs, won't let Claude finish while a rule is broken. About 0.2 s per action. |
| **Cursor, Codex and other MCP clients** | An MCP server with the rules, a preflight check and a test runner; git hooks enforce commits and pushes. |
| **Git** | Pre-commit and pre-push hooks for any agent, or a human. |
| **GitHub** | An Action that comments on the exact lines that break a rule. |
| **VS Code** | A rules panel, violations in Problems, and a 🔒 badge on protected files. |
| **Docker** | `covenant sandbox` runs the agent behind an allowlist proxy, with Covenant's state out of reach. |

**[Set up each agent →](https://github.com/visweshdev/Covenant/blob/main/docs/agents.md)** ·
**[CI and the GitHub Action →](https://github.com/visweshdev/Covenant/blob/main/docs/ci.md)**

## Also included

- **Undo:** restore points before risky steps; `covenant undo` puts the working tree back.
- **Explain:** `covenant explain` says what was blocked, why, and what you and the agent can do.
- **Alerts:** Slack, a webhook or email when an agent needs a human.
- **Dashboard:** every repo's rules, a timeline of blocks, a rule editor, and a step-by-step
  replay of any task.
- **Team policies:** shared rules from a file or an https address, optionally pinned by sha256.

**[Dashboard, undo, alerts and policies →](https://github.com/visweshdev/Covenant/blob/main/docs/tools.md)**

## Tried on real repos

Claude Code ran live under Covenant on [TinyDB](https://github.com/msiemens/tinydb),
[itsdangerous](https://github.com/pallets/itsdangerous),
[humanize](https://github.com/python-humanize/humanize) and
[prettytable](https://github.com/prettytable/prettytable), each with an honest task and a trap
task. **Every honest task committed, and every trap was refused or blocked.** On
CovenantBench, Covenant took violations from 1.17 per task to zero with no loss in task success.

**[Benchmark and trial notes →](https://github.com/visweshdev/Covenant/blob/main/bench/README.md)**

## Documentation

| Page | What's in it |
|---|---|
| [Rules](https://github.com/visweshdev/Covenant/blob/main/docs/rules.md) | Checkers, what the compiler understands, refused commands, rule packs, step rules |
| [Agents](https://github.com/visweshdev/Covenant/blob/main/docs/agents.md) | Claude Code, MCP clients, the hook server, the sandbox, VS Code |
| [CI](https://github.com/visweshdev/Covenant/blob/main/docs/ci.md) | The GitHub Action and `covenant ci` |
| [Tools](https://github.com/visweshdev/Covenant/blob/main/docs/tools.md) | Dashboard, undo, explain, alerts, suggest, team policies |
| [Commands](https://github.com/visweshdev/Covenant/blob/main/docs/commands.md) | Every command, flag, exit code and environment variable |
| [Safety model](https://github.com/visweshdev/Covenant/blob/main/docs/safety.md) | What Covenant protects against, and what it doesn't |

## Contributing

Bug reports, new checkers and benchmark tasks are welcome. See
[CONTRIBUTING.md](https://github.com/visweshdev/Covenant/blob/main/CONTRIBUTING.md), and
[SECURITY.md](https://github.com/visweshdev/Covenant/blob/main/SECURITY.md) to report a way
around Covenant privately.

<p align="center"><sub>Released under the <a href="https://github.com/visweshdev/Covenant/blob/main/LICENSE">MIT License</a>.</sub></p>
