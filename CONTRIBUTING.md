<p align="center"><img src="docs/logo.png" width="72" alt="Covenant logo"></p>

<h1 align="center">Contributing</h1>

<p align="center">Bug reports, new checkers, benchmark tasks and fixes are all welcome. Thanks for helping.</p>

---

## Set up

```bash
pip install -e ".[dev]"
npm install --prefix /tmp/ts typescript@5.6.3   # for the TypeScript checker tests
COVENANT_TYPESCRIPT=/tmp/ts/node_modules/typescript python -m pytest -q
```

The VS Code extension has its own tests:

```bash
cd vscode-extension
npm install
npm test
```

## Where things are

| Path | What's there |
|---|---|
| `src/covenant/ir.py` | The rule format |
| `src/covenant/compiler/` | Instruction → rules: extract, ground, bind, ask |
| `src/covenant/runtime.py`, `monitors/`, `verifiers/` | Enforcement and the checkers |
| `src/covenant/integrations/` | Claude Code hooks and the MCP server |
| `src/covenant/policy.py` | Team policies |
| `src/covenant/ci.py`, `github.py` | `covenant ci` and PR comments |
| `src/covenant/server.py`, `dashboard.py`, `cli.py` | Hook server, dashboard, commands |
| `src/covenant/bench/`, `bench/tasks/` | CovenantBench and its tasks |
| `examples/mylib` | The worked example |
| `vscode-extension/` | The VS Code extension |

## Before opening a pull request

- [ ] Add or update tests for what you changed. Every checker is tested against real files and
      git history, not mocks.
- [ ] Run the whole Python suite and, if you touched `vscode-extension/`, `npm test`.
- [ ] If you changed enforcement, run the benchmark and make sure no mode got worse:
      `covenant bench run --mode prompt --mode covenant --mode groundtruth`.
- [ ] Keep the README and [docs](docs/README.md) in step with what the code does.

## Adding a checker

1. Write it in `src/covenant/verifiers/` and register it in `verifiers/registry.py`.
2. Teach the binder (`compiler/bind.py`) which instructions map to it.
3. Add tests, and ideally a benchmark task in `bench/tasks/` whose naive plan breaks the rule.
4. Add it to the tables in [docs/rules.md](docs/rules.md).

## Reporting security problems

Please don't open a public issue. See [SECURITY.md](SECURITY.md).
