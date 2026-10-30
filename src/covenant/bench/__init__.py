"""CovenantBench: does Covenant cut constraint violations without costing task success?

    covenant bench run    --tasks bench/tasks --mode covenant --agent scripted --out results.jsonl
    covenant bench report results.jsonl [more.jsonl ...]

A task (bench/tasks/<id>/task.yaml) is a small repo, an instruction with constraints, a
ground-truth Covenant, traps where the easiest solution breaks a constraint, acceptance
checks for the task itself, and oracle checks that detect violations without using any of
Covenant's own verifiers, so Covenant is never graded by itself.
"""
