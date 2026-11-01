<p align="center"><img src="https://raw.githubusercontent.com/visweshdev/Covenant/main/docs/assets/headers/vscode.png" width="100%" alt="Covenant for VS Code: see and manage the rules your agents follow, in the editor."></p>

<p align="center">See and manage the rules <a href="https://github.com/visweshdev/Covenant">Covenant</a> enforces on coding agents, right in the editor. <a href="https://github.com/visweshdev/Covenant/blob/main/docs/README.md">Covenant docs</a></p>

---

## Requirements

The `covenant` command:

```bash
pipx install covenant-agents
```

Or `pip install covenant-agents`, or `pip install -e .` in a Covenant checkout.

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

> **Saving never changes anything.** The editor's check runs `covenant check --preview`, which
> doesn't roll back files and doesn't count toward escalating the agent. Your own edits to
> protected data stay put until the agent's next checked step.

## Commands

| Command | What it does |
|---|---|
| Covenant: Compile Rules from Instruction | Drafts `covenant.yaml` from a plain-English instruction, asks up to three questions, and shows the draft before writing it |
| Covenant: Set Up This Repo | `covenant init`: git hooks, Claude Code hooks, start enforcing |
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

```bash
npm install
npm test                 # unit tests (no VS Code needed)
npm run test:e2e -- "<path to Code executable>" <repo>   # opens VS Code on a repo and checks what it shows
npm run package          # builds covenant-<version>.vsix
code --install-extension covenant-0.1.1.vsix
```

A built `.vsix` is also attached to each [GitHub release](https://github.com/visweshdev/Covenant/releases).
