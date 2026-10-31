<p align="center"><img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/logo.png" width="110" alt="Covenant logo: a C closed by a short bar"></p>

# Covenant

Covenant turns the rules you give a coding agent ("don't change public APIs, don't delete
user data, run tests before committing") into checks that actually run, and enforces them
while the agent works, in git, and in CI.

It checks **outcomes**, not tool calls: the API surface after the edits, what happened to
protected files however they were touched, and the order of events (tests passed after the
last edit, before the commit).

```bash
sh install.sh                          # or on Windows: powershell -File install.ps1  (pipx, from GitHub)
cd your-repo
covenant compile "Update dependencies, but don't change public APIs, don't delete user data, and run tests before committing."
covenant init                          # git hooks + Claude Code hooks, then start enforcing
```

Or start from a ready-made rule set: `covenant compile --pack hotfix "fix the login crash"`
(`covenant packs` lists them). `covenant suggest` proposes rules from what went wrong in the
repo's own history.

## How it works

1. **Compile** (`covenant compile`): Claude, or the offline extractor with `--offline`, splits the
   instruction into a goal and clauses. Claude is reached through `ANTHROPIC_API_KEY` if it is set,
   and otherwise through your Claude Code login (`claude -p`), so a Claude subscription is enough
   (`--backend api|claude-code` to choose). Deterministic passes then:
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
| Temporal obligation | `trace_monitor` | Ordering, limit and deadline rules over the agent's steps, in past-time LTL (see below) |
| Semantic invariant | `scope_guard` | Only the files you name change ("only change files in src/"); optionally no new or deleted files |
| Semantic invariant | `secret_scan` | No API keys, tokens, private keys, passwords or `.env` files in the change (reports are masked) |
| Semantic invariant | `test_guard` | Tests aren't deleted, removed, skipped or focused with `.only` |
| Semantic invariant | `diff_budget` | The change stays within a number of files or lines |
| Semantic invariant | `no_debug_code` | No breakpoints, `debugger`, `console.log` or (optionally) debug prints left behind |
| Semantic invariant | `no_suppressions` | No new `# type: ignore`, `# noqa`, `@ts-ignore`, `eslint-disable` and the like |
| Semantic invariant | `large_files` | No added or changed file over a size limit |
| Effect prohibition | `command_guard` | Shell commands, web fetches and database tool calls refused before they run (see below) |
| Effect prohibition | `budget` | The task stops at a time, action or (estimated) cost limit |
| Semantic invariant | `path_owner` | Folders that belong to one team's agent change only by that agent |
| Semantic invariant | `llm_review` | Claude reviews each commit against a rule no checker covers; advisory, never blocks |

A rule with no checker ("keep the code readable") becomes an `llm_review` rule when Claude is
available: Claude reads each commit's change against it, and its findings go to the agent and to
you as warnings that never block. With `--offline` or `--no-review` the rule is kept and marked
`unenforceable`, never silently dropped.

### Rules the compiler understands

| Say | Becomes |
|---|---|
| "don't change public APIs" | `api_surface_diff` / `ts_api_surface_diff` / `source_api_diff` |
| "don't delete user data", "don't touch config/" | `effect_monitor`, `sqlite_guard` |
| "no destructive migrations" | `schema_guard` |
| "only update requests", "no major upgrades" | `dependency_diff` |
| "don't add new dependencies" | `dependency_diff` (upgrades are fine; new packages aren't) |
| "only change files in src/" | `scope_guard` |
| "don't commit secrets or API keys" | `secret_scan` |
| "don't delete or skip tests" | `test_guard` |
| "keep the change under 200 lines", "touch at most 3 files" | `diff_budget` |
| "no leftover debug prints" | `no_debug_code` |
| "don't silence type errors" | `no_suppressions` |
| "don't commit files over 5 MB" | `large_files` |
| "run tests before committing", "push at most once", "run tests within 5 steps of an edit" | `trace_monitor` |
| "never force push", "don't deploy", "don't install packages", "no network access", "don't read .env" | `command_guard` |
| "never run destructive SQL" | `command_guard` (`destructive_sql`) |
| "only reach pypi.org and github.com" | `command_guard` (`network` with `allow_hosts`) |
| "only the payments team's agent may edit billing/" | `path_owner` |
| "stop after 20 minutes", "spend at most $2", "use at most 50 tool calls" | `budget` |

`command_guard` categories: `force_push`, `history_rewrite`, `branch_delete`, `discard_changes`,
`sudo`, `pipe_to_shell`, `recursive_delete`, `install_packages`, `publish`, `deploy`, `network`,
`read_secrets`, `database_cli`, `kill_processes`, `system_changes`, `destructive_sql`, plus your
own `patterns`. It guards against mistakes and shortcuts; an agent determined to hide a command
(by writing it into a script first) needs the sandbox.

- **Destructive SQL** (Postgres, MySQL, SQLite, SQL Server, DuckDB, MongoDB, Redis): Covenant
  reads the SQL itself, inline (`psql -c`, `mysql -e`, piped in), in `.sql` files fed to a client,
  and in database MCP tool calls. `DROP`, `TRUNCATE`, `ALTER TABLE ... DROP`, and `DELETE` or
  `UPDATE` without a `WHERE` are refused; a `DELETE ... WHERE` is fine.
- **Network allowlist:** with `allow_hosts`, only those hosts (and their subdomains) can be
  reached, by commands, `git push`/`pull` (the remote's host), package installs (their registry)
  and the WebFetch tool. A network command whose destination can't be worked out is refused.
- **Path ownership:** an agent says who it is with `COVENANT_AGENT=payments claude`; edits to
  folders it doesn't own are refused, and so is a commit that changes them.
- **Budgets:** checked before each action. Cost is estimated from the token counts in Claude
  Code's session log at Anthropic's API prices (on a subscription nothing is charged per token,
  but it is still a fair measure of how much work was done).

### Rule packs

`covenant compile --pack NAME` adds a ready-made rule set to your instruction. Packs always
compile offline, so a pack means the same rules everywhere.

| Pack | For |
|---|---|
| `safe-refactor` | restructuring code without changing what it does |
| `dependency-bump` | updating dependencies without breaking anything |
| `docs-only` | changing documentation and nothing else |
| `hotfix` | a small, urgent, well-tested fix |
| `data-safe` | working near user data and databases without losing any |
| `secure` | keeping secrets and the machine safe |
| `lockdown` | the strictest set: no installs, no network, no history rewrites |

### Rules over the agent's steps

A step is one recorded action: an edit, a command, a test run, a commit or a push.

| Instruction | Formula |
|---|---|
| "run tests before committing" | `G(commit -> (!edit S test_pass))` |
| "never push" | `G(!push)` |
| "push at most once", "don't commit more than 3 times" | `G(#push <= 1)`, `G(#commit <= 3)` |
| "run the tests within 5 steps of any edit" | `G(!(!test S[>=5] edit))` |

The operators:
- `S` (since), `Y` (previous step), `O` (at some step so far) and `H` (at every step so far);
- bounded forms of these, `O[<=n]`, `H[<=n]` and `a S[<=n] b` / `a S[>=n] b`, which count steps back;
- `#a <= n` (also `<`, `>=`, `>`, `==`), which counts how often `a` has happened.

A limit counts every commit or push this task, including ones later undone with `git reset`.

## Agents

**Claude Code:** `covenant init` (or `covenant install-claude`) adds hooks to `.claude/settings.json`:

| Hook | What Covenant does |
|---|---|
| `SessionStart` | Starts a session and tells Claude the rules |
| `PreToolUse` | Denies commits and pushes that would break a rule, edits to protected or someone else's files, refused commands, web fetches and database tool calls, actions past a budget, changes to Covenant's own files, and hook bypasses (`--no-verify`, `core.hooksPath`). Takes a restore point before risky steps |
| `PostToolUse` / `PostToolUseFailure` | Records edits, commands and test runs, and reports side effects found by the snapshot scan |
| `Stop` | Claude can't finish while a rule is broken; after repeated blocks it hands over to you |

The hook fails closed: if it crashes before a tool call, the call is denied.

Hooks are fast: each one starts a tiny client that uses only Python's standard library and hands
the event to a background `covenant daemon` for the repo, which keeps Covenant loaded. That takes
about 0.2 s per agent action on Windows instead of about 1 s. When no daemon answers, the client
runs the full check itself and starts one, so a missing daemon never skips a check. The daemon
listens on 127.0.0.1 only, exits after 30 idle minutes, and restarts when Covenant is upgraded.
`covenant init --no-fast-hooks` starts a fresh Python for every hook instead.

A test run counts when it runs the whole suite and its exit status is visible. Extra flags
that only change output or speed (`-x`, `-v`, `-p no:cacheprovider`, `--cov`, `-n auto`,
`2>&1`) are fine. A run that picks some tests (a path, `-k`, `-m`, `--lf`) or pipes its output
doesn't count, and the agent is told why right away.

**Cursor, Codex and other MCP clients:** `covenant --repo <path> mcp` is an MCP server with
`covenant_rules`, `covenant_run_tests`, `covenant_record_edit`, `covenant_preflight`,
`covenant_check`, `covenant_check_command` and `covenant_explain`. MCP only works when the
agent chooses to call it, so the git hooks (and CI) are what enforce commits and pushes for
these agents.

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

A policy can also live at an https address, such as a raw file in an org-wide repo:
`extends: ["https://example.com/team.yaml#sha256=<hash>"]`. The `#sha256=` pin is optional;
with it, a policy that changes without the pin being updated is an error. The last good copy
is kept in `~/.covenant/policies/` and used when the network is down.

Policy rules:
- are merged in first, with an `origin` naming their policy;
- can't be redefined by a repo: an id clash is an error;
- are protected from the agent like Covenant's own files;
- in CI, come from the base commit, and a PR that changes them is flagged.

## Dashboard

`covenant dashboard` opens a page on `127.0.0.1:8765` covering every repo Covenant enforces on this
machine:
- each repo's rules, with how close each is to handing over to a human;
- a timeline of blocks and reports;
- recent events and protected paths;
- an editor for the rules: write a rule in plain English and Claude drafts it, check the result,
  then save and approve. Added rules apply from now on, like `covenant amend`; changing or
  removing a rule restarts the task, after asking;
- a replay of any task, this one or one of the last 30: step through everything the agent did
  and every decision Covenant made, with why each rule fired and how to fix it.

It refreshes every few seconds and rejects requests whose Host header isn't localhost. Changes
need a key created at start: the browser gets it in the part of the address after `#`, which is
never sent over the network, and the page removes it from the address bar. Requests from other
web pages are refused. `--read-only` turns editing off.

## Undo, explain, alerts

- **Undo snapshots:** before a risky step (`rm`, `mv`, `git reset`/`checkout`/`clean`, `sed -i`,
  redirects, formatters, rewriting a whole file), Covenant records the working tree, untracked
  files included, as a git commit under `refs/covenant/snapshots/`. Your branch, index and stash
  aren't touched, and the refs are never pushed. `covenant snapshots` lists them and
  `covenant undo [ID]` restores one. An undo first snapshots the current state, so it can be
  undone too. `COVENANT_SNAPSHOTS=0` turns them off.
- **`covenant explain`:** what was blocked, why, what the agent can do and what you can do, in
  plain words (`-n 3` for the last three).
- **Alerts:** a Slack message, a webhook call or an email when an agent needs a human (and
  optionally whenever it is blocked). Set `COVENANT_SLACK_WEBHOOK`, `COVENANT_ALERT_WEBHOOK` or
  `COVENANT_ALERT_EMAIL`, or write `~/.covenant/alerts.yaml` (see `src/covenant/alerts.py`).
  `covenant alerts test` sends a test message. The same problem alerts at most once every 10
  minutes.
- **`covenant suggest`:** reads the repo's history for reverted changes, removed secrets,
  deleted tests, debug code cleaned up later, restored APIs and rolled-back upgrades, and
  proposes rules with the evidence. `--github` also reads recent PR review comments.

## Sandbox

`covenant sandbox` runs the agent in Docker, in three containers on a private network with no
route to the internet:
- **agent:** your repo and Claude Code (or any command after `--`);
- **hooks:** `covenant serve`, holding the task's state in a volume the agent can't see;
- **proxy:** the only way out, and only to `api.anthropic.com`, the hosts in your `allow_hosts`
  rules and `--allow` hosts. Everything else gets a 403.

So an agent that dodges a command rule (say, by writing the command into a script) still can't
reach a host that isn't allowed, or touch Covenant's state. `covenant sandbox --print` writes
the files and shows the `docker compose` commands without running them. One limit: the hook
token is in the agent's settings, so a determined agent could post made-up hook events. The
server still checks commits, pushes and diffs itself, but a made-up "tests passed" would count;
run `covenant test` from outside when that matters.

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
permissions:
  contents: read
  pull-requests: write          # for the comments
jobs:
  covenant:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: visweshdev/Covenant@main
        # with: { comment: "false", run-tests: "false", agent: payments }
```

The action comments on the exact lines that break a rule (once; re-runs don't repeat a
comment) and keeps one summary comment on the PR up to date. Problems with no changed line to
point at, like a deleted protected file, go in the summary. Without the action:
`covenant ci --format github --comment` does the same with `GITHUB_TOKEN` set.

How the CI check judges a pull request:
- The rules come from the **base** commit, so a PR can't weaken them; a PR that changes `covenant.yaml` is flagged for human review.
- API and migration checkers compare against the base.
- Protected files are checked against the diff.
- Test commands run on the PR's final state.

## Commands

| Command | What it does |
|---|---|
| `covenant compile "<instruction>"` | Writes `covenant.yaml`. Flags: `--pack`, `--offline`, `--backend`, `--no-review`, `--unattended`, `--answer Q1=2`, `--dry-run`, `--yes` |
| `covenant packs` | Lists the rule packs |
| `covenant suggest` | Proposes rules from the repo's history (`--github` adds PR review comments) |
| `covenant explain` | Why something was blocked and what can be done (`-n`, `--format json`) |
| `covenant snapshots` / `snapshot` / `undo [ID]` | Lists, takes and restores working-tree restore points |
| `covenant alerts [test]` | Shows the alert settings, or sends a test alert |
| `covenant sandbox [-- cmd]` | Runs the agent in Docker with the internet and Covenant's state out of reach (`--allow`, `--print`) |
| `covenant init` | Validates, installs git and Claude Code hooks, starts a session (`--no-fast-hooks`) |
| `covenant start` | Approves the current `covenant.yaml` and begins a task |
| `covenant amend "<instruction>"` | Adds rules mid-task, keeping the history; only additions are accepted. Same flags as `compile` |
| `covenant test` / `check` / `status` | Runs the declared tests / every check / shows the state |
| `covenant ci` | Checks a pull request (`--base`, `--format github\|json\|text`, `--no-tests`, `--comment`) |
| `covenant mcp` | Runs the MCP server |
| `covenant serve` | Runs the hook server (`--install` points Claude Code and git at it) |
| `covenant dashboard` | Opens the local dashboard, where rules can be edited and approved (`--read-only`) |
| `covenant daemon` | The background process behind the fast hooks (started automatically) |
| `covenant bench run` / `report` | Runs and scores CovenantBench (see `bench/README.md`) |

Exit codes: 0 clean, 1 violations (the agent should revise), 2 needs a human, 3 bad setup.

Compiling with Claude reads `ANTHROPIC_API_KEY`, or uses your Claude Code login when it isn't set. If `ANTHROPIC_BASE_URL` points somewhere other
than Anthropic (agent CLIs and proxies often export it), `compile` warns you; set
`COVENANT_ANTHROPIC_BASE_URL=https://api.anthropic.com` to override it for Covenant only.

## Safety model

- State lives outside the repo, in `~/.covenant/workspaces/<repo>-<hash>` (set `COVENANT_HOME` to move it). A file lock keeps parallel hooks from corrupting it.
- The approved Covenant is pinned when a session starts. If `covenant.yaml`, the git hooks or the Claude settings change, it's reported as an `INTEGRITY` violation, and so is `disableAllHooks` or `core.hooksPath`.
- A verifier that can't run blocks rather than passes.

**Limits.** Outside the sandbox, Covenant runs as your user. An agent with a shell could still
edit its state folder directly. That gets detected, but it isn't prevented; `covenant sandbox`
prevents it. Effects are found by comparing hashes on every event, not by kernel events. SQLite
databases are compared row by row; for other databases, Covenant sees the SQL the agent runs
(commands, `.sql` files, database tools) and migrations, but not queries the app makes on its own.

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

- **The sandbox hasn't run in Docker yet:** its compose file, images and allowlist proxy are
  built and tested (the proxy for real, over sockets), but this project's development machine
  has no Docker, so the three containers haven't been started together.
- **Live database proxy:** row-level rollback of SQL as it runs against Postgres or MySQL,
  including queries the app makes on its own. Destructive SQL the agent runs is already refused
  (`destructive_sql`), and SQLite is guarded row by row. It needs a database server to test against.
- **A real-repo CovenantBench:** the harness, metrics and six synthetic tasks exist. The
  proposal's 60–200 tasks from real repos, using commits after the models' training cutoff and
  with annotated ground truth, are curation work.
- **Publishing:** `.github/workflows/release.yml` publishes to PyPI and the VS Code Marketplace
  on a `v*` tag, once the PyPI trusted publisher and the `VSCE_PAT` secret are set up.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and tests, and [SECURITY.md](SECURITY.md) to
report a way around Covenant. Covenant is released under the [MIT License](LICENSE).
