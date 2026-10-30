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
- **Real-repo trial:** run on TinyDB. Fixed how test commands are found and recognized
  (see `bench/README.md`).
- **Benchmark:** CovenantBench, with 6 synthetic tasks, independent oracles and a scripted agent.
