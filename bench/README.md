# CovenantBench

Measures whether Covenant cuts constraint violations without costing task success.

Each task in `tasks/<id>/` is a small repo plus a `task.yaml` with:
- an instruction containing constraints;
- a **ground-truth Covenant**;
- **traps**: places where the easiest solution breaks a constraint;
- **acceptance checks**: was the task itself done?
- **oracle checks**: were the constraints kept? These look only at the final files and git history, with their own logic, so Covenant never grades itself;
- for ambiguous tasks, the question that should be asked and the developer's answer.

## Run

```bash
covenant bench run --mode prompt --mode covenant-unattended --mode covenant --mode groundtruth
covenant bench report bench-results.jsonl
```

| Mode | What it measures |
|---|---|
| `prompt` | The instruction alone, with no Covenant (the baseline) |
| `covenant-unattended` | Compiled Covenant, with every ambiguity resolved to the conservative default |
| `covenant` | Compiled Covenant, with a simulated developer answering the clarification questions |
| `groundtruth` | The hand-written Covenant: enforcement without compilation error (the upper bound) |

Agents:
- **`--agent scripted`** (the default) replays each task's "naive" plan, the one that falls into the traps, through Claude Code's hook protocol. When Covenant objects, it resets and follows the "careful" plan. It's deterministic and free, so use it to test the harness and the enforcement path.
- **`--agent command --agent-cmd "..."`** runs any real agent in the task repo, with `{instruction}` filled in. For example:

  ```bash
  covenant bench run --mode prompt --mode covenant --agent command \
    --agent-cmd "claude -p {instruction} --permission-mode acceptEdits --allowedTools Read Edit Write Bash"
  ```

  Real agents cost money per task.

Add `--llm` to compile with Claude instead of the offline extractor.

## Metrics

| Metric | Computed as |
|---|---|
| `violations_per_task` | Failed oracle checks per task |
| `task_success` | Share of tasks whose acceptance checks pass |
| `compliant_success` | Share of tasks that succeed with no violation |
| `over_restriction` | Tasks that failed with no violation after Covenant blocked something |
| `feedback_recovery` | Share of blocked tasks that still ended in success |
| `fidelity_precision` / `fidelity_recall` | Compiled clauses against the ground truth (same checker, listed params match) |
| `asked_the_right_question` | Ambiguous tasks where a clarification question matched the expected one |
| `seconds_per_task` | Added wall-clock time |

## Results with the scripted agent (6 tasks)

| Mode | Violations per task | Task success | Compliant success | Wrongly blocked tasks | Fidelity (precision/recall) |
|---|---|---|---|---|---|
| prompt | 1.17 | 1.0 | 0.0 | 0 | — |
| covenant-unattended | 0.0 | 0.83 | 0.83 | 1 | 0.92 / 0.92 |
| covenant | 0.0 | 1.0 | 1.0 | 0 | 1.0 / 1.0 |
| groundtruth | 0.0 | 1.0 | 1.0 | 0 | 1.0 / 1.0 |

The single over-restriction is the ambiguous cache task. The compiler asked the right question, but with nobody answering it kept the safe reading, which protected the cache. Answering that one question closes the gap to the ground truth.

The SQLite task shows why database rules are checked row by row. Without Covenant, the agent
resets the users table before inserting. With Covenant, the lost rows are put back after that
step and the agent's own insert is kept. Protecting the database as a whole file would have
rolled back the insert too.

## Adding tasks

The proposal targets about 200 tasks from real open-source repos, using commits after the models' training cutoff, with each ground-truth Covenant written and cross-checked by two annotators. These six synthetic tasks exercise the format and every check type. Real-repo tasks need that curation work.
