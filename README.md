<p align="center">
  <img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/assets/banner.svg" width="100%" alt="Covenant: rules for coding agents that actually hold">
</p>

<p align="center">
  <a href="https://pypi.org/project/covenant-agents/"><img src="https://img.shields.io/pypi/v/covenant-agents?style=flat-square&color=b5452f&labelColor=1d1b19&label=pypi" alt="PyPI version"></a>
  <a href="https://pypi.org/project/covenant-agents/"><img src="https://img.shields.io/pypi/pyversions/covenant-agents?style=flat-square&color=b5452f&labelColor=1d1b19" alt="Python versions"></a>
  <a href="https://github.com/visweshdev/Covenant/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/visweshdev/Covenant/ci.yml?branch=main&style=flat-square&labelColor=1d1b19&label=tests" alt="Tests"></a>
  <a href="https://github.com/visweshdev/Covenant/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-b5452f?style=flat-square&labelColor=1d1b19" alt="MIT license"></a>
</p>

<p align="center">
  <a href="#quick-start"><b>Quick start</b></a> &nbsp;·&nbsp;
  <a href="#how-it-works"><b>How it works</b></a> &nbsp;·&nbsp;
  <a href="#what-it-can-enforce"><b>Rules</b></a> &nbsp;·&nbsp;
  <a href="https://github.com/visweshdev/Covenant/blob/main/docs/README.md"><b>Docs</b></a> &nbsp;·&nbsp;
  <a href="https://github.com/visweshdev/Covenant/blob/main/CHANGELOG.md"><b>Changelog</b></a>
</p>

<br>

You tell an agent *"don't change public APIs, don't delete user data, run tests before
committing."* It usually listens. **Covenant makes sure it does.**

It checks **outcomes, not tool calls**: the API surface after the edits, what happened to
protected files however they were touched, and the order of events (tests passed *after* the
last edit, *before* the commit).

<p align="center">
  <img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/assets/demo.svg" width="100%" alt="Animated illustration: an agent tries a force push, destructive SQL and an untested commit; Covenant refuses each, then allows the commit once the tests pass">
</p>

## Quick start

```bash
pipx install covenant-agents        # or: pip install covenant-agents
```

```bash
cd your-repo
covenant compile "Don't change public APIs, don't delete user data, run tests before committing."
covenant init        # installs git + Claude Code hooks and starts enforcing
```

That's it. Works on **Windows, macOS and Linux** with Python 3.11+.

> 💡 **Tip:** start from a ready-made rule set with `covenant compile --pack hotfix "fix the login crash"`
> (`covenant packs` lists them), or let `covenant suggest` propose rules from what went wrong in
> your repo's own history.

## How it works

<p align="center">
  <img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/assets/how-it-works.svg" width="100%" alt="You write rules in plain English; covenant compile grounds them in the repo and binds each to a checker in covenant.yaml; they are enforced while the agent works, at commit and push, and on every pull request">
</p>

When something breaks a rule, the agent gets the rule, the evidence and a fix, and you can ask
for the story any time:

```text
$ covenant explain
At 01:23 on 01 Nov, the agent tried to use a tool. Covenant blocked it.

Rule C3: "never run destructive SQL"
  Why: The command counts as SQL that drops a table, database or schema: psql -c "DROP TABLE users".
  What the agent can do: Don't drop, truncate or wipe tables; change rows with a WHERE, or ask the human
```

<p align="center">
  <img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/assets/features.svg" width="100%" alt="Checks outcomes, not tool calls. Refuses risky commands before they run. Undo with restore points. Explains every block. Works with every agent. CI rules a pull request can't weaken.">
</p>

## What it can enforce

Say it in plain English; Covenant picks the checker.

| You say | Covenant checks |
|---|---|
| *"don't change public APIs"* | Exported names and signatures in Python, TypeScript, Go, Rust and Java |
| *"don't delete user data"*, *"don't touch config/"* | Protected files and SQLite rows, put back from a snapshot if lost |
| *"no destructive migrations"* | No dropped tables or columns, truncation or type changes |
| *"only update requests"*, *"no new dependencies"* | Only allowed packages change, majors stay put |
| *"only change files in src/"* | The change stays inside the folders you name |
| *"don't commit secrets"*, *"don't skip tests"* | Keys, tokens and `.env` files; deleted, skipped or `.only` tests |
| *"keep the change under 200 lines"* | Files and lines changed |
| *"no debug prints"*, *"don't silence type errors"* | Leftover breakpoints and logs; new `noqa`, `@ts-ignore` and friends |
| *"run tests before committing"*, *"push at most once"* | The order and count of the agent's steps |
| *"never force push"*, *"don't deploy"*, *"never run destructive SQL"* | Refused before they run (16 categories) |
| *"only reach pypi.org and github.com"* | A network allowlist for commands, git remotes, installs and fetches |
| *"only the payments agent may edit billing/"* | Folder ownership between agents |
| *"stop after 20 minutes"*, *"spend at most $2"* | Time, action and cost budgets |

