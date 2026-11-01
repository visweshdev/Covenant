<sub>[Covenant](../README.md) › [Docs](README.md) › Rules</sub>

# 📏 Rules

How plain-English instructions become checks, and every check Covenant can run.

**On this page:** [Compiling](#compiling) · [Checkers](#checkers) ·
[What the compiler understands](#what-the-compiler-understands) ·
[Refused commands](#refused-commands) · [Rule packs](#rule-packs) ·
[Rules over the agent's steps](#rules-over-the-agents-steps)

---

## Compiling

`covenant compile "<instruction>"` writes `covenant.yaml`.

1. **Split.** Claude, or the offline extractor with `--offline`, splits the instruction into a
   goal and clauses.
2. **Ground.** Each clause is tied to the repo: the real exports, data folders, tables and test
   command.
3. **Bind.** Each clause gets a checker.
4. **Ask.** At most three questions, each about a specific thing in the repo.
5. **Validate.** The result is checked statically, then you approve it.

Claude is reached through `ANTHROPIC_API_KEY` if it is set, and otherwise through your Claude
Code login (`claude -p`), so a Claude subscription is enough. Choose with
`--backend api|claude-code`.

To add a rule mid-task, run `covenant amend "also don't touch config/"`. The new rules apply
from then on and the task's history is kept.

> [!NOTE]
> A rule with no checker ("keep the code readable") becomes an `llm_review` rule when Claude is
> available: Claude reads each commit's change against it, and its findings go to the agent and
> to you as warnings that never block. With `--offline` or `--no-review` the rule is kept and
> marked `unenforceable`, never silently dropped.

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
| Temporal obligation | `trace_monitor` | Ordering, limit and deadline rules over the agent's steps ([see below](#rules-over-the-agents-steps)) |
| Semantic invariant | `scope_guard` | Only the files you name change ("only change files in src/"); optionally no new or deleted files |
| Semantic invariant | `secret_scan` | No API keys, tokens, private keys, passwords or `.env` files in the change (reports are masked) |
| Semantic invariant | `test_guard` | Tests aren't deleted, removed, skipped or focused with `.only` |
| Semantic invariant | `diff_budget` | The change stays within a number of files or lines |
| Semantic invariant | `no_debug_code` | No breakpoints, `debugger`, `console.log` or (optionally) debug prints left behind |
| Semantic invariant | `no_suppressions` | No new `# type: ignore`, `# noqa`, `@ts-ignore`, `eslint-disable` and the like |
| Semantic invariant | `large_files` | No added or changed file over a size limit |
| Effect prohibition | `command_guard` | Shell commands, web fetches and database tool calls refused before they run ([see below](#refused-commands)) |
| Effect prohibition | `budget` | The task stops at a time, action or (estimated) cost limit |
| Semantic invariant | `path_owner` | Folders that belong to one team's agent change only by that agent |
| Semantic invariant | `llm_review` | Claude reviews each commit against a rule no checker covers; advisory, never blocks |

## What the compiler understands

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
| "don't silence type errors" | `no_suppressions` (type-checker suppressions only: `# type: ignore`, `@ts-ignore`, ...) |
| "don't disable lint rules", "don't suppress warnings" | `no_suppressions` (lint only / everything, coverage pragmas included) |
| "don't commit files over 5 MB" | `large_files` |
| "run tests before committing", "push at most once", "run tests within 5 steps of an edit" | `trace_monitor` |
| "never force push", "don't deploy", "don't install packages", "no network access", "don't read .env" | `command_guard` |
| "never run destructive SQL" | `command_guard` (`destructive_sql`) |
| "only reach pypi.org and github.com" | `command_guard` (`network` with `allow_hosts`) |
| "only the payments team's agent may edit billing/" | `path_owner` |
| "stop after 20 minutes", "spend at most $2", "use at most 50 tool calls" | `budget` |

## Refused commands

`command_guard` refuses commands before they run. Its categories:

| | | | |
|---|---|---|---|
| `force_push` | `history_rewrite` | `branch_delete` | `discard_changes` |
| `sudo` | `pipe_to_shell` | `recursive_delete` | `install_packages` |
| `publish` | `deploy` | `network` | `read_secrets` |
| `database_cli` | `kill_processes` | `system_changes` | `destructive_sql` |

You can add your own `patterns` too.

<details>
<summary><b>Destructive SQL</b></summary>

Works for Postgres, MySQL, SQLite, SQL Server, DuckDB, MongoDB and Redis. Covenant reads the
SQL itself: inline (`psql -c`, `mysql -e`, piped in), in `.sql` files fed to a client, and in
database MCP tool calls. `DROP`, `TRUNCATE`, `ALTER TABLE ... DROP`, and `DELETE` or `UPDATE`
without a `WHERE` are refused; a `DELETE ... WHERE` is fine.
</details>

<details>
<summary><b>Network allowlist</b></summary>

With `allow_hosts`, only those hosts (and their subdomains) can be reached: by commands, by
`git push`/`pull` (the remote's host), by package installs (their registry) and by the WebFetch
tool. A network command whose destination can't be worked out is refused.
</details>

<details>
<summary><b>Path ownership</b></summary>

An agent says who it is with `COVENANT_AGENT=payments claude`. Edits to folders it doesn't own
are refused, and so is a commit that changes them.
</details>

<details>
<summary><b>Budgets</b></summary>

Checked before each action. Cost is estimated from the token counts in Claude Code's session
log at Anthropic's API prices. On a subscription nothing is charged per token, but it is still a
fair measure of how much work was done.
</details>

> [!IMPORTANT]
> `command_guard` guards against mistakes and shortcuts. An agent determined to hide a command
> (by writing it into a script first) needs the [sandbox](agents.md#sandbox).

## Rule packs

`covenant compile --pack NAME` adds a ready-made rule set to your instruction. Packs always
compile offline, so a pack means the same rules everywhere. `covenant packs` lists them.

| Pack | For |
|---|---|
| `safe-refactor` | restructuring code without changing what it does |
| `dependency-bump` | updating dependencies without breaking anything |
| `docs-only` | changing documentation and nothing else |
| `hotfix` | a small, urgent, well-tested fix |
| `data-safe` | working near user data and databases without losing any |
| `secure` | keeping secrets and the machine safe |
| `lockdown` | the strictest set: no installs, no network, no history rewrites |

```bash
covenant compile --pack safe-refactor --pack secure "split utils.py into smaller modules"
```

## Rules over the agent's steps

A step is one recorded action: an edit, a command, a test run, a commit or a push. These rules
are written in past-time LTL.

| Instruction | Formula |
|---|---|
| "run tests before committing" | `G(commit -> (!edit S test_pass))` |
| "never push" | `G(!push)` |
| "push at most once", "don't commit more than 3 times" | `G(#push <= 1)`, `G(#commit <= 3)` |
| "run the tests within 5 steps of any edit" | `G(!(!test S[>=5] edit))` |

| Operator | Meaning |
|---|---|
| `S` | since |
| `Y` | the previous step |
| `O` | at some step so far |
| `H` | at every step so far |
| `O[<=n]`, `H[<=n]`, `a S[<=n] b`, `a S[>=n] b` | bounded forms that count steps back |
| `#a <= n` (also `<`, `>=`, `>`, `==`) | how often `a` has happened |

A limit counts every commit or push this task, including ones later undone with `git reset`.

### What counts as a test run

A test run counts when it runs the whole suite and its exit status is visible. Extra flags
that only change output or speed (`-x`, `-v`, `-p no:cacheprovider`, `--cov`, `-n auto`,
`2>&1`) are fine. A run that picks some tests (a path, `-k`, `-m`, `--lf`) or pipes its output
doesn't count, and the agent is told why right away.

---

<sub>Next: [🤖 Agents](agents.md)</sub>
