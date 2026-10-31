# Changelog

## 0.1.0 (unreleased)

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
- **Live trials on itsdangerous, humanize and prettytable:** each had an honest task and a trap
  task. All three honest tasks committed, and all three traps were refused or blocked.
  - Fixed false API alarms caused by git-ignored `_version.py` files.
  - Fixed test-command picks of CI placeholders, benchmarks and docs/type-check runs.
- **Benchmark:** CovenantBench, with 6 synthetic tasks, independent oracles and a scripted agent.
