<p align="center"><img src="docs/logo.png" width="72" alt="Covenant logo"></p>

<h1 align="center">Changelog</h1>

<p align="center">Install the latest with <code>pipx install covenant-agents</code> · <a href="https://github.com/visweshdev/Covenant/releases">All releases</a></p>

---

## Unreleased

- Redesigned README, with the reference split into [docs/](docs/README.md) pages.
- Installers install from PyPI by default (`--github` / `-GitHub` for the main branch).

## 0.1.1 (2026-10-31)

First published release; 0.1.0 was tagged but never published, because its tests failed on
GitHub's Linux and Windows machines.

- Restore points work where no git name or email is set (CI, containers).
- The benchmark runner finds Git's bash on Windows instead of the WSL launcher.
- Failing tests show up as annotations on the GitHub Actions page.
- The logo appears on PyPI, the VS Code Marketplace page, PR comments and Slack alerts.

## 0.1.0 (2026-10-31)

First release.

- **Compiler:** turns a plain-English instruction into typed rules, using Claude or an
  offline extractor. It grounds each rule in the repo, asks at most three questions, and
  keeps rules it can't check as `unenforceable` instead of dropping them. `covenant amend`
  adds rules mid-task.
- **Checkers:**
  - public APIs: Python, TypeScript, Go, Rust and Java;
  - destructive migrations;
  - dependency scope;
  - SQLite rows, tables and columns;
  - protected files, with rollback;
  - ordering, limit and deadline rules over the agent's steps.
- **Enforcement:**
  - Claude Code hooks;
  - git hooks;
  - an MCP server for other agents;
  - a local hook server;
  - CI with `covenant ci` and a GitHub Action.
- **Tools:** team policies (`extends`), a local dashboard and a VS Code extension.
- **More rules:** only-these-files, secret scanning, keep-tests, change-size budgets, no debug
  code, no warning suppressions, no large files, no new dependencies, and 15 kinds of refused
  shell commands (force push, deploy, publish, installing packages, network access and more).
- **Claude review:** rules no checker covers are reviewed by Claude at each commit, as warnings.
- **Compile with a Claude subscription:** `covenant compile` uses your Claude Code login when no
  API key is set.
- **Fast hooks:** about 0.2 s per agent action instead of about 1 s, through a background daemon.
- **Dashboard editing:** draft rules from plain English, check them, and approve them from the page.
- **Real-repo trial:** run on TinyDB. Fixed how test commands are found and recognized
  (see `bench/README.md`).
- **Rule packs:** `covenant compile --pack safe-refactor|dependency-bump|docs-only|hotfix|data-safe|secure|lockdown`.
- **Destructive SQL guard:** refuses DROP, TRUNCATE and unqualified DELETE/UPDATE for Postgres,
  MySQL, SQLite and others, in commands, `.sql` files and database MCP tools.
- **Network allowlist, path ownership and budgets:** "only reach pypi.org", "only the payments
  agent may edit billing/", "stop after 20 minutes", "spend at most $2", "at most 50 tool calls".
- **Undo snapshots:** restore points before risky steps; `covenant snapshots` and `covenant undo`.
- **Team policies from a URL**, optionally pinned by sha256, with an offline copy.
- **Alerts** to Slack, a webhook or email when an agent needs a human.
- **`covenant explain`** and **`covenant suggest`** (rules learned from the repo's history).
- **GitHub PR comments** on the exact broken lines plus one summary comment, from the action.
- **Dashboard replay** of the current and past tasks, step by step.
- **Sandbox mode** (`covenant sandbox`): Docker containers with an allowlist proxy and the
  task's state out of the agent's reach.
- **Installers and a release workflow** for PyPI and the VS Code Marketplace.
- **Live trials on itsdangerous, humanize and prettytable:** each had an honest task and a trap
  task. All three honest tasks committed, and all three traps were refused or blocked.
  - Fixed false API alarms caused by git-ignored `_version.py` files.
  - Fixed test-command picks of CI placeholders, benchmarks and docs/type-check runs.
- **Benchmark:** CovenantBench, with 6 synthetic tasks, independent oracles and a scripted agent.
