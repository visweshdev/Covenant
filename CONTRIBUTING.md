# Contributing

Thanks for helping. Bug reports, new checkers, benchmark tasks and fixes are all welcome.

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

## Before opening a pull request

- Add or update tests for what you changed. Every checker is tested against real files and
  git history, not mocks.
- Run the whole Python suite and, if you touched `vscode-extension/`, `npm test`.
- If you changed enforcement, run the benchmark and make sure no mode got worse:
  `covenant bench run --mode prompt --mode covenant --mode groundtruth`.
- Keep the README in step with what the code does.

## Adding a checker

1. Write it in `src/covenant/verifiers/` and register it in `verifiers/registry.py`.
2. Teach the binder (`compiler/bind.py`) which instructions map to it.
3. Add tests, and ideally a benchmark task in `bench/tasks/` whose naive plan breaks the rule.

## Reporting security problems

Please don't open a public issue. See [SECURITY.md](SECURITY.md).
