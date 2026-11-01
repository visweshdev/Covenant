<p align="center"><img src="docs/logo.png" width="72" alt="Covenant logo"></p>

<h1 align="center">Changelog</h1>

<p align="center">Install the latest with <code>pipx install covenant-agents</code> · <a href="https://github.com/visweshdev/Covenant/releases">All releases</a></p>

---

## Unreleased

- **Replayed on 50 real repositories** (`covenant bench replay`): the last 30 commits of 50
  popular Python, TypeScript, Go, Rust and Java projects (1,446 commits), judged by the CI check.
  Flagged commits fell from 141 to 75, of which 72 are real rule breaks; every TypeScript repo now
  gets an API check (4 of 10 before). What it found and fixed (see
  [bench/README.md](bench/README.md#replay-on-50-real-repositories)):
  - checks run about 3× faster: Python API extraction reads each file once per commit instead of
    once per exported name, and added lines come from one `git diff` instead of a Python diff;
  - test commands: install steps (`pip install tox`), shell plumbing (`sed`, `xargs`) and
    `pytest` in Rust repos are no longer picked;
  - TypeScript entry points are found from `package.json` (`types`, `exports`, `main`) mapped
    back to source through `tsconfig` `outDir`/`rootDir`, or hand-written `.d.ts` files;
  - API changes that every caller survives aren't reported: a parameter type that widens
    (`A` → `A | None`), a return type that narrows, a new base class, `ClassVar`, `@override`,
    import aliases of the same type, Go `interface{}` → `any` and new variadic parameters, Rust
    bounds that loosen and `#[doc(hidden)]` items, TypeScript `_private` members;
  - Go signatures with `interface{}` were cut short at its brace, hiding later parameters;
  - very long TypeScript types (cut by the compiler near a million characters) are compared by
    their declaration, so they no longer differ between runs;
  - module lookups match file names case-exactly (on Windows a class looked like a module);
  - "don't silence type errors" checks type-checker suppressions only, not lint ones;
    suppressions that were edited or moved, quoted in docs, in test fixtures, or used as test
    assertions (`@ts-expect-error`, `# type: ignore[code]` in tests) aren't new ones;
  - debug-code checks skip comments, doc examples, scripts and CLIs; a `skipif` on a brand-new
    test isn't a skipped test; compiled requirements (pip-compile, uv) are lockfiles;
  - secret scanning ignores values that are plain lowercase words.
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
