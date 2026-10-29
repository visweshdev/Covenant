# Covenant

Covenant turns the rules you give a coding agent ("don't change public APIs, don't delete
user data, run tests before committing") into checks that actually run, and enforces them
while the agent works, in git, and in CI.

It checks **outcomes**, not tool calls: the API surface after the edits, what happened to
protected files however they were touched, and the order of events (tests passed after the
last edit, before the commit).

```bash
pip install -e .                       # from this checkout
cd your-repo
covenant compile "Update dependencies, but don't change public APIs, don't delete user data, and run tests before committing."
covenant init                          # git hooks + Claude Code hooks, then start enforcing
```

## How it works

1. **Compile** (`covenant compile`): Claude, or the offline extractor with `--offline`, splits the
   instruction into a goal and clauses. Deterministic passes then:
   - ground each clause in the repo: the real exports, data folders, tables and test command;
   - bind it to a checker;
   - ask at most three questions about specific things in the repo;
   - check the result statically.

   You approve the resulting `covenant.yaml`.
2. **Enforce locally:** hooks turn what the agent does into events. Checkers judge them, and a
   violation goes back to the agent as a report naming the rule, the evidence and a fix.
3. **Enforce in CI** (`covenant ci` / the GitHub Action): every pull request is judged against
   the Covenant from its base commit.

## Checkers

| Rule kind | Checker | What it checks |
|---|---|---|
| Semantic invariant | `api_surface_diff` | Python exported names and signatures vs. the base commit (`ast`) |
| Semantic invariant | `ts_api_surface_diff` | TypeScript exported types, via the project's own compiler |
| Semantic invariant | `schema_guard` | No dropped tables or columns, truncation, unqualified deletes or type changes in new migrations |
| Semantic invariant | `dependency_diff` | Only allowed packages change; locked versions keep their major version |
| Effect prohibition | `effect_monitor` | Protected files and tables aren't deleted, truncated or overwritten, whatever does it; rolled back from a snapshot |
| Temporal obligation | `trace_monitor` | Ordering rules in past-time LTL, e.g. `G(commit -> (!edit S test_pass))` |

A rule with no checker ("keep the code readable") is kept and marked `unenforceable`,
never silently dropped.

## Agents

**Claude Code:** `covenant init` (or `covenant install-claude`) adds hooks to `.claude/settings.json`:

| Hook | What Covenant does |
|---|---|
| `SessionStart` | Starts a session and tells Claude the rules |
| `PreToolUse` | Denies commits and pushes that would break a rule, edits to protected files, changes to Covenant's own files, and hook bypasses (`--no-verify`, `core.hooksPath`) |
| `PostToolUse` / `PostToolUseFailure` | Records edits, commands and test runs, and reports side effects found by the snapshot scan |
| `Stop` | Claude can't finish while a rule is broken; after repeated blocks it hands over to you |

The hook fails closed: if it crashes before a tool call, the call is denied.

**Cursor, Codex and other MCP clients:** `covenant --repo <path> mcp` is an MCP server with
`covenant_rules`, `covenant_run_tests`, `covenant_record_edit`, `covenant_preflight` and
`covenant_check`. The git hooks enforce commits and pushes for every agent.

```json
// .cursor/mcp.json
{ "mcpServers": { "covenant": { "command": "covenant", "args": ["--repo", ".", "mcp"] } } }
```

```toml
# ~/.codex/config.toml
[mcp_servers.covenant]
command = "covenant"
args = ["--repo", "/path/to/repo", "mcp"]
```

## CI

```yaml
# .github/workflows/covenant.yml
on: pull_request
jobs:
  covenant:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: <owner>/covenant@main      # this repository
```

How the CI check judges a pull request:
- The rules come from the **base** commit, so a PR can't weaken them; a PR that changes `covenant.yaml` is flagged for human review.
- API and migration checkers compare against the base.
- Protected files are checked against the diff.
- Test commands run on the PR's final state.

## Commands

| Command | What it does |
|---|---|
| `covenant compile "<instruction>"` | Writes `covenant.yaml`. Flags: `--offline`, `--unattended`, `--answer Q1=2`, `--dry-run`, `--yes` |
| `covenant init` | Validates, installs git and Claude Code hooks, starts a session |
| `covenant start` | Approves the current `covenant.yaml` and begins a task |
| `covenant test` / `check` / `status` | Runs the declared tests / every check / shows the state |
| `covenant ci` | Checks a pull request (`--base`, `--format github\|json\|text`, `--no-tests`) |
| `covenant mcp` | Runs the MCP server |

Exit codes: 0 clean, 1 violations (the agent should revise), 2 needs a human, 3 bad setup.

## Safety model

- State lives outside the repo, in `~/.covenant/workspaces/<repo>-<hash>` (set `COVENANT_HOME` to move it). A file lock keeps parallel hooks from corrupting it.
- The approved Covenant is pinned when a session starts. If `covenant.yaml`, the git hooks or the Claude settings change, it's reported as an `INTEGRITY` violation, and so is `disableAllHooks` or `core.hooksPath`.
- A verifier that can't run blocks rather than passes.

**Limits.** Covenant runs as your user. An agent with a shell could still edit its state folder
directly. That gets detected, but it isn't prevented: full isolation needs the agent in a sandbox
where that folder isn't writable. Effects are found by comparing hashes on every event, not by
kernel events, and database effects are only seen through migrations, the diff, or an explicit
`covenant record effect`.

## Development

```bash
pip install -e ".[dev]"
npm install --prefix /tmp/ts typescript@5.6.3   # for the TypeScript checker tests
COVENANT_TYPESCRIPT=/tmp/ts/node_modules/typescript python -m pytest -q
```

Layout: `src/covenant/` holds the rule format (`ir.py`), `runtime.py`, `monitors/`, `verifiers/`,
`compiler/`, `integrations/` (Claude Code and MCP), `ci.py` and `cli.py`. The worked example
from the proposal is in `examples/mylib`.

## Not built yet

- **Sandbox:** a container with copy-on-write storage, so the agent can't reach Covenant's state.
  Today the snapshot copies files aside and detects changes by hash.
- **Database proxy:** to see and roll back live SQL.
- **API checkers for Go, Java and Rust.**
- **VS Code extension, web dashboard and team-wide policies.**
- **CovenantBench:** the evaluation suite from the proposal.
