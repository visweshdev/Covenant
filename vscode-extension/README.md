# Covenant for VS Code

See and manage the rules Covenant enforces on coding agents, right in the editor. Needs the
`covenant` Python package (`pip install -e .` in the Covenant checkout).

- **Status bar:** whether every rule holds, how many issues there are, or that tests are needed before the next commit.
- **Rules panel** (the shield in the activity bar):
  - each rule with its scope, the assumptions it was compiled under, its current violations, and how often it has blocked the agent this task;
  - a log of recent blocks.
- **Problems:** each violation appears at the place it's about:
  - the changed function's definition;
  - the protected file;
  - the migration line;
  - the dependency in `package.json` or `pyproject.toml`;
  - or the rule in `covenant.yaml`.
- **Protected files:** a 🔒 badge in the explorer on every file a rule protects.
- **Live updates:** the panel refreshes when an agent's hooks record something, when `covenant.yaml` changes, and when you save.

Saving never changes anything. The editor's check runs `covenant check --preview`, which doesn't roll back files and doesn't count toward escalating the agent. So your own edits to protected data stay put until the agent's next checked step.

## Commands

| Command | What it does |
|---|---|
| Covenant: Compile Rules from Instruction | Drafts `covenant.yaml` from a plain-English instruction, asks up to three questions, and shows the draft before writing it |
| Covenant: Set Up This Repo | `covenant init`: git hooks, Claude Code hooks, start enforcing |
| Covenant: Add a Rule to the Current Task | `covenant amend`: compiles an extra instruction, shows the new rules, and approves them without resetting the task |
| Covenant: Approve Rules and Start Task | Pins the current `covenant.yaml` and baseline commit and starts a fresh task |
| Covenant: Run Tests | Runs the declared test command so a commit can follow |
| Covenant: Check Now | Re-checks immediately |

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
npm run package          # builds covenant-0.1.0.vsix
code --install-extension covenant-0.1.0.vsix
```
