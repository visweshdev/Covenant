<p align="center"><img src="assets/headers/README.svg" width="100%" alt="Covenant docs: everything Covenant does, and how to use it."></p>

<p align="center">
  <a href="../README.md">← Back to the README</a> &nbsp;·&nbsp;
  <a href="getting-started.md"><b>Start here</b></a> &nbsp;·&nbsp;
  <a href="faq.md">FAQ</a>
</p>

<br>

<p align="center"><img src="assets/how-it-works.svg" width="100%" alt="How Covenant works: plain English, compiled to typed rules, enforced while the agent works, at commit and push, and on every pull request"></p>

## Guides

<table>
<tr>
<td width="50%" valign="top">
<img src="assets/icons/start.svg" width="40" align="left" hspace="10" alt="">
<a href="getting-started.md"><b>Getting started</b></a><br>
<sub>Install, write your first rules, see what happens when the agent breaks one, and remove Covenant if you need to.</sub>
</td>
<td width="50%" valign="top">
<img src="assets/icons/rules.svg" width="40" align="left" hspace="10" alt="">
<a href="rules.md"><b>Rules</b></a><br>
<sub>All 19 checkers, the phrases the compiler understands, the 16 kinds of refused command, rule packs and step rules.</sub>
</td>
</tr>
<tr>
<td valign="top">
<img src="assets/icons/file.svg" width="40" align="left" hspace="10" alt="">
<a href="covenant-yaml.md"><b>The rules file</b></a><br>
<sub><code>covenant.yaml</code> field by field: kinds, what happens on a violation, and every checker's settings.</sub>
</td>
<td valign="top">
<img src="assets/icons/agents.svg" width="40" align="left" hspace="10" alt="">
<a href="agents.md"><b>Agents</b></a><br>
<sub>Claude Code hooks, Cursor and Codex over MCP, any agent with <code>record</code>, git hooks, the hook server, Docker and VS Code.</sub>
</td>
</tr>
<tr>
<td valign="top">
<img src="assets/icons/ci.svg" width="40" align="left" hspace="10" alt="">
<a href="ci.md"><b>CI</b></a><br>
<sub>The GitHub Action with line comments, <code>covenant ci</code> on any CI system, and how a pull request is judged.</sub>
</td>
<td valign="top">
<img src="assets/icons/tools.svg" width="40" align="left" hspace="10" alt="">
<a href="tools.md"><b>Tools</b></a><br>
<sub>The dashboard and task replay, undo, explain, Slack/webhook/email alerts, suggest and team policies.</sub>
</td>
</tr>
<tr>
<td valign="top">
<img src="assets/icons/commands.svg" width="40" align="left" hspace="10" alt="">
<a href="commands.md"><b>Commands</b></a><br>
<sub>Every command and flag, exit codes, environment variables, and where Covenant keeps its files.</sub>
</td>
<td valign="top">
<img src="assets/icons/safety.svg" width="40" align="left" hspace="10" alt="">
<a href="safety.md"><b>Safety model</b></a><br>
<sub>What Covenant guarantees, its limits, measured precision, and what isn't built yet.</sub>
</td>
</tr>
<tr>
<td valign="top">
<img src="assets/icons/faq.svg" width="40" align="left" hspace="10" alt="">
<a href="faq.md"><b>FAQ</b></a><br>
<sub>API keys, other agents, privacy, overriding a rule, speed, monorepos, and fixes for common problems.</sub>
</td>
<td valign="top">
<img src="assets/icons/bench.svg" width="40" align="left" hspace="10" alt="">
<a href="../bench/README.md"><b>Benchmark</b></a><br>
<sub>CovenantBench, live trials with Claude Code on 4 repos, and the replay of 1,446 commits from 50 repos.</sub>
</td>
</tr>
</table>

## Common tasks

| I want to… | Read |
|---|---|
| Try Covenant on a repo in two minutes | [Getting started](getting-started.md) |
| Stop an agent from force pushing, deploying or running `DROP TABLE` | [Refused commands](rules.md#refused-commands) |
| Keep an agent inside `src/` | [What the compiler understands](rules.md#what-the-compiler-understands) |
| Make sure tests pass before every commit | [Rules over the agent's steps](rules.md#rules-over-the-agents-steps) |
| Cap a task's time or cost | [Budgets](rules.md#refused-commands) |
| Use Covenant with Cursor or Codex | [MCP clients](agents.md#cursor-codex-and-other-mcp-clients) |
| Check pull requests | [GitHub Action](ci.md#github-action) |
| Undo what an agent did | [Undo](tools.md#undo) |
| Get a Slack message when an agent is stuck | [Alerts](tools.md#alerts) |
| Share rules across a team | [Team policies](tools.md#team-policies) |
| Tune a rule by hand | [Checker settings](covenant-yaml.md#checker-settings) |

## Elsewhere in the repo

| | |
|---|---|
| [VS Code extension](../vscode-extension/README.md) | The editor panel, its commands and settings |
| [Changelog](../CHANGELOG.md) | What changed in each release |
| [Contributing](../CONTRIBUTING.md) | Setup, tests, and how to add a checker |
| [Security](../SECURITY.md) | How to report a way around Covenant |