<p align="right"><a href="https://github.com/visweshdev/Covenant/blob/main/docs/rules.md"><b>Every checker, phrase and rule pack →</b></a></p>

## Works with

<table>
<tr>
  <td width="33%" valign="top"><h3>🤖 Claude Code</h3>Hooks on every tool call. Refuses bad actions, records test runs, and won't let Claude finish while a rule is broken.</td>
  <td width="33%" valign="top"><h3>🧩 Cursor, Codex, any MCP client</h3>An MCP server with the rules, a preflight check and a test runner. Git hooks enforce commits and pushes.</td>
  <td width="33%" valign="top"><h3>🌿 Git</h3>Pre-commit and pre-push hooks that check every commit and push, from any agent or a human.</td>
</tr>
<tr>
  <td width="33%" valign="top"><h3>✅ GitHub</h3>An Action that judges each PR by its base commit's rules and comments on the exact lines.</td>
  <td width="33%" valign="top"><h3>🧑‍💻 VS Code</h3>A rules panel, violations in Problems, and a 🔒 badge on every protected file.</td>
  <td width="33%" valign="top"><h3>🐳 Docker</h3><code>covenant sandbox</code> runs the agent behind an allowlist proxy, with Covenant's state out of reach.</td>
</tr>
</table>

<p align="right">
  <a href="https://github.com/visweshdev/Covenant/blob/main/docs/agents.md"><b>Set up each agent →</b></a> &nbsp;
  <a href="https://github.com/visweshdev/Covenant/blob/main/docs/ci.md"><b>CI and the GitHub Action →</b></a>
</p>

## Tried on real repos

<p align="center">
  <img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/assets/stats.svg" width="100%" alt="Violations per task fell from 1.17 to 0 on CovenantBench; task success stayed at 100%; on 4 real repos every trap was stopped; about 0.2 seconds per agent action">
</p>

Claude Code ran live under Covenant on [TinyDB](https://github.com/msiemens/tinydb),
[itsdangerous](https://github.com/pallets/itsdangerous),
[humanize](https://github.com/python-humanize/humanize) and
[prettytable](https://github.com/prettytable/prettytable), each with an honest task and a trap
task. Every honest task committed; every trap was refused or blocked.

<p align="right"><a href="https://github.com/visweshdev/Covenant/blob/main/bench/README.md"><b>Benchmark and trial notes →</b></a></p>

## Also included

| Feature | What it does |
|---|---|
| ↩️ **Undo** | Restore points before risky steps; `covenant undo` puts the working tree back |
| 💬 **Explain** | `covenant explain` says what was blocked, why, and what you and the agent can do |
| 🔔 **Alerts** | Slack, a webhook or email when an agent needs a human |
| 📊 **Dashboard** | Every repo's rules, a timeline of blocks, a rule editor, and a step-by-step replay of any task |
| 👥 **Team policies** | Shared rules from a file or an https address, optionally pinned by sha256 |

<p align="right"><a href="https://github.com/visweshdev/Covenant/blob/main/docs/tools.md"><b>Dashboard, undo, alerts and policies →</b></a></p>

## Documentation

| | Page | What's in it |
|---|---|---|
| 📏 | [Rules](https://github.com/visweshdev/Covenant/blob/main/docs/rules.md) | Checkers, what the compiler understands, refused commands, rule packs, step rules |
| 🤖 | [Agents](https://github.com/visweshdev/Covenant/blob/main/docs/agents.md) | Claude Code, MCP clients, the hook server, the sandbox, VS Code |
| ✅ | [CI](https://github.com/visweshdev/Covenant/blob/main/docs/ci.md) | The GitHub Action and `covenant ci` |
| 🧰 | [Tools](https://github.com/visweshdev/Covenant/blob/main/docs/tools.md) | Dashboard, undo, explain, alerts, suggest, team policies |
| ⌨️ | [Commands](https://github.com/visweshdev/Covenant/blob/main/docs/commands.md) | Every command, flag, exit code and environment variable |
| 🛡️ | [Safety model](https://github.com/visweshdev/Covenant/blob/main/docs/safety.md) | What Covenant protects against, and what it doesn't |

## Contributing

Bug reports, new checkers and benchmark tasks are welcome. See
[CONTRIBUTING.md](https://github.com/visweshdev/Covenant/blob/main/CONTRIBUTING.md), and
[SECURITY.md](https://github.com/visweshdev/Covenant/blob/main/SECURITY.md) to report a way
around Covenant privately.

<br>

<p align="center">
  <img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/logo.png" width="44" alt="Covenant logo"><br>
  <sub>Made for agents that mean well. Released under the <a href="https://github.com/visweshdev/Covenant/blob/main/LICENSE">MIT License</a>.</sub>
</p>
