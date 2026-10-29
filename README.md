# Covenant

Compiles a developer's plain-English instruction to a coding agent into a typed
contract, then enforces it with real checks on diffs, side effects and event order.

This is the **runtime first** milestone: Covenant files are written by hand
(see [examples/mylib/covenant.yaml](examples/mylib/covenant.yaml)); the compiler comes next.

## Layout

| Path | What it is |
|---|---|
| `src/covenant/ir.py` | The rule format: `Covenant`, `Clause`, the three clause kinds (Pydantic) |
| `src/covenant/events.py` | Typed events: edit, run, test, commit, push, effect |
| `src/covenant/monitors/trace.py` | Past-time LTL parser and step-by-step monitor (`TemporalObligation`) |
| `src/covenant/monitors/effects.py` | Matches `(operation, target)` effects against protected paths and tables (`EffectProhibition`) |
| `src/covenant/monitors/snapshot.py` | Copies protected files aside, detects deletes/overwrites, restores them |
| `src/covenant/verifiers/api_surface.py` | `api_surface_diff`: exported names and signatures vs. a pinned baseline commit |
| `src/covenant/verifiers/test_gate.py` | Runs the test command and turns the result into an event |
| `src/covenant/verifiers/registry.py` | Which checkers exist and which clause kind each enforces |
| `src/covenant/validate.py` | Static checks: checker binding, params, formulas, vacuous scopes |
| `src/covenant/runtime.py` | Judges each event, gates commit/push, logs reports, escalates repeats |
| `src/covenant/cli.py` | `covenant` command and git hooks |

## Try it

```bash
python -m pytest -q
```

Using the CLI on a git repo that has a `covenant.yaml` (run with `PYTHONPATH=src` or after `pip install -e .`):

```bash
covenant --repo path/to/repo validate
covenant --repo path/to/repo install-hooks
covenant --repo path/to/repo start
covenant --repo path/to/repo record edit mylib/core.py
covenant --repo path/to/repo test
covenant --repo path/to/repo check
```

Exit codes: 0 clean, 1 violations (the agent should revise), 2 needs a human, 3 bad setup.

## Not built yet

- The compiler (extract, classify, ground, validate, emit) and clarification questions.
- The real sandbox: overlayfs/btrfs snapshots and fanotify/eBPF events. For now, protected
  files are copied aside and checked by hash on every event and checkpoint.
- A database proxy: database effects must be recorded by hand, and they can't be rolled back.
- griffe for API extraction (Python's `ast` module is used for now), the TypeScript API differ,
  a CLI-flag checker, `schema_guard` and `dependency_diff`.
- Keeping state out of the agent's reach: pass `--state-dir` somewhere the agent can't write.
- The pre-commit hook records the commit event before git finishes; a commit that fails
  later for another reason still appears in the trace.
