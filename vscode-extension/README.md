<p align="center"><img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/assets/headers/vscode.png" width="100%" alt="Covenant for VS Code: see and manage the rules your agents follow, in the editor."></p>

<p align="center">See and manage the rules <a href="https://github.com/visweshdev/Covenant">Covenant</a> enforces on coding agents, right in the editor. <a href="https://github.com/visweshdev/Covenant/blob/main/docs/README.md">Covenant docs</a></p>

---

## Requirements

Use VS Code 1.100 or later and install Covenant into a Python 3.11+ environment that VS Code
can access.

```bash
python -m pip install covenant-agents
python -m covenant --version
```

Set `covenant.pythonPath` to that interpreter if it differs from the editor's default
`python`. If you use pipx, point the setting at the Python executable inside Covenant's
pipx environment.

Open the Git repository as a workspace folder and select **Covenant** in the Activity Bar.
Open an existing `covenant.yaml` or use **Covenant: Compile Rules from Instruction** to draft
one. The **Set Up This Repo** command installs hooks and starts a task. To preserve existing
Git hooks, follow the manual setup in the terminal first.
[Git hook setup](https://github.com/visweshdev/Covenant/blob/main/docs/agents.md#git-hooks)

## Features

| | |
|---|---|
| **Status bar** | Whether every rule holds, how many issues there are, or that tests are needed before the next commit |
| **Rules panel** | The shield in the activity bar: each rule with its scope, the assumptions it was compiled under, its current violations, how often it has blocked the agent this task, and a log of recent blocks |
| **Problems** | Each violation at the place it's about (see below) |
| **Protected files** | A 🔒 badge in the explorer on every file a rule protects |
| **Live updates** | The panel refreshes when an agent's hooks record something, when `covenant.yaml` changes, and when you save |

Problems are placed at:

- the changed function's or type's definition (Python, TypeScript, Go, Rust and Java);
- the protected file;
- the migration line;
- the line with a secret, a skipped test, debug code or a suppressed warning;
- the dependency in `package.json` or `pyproject.toml`;
- or the rule in `covenant.yaml`.

> The editor uses `covenant check --preview` on refresh. That check leaves protected files
> in place and leaves the agent's block count unchanged.

## Commands

| Command | What it does |
|---|---|
| Covenant: Compile Rules from Instruction | Drafts `covenant.yaml` from a plain-English instruction, asks up to three questions, and shows the draft before writing it |
| Covenant: Set Up This Repo (Hooks + Start) | Runs `covenant init` to install Git and Claude Code hooks and start a task |
| Covenant: Add a Rule to the Current Task | `covenant amend`: compiles an extra instruction, shows the new rules, and approves them without resetting the task |
| Covenant: Approve Rules and Start Task | Pins the current `covenant.yaml` and baseline commit and starts a fresh task |
| Covenant: Run Tests | Runs the declared test command so a commit can follow |
| Covenant: Check Now | Re-checks immediately |
| Covenant: Open Rule | Jumps to a rule in `covenant.yaml` (from the panel) |
| Covenant: Show Violation | Opens the place a violation is about (from the panel) |
| Covenant: Show Activity Log | Shows the extension's log of checks and commands |

## Settings

| Setting | Default | What it does |
|---|---|---|
| `covenant.pythonPath` | `python` | A Python with Covenant installed |
| `covenant.checkOnSave` | `true` | Re-check when you save |
| `covenant.offlineCompile` | `false` | Compile without calling Claude |

## Build

From the `vscode-extension` folder with Node.js 22, use the same commands as CI.

```bash
npm ci
npm test
npm run package
code --install-extension covenant-0.1.2.vsix
```

With Covenant installed into the Python on your PATH, run `npm run test:e2e` to download
VS Code 1.100.3 and open a disposable repository in an isolated profile. On headless Linux,
use `xvfb-run -a npm run test:e2e`. Pass an installed executable with
`npm run test:e2e -- "/path/to/Code"` to use the same fixture with a different host.
The suite checks activation, commands, diagnostics, protected-file status and preview behavior.

A built `.vsix` is also attached to each [GitHub release](https://github.com/visweshdev/Covenant/releases).
