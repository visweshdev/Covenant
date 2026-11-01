<sub>[Covenant](../README.md) › [Docs](README.md) › Commands</sub>

# ⌨️ Commands

Every command takes `--repo PATH` to act on another repo. `covenant <command> --help` shows all
flags.

---

## Set up and rules

| Command | What it does |
|---|---|
| `covenant compile "<instruction>"` | Writes `covenant.yaml`. Flags: `--pack`, `--offline`, `--backend`, `--no-review`, `--unattended`, `--answer Q1=2`, `--dry-run`, `--yes` |
| `covenant packs` | Lists the rule packs |
| `covenant suggest` | Proposes rules from the repo's history (`--github` adds PR review comments) |
| `covenant init` | Validates, installs git and Claude Code hooks, starts a session (`--no-fast-hooks`) |
| `covenant start` | Approves the current `covenant.yaml` and begins a task |
| `covenant amend "<instruction>"` | Adds rules mid-task, keeping the history; only additions are accepted. Same flags as `compile` |
| `covenant validate` | Checks `covenant.yaml` statically |

## During a task

| Command | What it does |
|---|---|
| `covenant test` | Runs the declared tests and records the result |
| `covenant check` | Runs every check now |
| `covenant status` | Shows the rules and what the agent has done |
| `covenant explain` | Why something was blocked and what can be done (`-n`, `--format json`) |
| `covenant snapshots` / `snapshot` / `undo [ID]` | Lists, takes and restores working-tree restore points |
| `covenant alerts [test]` | Shows the alert settings, or sends a test alert |
| `covenant dashboard` | Opens the local dashboard, where rules can be edited and approved (`--read-only`) |

## Integrations

| Command | What it does |
|---|---|
| `covenant ci` | Checks a pull request (`--base`, `--format github\|json\|text`, `--no-tests`, `--comment`) |
| `covenant mcp` | Runs the MCP server |
| `covenant serve` | Runs the hook server (`--install` points Claude Code and git at it) |
| `covenant sandbox [-- cmd]` | Runs the agent in Docker with the internet and Covenant's state out of reach (`--allow`, `--print`) |
| `covenant install-hooks` / `install-claude` | Installs only the git hooks / only the Claude Code hooks |
| `covenant daemon` | The background process behind the fast hooks (started automatically) |
| `covenant bench run` / `report` | Runs and scores CovenantBench (see [bench/README.md](../bench/README.md)) |
| `covenant bench replay [repos.txt]` | Judges real repos' recent commits with the CI check, nothing from them executed (`--commits`, `--jobs`, `--only`, `--report`); see [the replay results](../bench/README.md#replay-on-50-real-repositories) |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean |
| `1` | Violations: the agent should revise |
| `2` | Needs a human |
| `3` | Bad setup |

## Environment variables

| Variable | What it does |
|---|---|
| `ANTHROPIC_API_KEY` | Compile with the Claude API. Without it, your Claude Code login is used |
| `COVENANT_ANTHROPIC_BASE_URL` | Overrides `ANTHROPIC_BASE_URL` for Covenant only (`compile` warns when that points away from Anthropic) |
| `COVENANT_HOME` | Where state is kept (default `~/.covenant`) |
| `COVENANT_AGENT` | This agent's name, for path ownership rules |
| `COVENANT_POLICY` | Team policies applied to every repo on this machine |
| `COVENANT_SNAPSHOTS` | `0` turns restore points off |
| `COVENANT_SLACK_WEBHOOK`, `COVENANT_ALERT_WEBHOOK`, `COVENANT_ALERT_EMAIL` | Where alerts go |
| `COVENANT_HOOK_TOKEN` | The hook server's token (set by the sandbox) |
| `COVENANT_TYPESCRIPT` | The TypeScript package to use for TypeScript API checks |
| `GITHUB_TOKEN` | Lets `covenant ci --comment` post to the pull request (`suggest --github` uses the `gh` CLI's login instead) |

---

<sub>Previous: [🧰 Tools](tools.md) · Next: [🛡️ Safety model](safety.md)</sub>
