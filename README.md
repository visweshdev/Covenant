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

Covenant turns plain-English instructions into repository rules for coding agents. Use it to
check for public API changes, protect data files, and require a passing test run after the last
recorded edit. It checks changes and recorded events at supported checkpoints, and Claude Code
hooks can refuse covered actions before they run.

<p align="center">
  <img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/assets/demo.svg" width="100%" alt="Animated illustration: an agent tries a force push, destructive SQL and an untested commit; Covenant refuses each, then allows the commit once the tests pass">
</p>

## Quick start

```bash
pipx install covenant-agents        # or: pip install covenant-agents
```

```bash
cd your-repo
covenant compile --offline "Don't change public APIs, don't delete user data, run tests before committing."
covenant init
covenant status
```

Run this in a Git repository with at least one commit. Review the draft before accepting
`covenant.yaml`. The example uses the offline compiler, which works without Claude credentials.
`covenant init` installs Git and Claude Code hooks and approves the rules for a new task.
Covenant requires Python 3.11+ and Git and supports Windows, macOS and Linux.

**Already have Git hooks?** Use `covenant init --no-git` and integrate Covenant with your
hooks. PyPI 0.1.2 replaces existing default hooks, while the current source refuses to
overwrite them. [Git hook setup](https://github.com/visweshdev/Covenant/blob/main/docs/agents.md#git-hooks)

> Start with a rule pack using `covenant compile --offline --pack hotfix "fix the login crash"`.
> Run `covenant packs` to list the packs or `covenant suggest` to propose rules from the
> repository's history.

## How it works

<p align="center">
  <img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/assets/how-it-works.svg" width="100%" alt="Covenant compiles plain English into typed rules that can run through agent hooks, Git hooks, and CI">
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
  <img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/assets/features.svg" width="100%" alt="Outcome checks, tool preflight, restore points, violation reports, agent integrations, and CI checks">
</p>

## What it can enforce

Say it in plain English; Covenant picks the checker.

| You say | Covenant checks |
|---|---|
| *"don't change public APIs"* | Exported names and signatures in Python, TypeScript, Go, Rust and Java |
| *"don't delete user data"*, *"don't touch config/"* | Protected paths and SQLite data, with recovery for rules configured to roll back |
| *"no destructive migrations"* | No dropped tables or columns, truncation or type changes |
| *"only update requests"*, *"no new dependencies"* | Only allowed packages change, majors stay put |
| *"only change files in src/"* | The change stays inside the folders you name |
| *"don't commit secrets"*, *"don't skip tests"* | Keys, tokens and `.env` files; deleted, skipped or `.only` tests |
| *"keep the change under 200 lines"* | Files and lines changed |
| *"no debug prints"*, *"don't silence type errors"* | Leftover breakpoints and logs, plus new type-checker suppressions such as `@ts-ignore` |
| *"run tests before committing"*, *"push at most once"* | The order and count of the agent's steps |
| *"never force push"*, *"don't deploy"*, *"never run destructive SQL"* | Refused before they run (16 categories) |
| *"only reach pypi.org and github.com"* | A host allowlist for recognized commands, Git remotes, installs and fetches |
| *"only the payments agent may edit billing/"* | Folder ownership between agents |
| *"stop after 20 minutes"*, *"spend at most $2"* | Time, action and cost budgets |

<p align="right"><a href="https://github.com/visweshdev/Covenant/blob/main/docs/rules.md"><b>Every checker, phrase and rule pack →</b></a></p>

## Works with

<table>
<tr>
  <td width="33%" valign="top"><h3>🤖 Claude Code</h3>Tool hooks can refuse covered actions, record test runs and request revisions at task end.</td>
  <td width="33%" valign="top"><h3>🧩 Cursor, Codex and MCP</h3>Clients can query rules, check commands and run tests through the MCP server.</td>
  <td width="33%" valign="top"><h3>🌿 Git</h3>Installed pre-commit and pre-push hooks check the changes Git presents to Covenant.</td>
</tr>
<tr>
  <td width="33%" valign="top"><h3>✅ GitHub</h3>An Action that checks changes against the base rules and can comment on findings.</td>
  <td width="33%" valign="top"><h3>🧑‍💻 VS Code</h3>A rules panel, violations in Problems, and a 🔒 badge on every protected file.</td>
  <td width="33%" valign="top"><h3>🐳 Docker</h3><code>covenant sandbox</code> runs the agent with an HTTP(S) allowlist proxy and a separate hook state volume.</td>
</tr>
</table>

<p align="right">
  <a href="https://github.com/visweshdev/Covenant/blob/main/docs/agents.md"><b>Set up each agent →</b></a> &nbsp;
  <a href="https://github.com/visweshdev/Covenant/blob/main/docs/ci.md"><b>CI and the GitHub Action →</b></a>
</p>

See the [safety model](https://github.com/visweshdev/Covenant/blob/main/docs/safety.md) for
local hook, sandbox, and CI boundaries.

## Evaluation

<p align="center">
  <img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/assets/stats.svg" width="100%" alt="Six scripted task results, historical replay of 1,446 commits, and a historical Windows fast-hook measurement">
</p>

Claude Code ran live under Covenant on [TinyDB](https://github.com/msiemens/tinydb),
[itsdangerous](https://github.com/pallets/itsdangerous),
[humanize](https://github.com/python-humanize/humanize) and
[prettytable](https://github.com/prettytable/prettytable), each with an honest task and a trap
task. Every honest task committed; every trap was refused or blocked.

The six scripted CovenantBench tasks averaged **1.17 violations per task with the instruction
alone and 0 with Covenant and developer answers**. Both modes completed all six tasks.

A historical replay of **1,446 commits from 50 repositories** flagged 75 commits. Review found
72 rule breaks, two false alarms and one unclear case. Only flagged commits were reviewed,
so the replay cannot estimate recall.

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
| 🚀 | [Getting started](https://github.com/visweshdev/Covenant/blob/main/docs/getting-started.md) | Install, first rules, what happens when one breaks, changing rules, removing Covenant |
| 📏 | [Rules](https://github.com/visweshdev/Covenant/blob/main/docs/rules.md) | All 19 checkers, what the compiler understands, refused commands, rule packs, step rules |
| 📄 | [The rules file](https://github.com/visweshdev/Covenant/blob/main/docs/covenant-yaml.md) | `covenant.yaml` field by field, with every checker's settings |
| 🤖 | [Agents](https://github.com/visweshdev/Covenant/blob/main/docs/agents.md) | Claude Code, Cursor, Codex, any agent, git hooks, the hook server, the sandbox, VS Code |
| ✅ | [CI](https://github.com/visweshdev/Covenant/blob/main/docs/ci.md) | The GitHub Action, `covenant ci` on any CI, how a pull request is judged |
| 🧰 | [Tools](https://github.com/visweshdev/Covenant/blob/main/docs/tools.md) | Dashboard and replay, undo, explain, alerts, suggest, team policies |
| ⌨️ | [Commands](https://github.com/visweshdev/Covenant/blob/main/docs/commands.md) | Every command and flag, exit codes, environment variables, files |
| 🛡️ | [Safety model](https://github.com/visweshdev/Covenant/blob/main/docs/safety.md) | What Covenant protects against, its limits, what isn't built yet |
| 🔎 | [Compatibility](https://github.com/visweshdev/Covenant/blob/main/docs/compatibility.md) | Published wheel and source differences, tested versions, and verification scope |
| ❓ | [FAQ](https://github.com/visweshdev/Covenant/blob/main/docs/faq.md) | Common questions and fixes for common problems |

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
