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

   You approve the resulting `covenant.yaml`. To add a rule mid-task (`covenant amend "also don't
   touch config/"`), the new rules apply from then on and the task's history is kept.
2. **Enforce locally:** hooks turn what the agent does into events. Checkers judge them, and a
   violation goes back to the agent as a report naming the rule, the evidence and a fix.
3. **Enforce in CI** (`covenant ci` / the GitHub Action): every pull request is judged against
   the Covenant from its base commit.

## Checkers

| Rule kind | Checker | What it checks |
|---|---|---|
| Semantic invariant | `api_surface_diff` | Python exported names and signatures vs. the base commit (`ast`) |
| Semantic invariant | `ts_api_surface_diff` | TypeScript exported types, via the project's own compiler |
| Semantic invariant | `source_api_diff` | Go, Rust and Java public API, read from source (no compiler needed) |
| Semantic invariant | `schema_guard` | No dropped tables or columns, truncation, unqualified deletes or type changes in new migrations |
| Semantic invariant | `dependency_diff` | Only allowed packages change; locked versions keep their major version |
| Semantic invariant | `sqlite_guard` | Rows, tables and columns in SQLite databases are never lost; inserts are allowed. Checked after every step, and lost rows are put back from a snapshot |
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

## Team policies

A repo's `covenant.yaml` can list `extends: [policies/team.yaml]`. Policy files hold clauses
in the same format and can extend each other. `COVENANT_POLICY` (paths separated by your
OS's path separator) applies policies to every repo on a machine.

Policy rules:
- are merged in first, with an `origin` naming their policy;
- can't be redefined by a repo: an id clash is an error;
- are protected from the agent like Covenant's own files;
- in CI, come from the base commit, and a PR that changes them is flagged.

## Dashboard

`covenant dashboard` opens a read-only page on `127.0.0.1:8765` covering every repo Covenant enforces
on this machine:
- each repo's rules, with how close each is to handing over to a human;
- a timeline of blocks and reports;
- recent events and protected paths.

It refreshes every few seconds, answers GET only, and rejects requests whose Host header isn't localhost.

## Hook server

`covenant serve --install` runs Covenant as a long-lived local server.
- **Claude Code** uses HTTP hooks to reach it at `/hook`.
- **Git** hooks call it with `curl`, and refuse the commit if it's unreachable.

State and snapshots then live with the server instead of wherever the agent runs, which is
the building block for running agents in a container. Requests need a bearer token created at
start; that keeps other local processes out, but not an agent that can read the hook settings.

## VS Code

`vscode-extension/` adds a Covenant panel, the current violations in Problems, a 🔒 badge on
protected files, and commands to compile rules, approve them and run tests. It updates live as
an agent's hooks record events. Its checks never change files. Build it with `npm run package`
and install the `.vsix` (see `vscode-extension/README.md`).

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
| `covenant amend "<instruction>"` | Adds rules mid-task, keeping the history; only additions are accepted. Same flags as `compile` |
| `covenant test` / `check` / `status` | Runs the declared tests / every check / shows the state |
| `covenant ci` | Checks a pull request (`--base`, `--format github\|json\|text`, `--no-tests`) |
| `covenant mcp` | Runs the MCP server |
| `covenant serve` | Runs the hook server (`--install` points Claude Code and git at it) |
| `covenant dashboard` | Opens the local dashboard |
| `covenant bench run` / `report` | Runs and scores CovenantBench (see `bench/README.md`) |

Exit codes: 0 clean, 1 violations (the agent should revise), 2 needs a human, 3 bad setup.

Compiling with Claude reads `ANTHROPIC_API_KEY`. If `ANTHROPIC_BASE_URL` points somewhere other
than Anthropic (agent CLIs and proxies often export it), `compile` warns you; set
`COVENANT_ANTHROPIC_BASE_URL=https://api.anthropic.com` to override it for Covenant only.

## Safety model

- State lives outside the repo, in `~/.covenant/workspaces/<repo>-<hash>` (set `COVENANT_HOME` to move it). A file lock keeps parallel hooks from corrupting it.
- The approved Covenant is pinned when a session starts. If `covenant.yaml`, the git hooks or the Claude settings change, it's reported as an `INTEGRITY` violation, and so is `disableAllHooks` or `core.hooksPath`.
- A verifier that can't run blocks rather than passes.

**Limits.** Covenant runs as your user. An agent with a shell could still edit its state folder
directly. That gets detected, but it isn't prevented: full isolation needs the agent in a sandbox
where that folder isn't writable. Effects are found by comparing hashes on every event, not by
kernel events. SQLite databases are compared row by row; other databases are only seen through
migrations, the diff, or an explicit `covenant record effect`.

## Development

```bash
pip install -e ".[dev]"
npm install --prefix /tmp/ts typescript@5.6.3   # for the TypeScript checker tests
COVENANT_TYPESCRIPT=/tmp/ts/node_modules/typescript python -m pytest -q
```

Layout of `src/covenant/`:
- `ir.py`: the rule format;
- `policy.py`: team policies;
- `runtime.py`, `monitors/` and `verifiers/`: enforcement;
- `compiler/`;
- `integrations/`: Claude Code and MCP;
- `ci.py`, `server.py`, `dashboard.py` and `cli.py`;
- `bench/`: CovenantBench.

The worked example from the proposal is in `examples/mylib`. The benchmark tasks are in
`bench/tasks/`, and the VS Code extension is in `vscode-extension/`.

## Not built yet

These need tools this project's development machine doesn't have, so they couldn't be built and tested:

- **Container sandbox:** run the agent in a container or VM that can't see Covenant's state.
  The hook server is the piece that makes this work. What's missing is the container image, a
  bind mount of the repo, the hook URL pointing at the host, and copy-on-write snapshots in
  place of the file copies. It needs Docker or WSL to build and test.
- **Live database proxy:** see and roll back SQL against Postgres or MySQL as it runs. SQLite
  databases are already guarded row by row (`sqlite_guard`), and migrations are checked by
  `schema_guard`. It needs a database server to test against.
- **A real-repo CovenantBench:** the harness, metrics and five synthetic tasks exist. The
  proposal's 60–200 tasks from real repos, using commits after the models' training cutoff and
  with annotated ground truth, are curation work.
