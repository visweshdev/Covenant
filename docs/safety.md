<sub>[Covenant](../README.md) › [Docs](README.md) › Safety model</sub>

# 🛡️ Safety model

What Covenant protects against, and what it doesn't.

---

## What it does

- **State lives outside the repo,** in `~/.covenant/workspaces/<repo>-<hash>` (set
  `COVENANT_HOME` to move it). A file lock keeps parallel hooks from corrupting it.
- **The approved rules are pinned** when a session starts. If `covenant.yaml`, the git hooks or
  the Claude settings change, it's reported as an `INTEGRITY` violation, and so is
  `disableAllHooks` or `core.hooksPath`.
- **It fails closed.** A verifier that can't run blocks rather than passes, and a hook that
  crashes before a tool call denies the call.
- **CI uses the base commit's rules,** so a pull request can't weaken them.

## Limits

> [!WARNING]
> Outside the sandbox, Covenant runs as your user. An agent with a shell could still edit its
> state folder directly. That gets detected, but it isn't prevented; [`covenant sandbox`](agents.md#sandbox)
> prevents it.

- **Effects** are found by comparing hashes on every event, not by kernel events.
- **Databases:** SQLite databases are compared row by row. For other databases, Covenant sees the
  SQL the agent runs (commands, `.sql` files, database tools) and migrations, but not queries the
  app makes on its own.
- **Refused commands** guard against mistakes and shortcuts, not an agent determined to hide a
  command in a script. The sandbox's allowlist proxy covers the network side of that.

## Not built yet

- **The sandbox hasn't run in Docker yet.** Its compose file, images and allowlist proxy are
  built and tested (the proxy for real, over sockets), but this project's development machine
  has no Docker, so the three containers haven't been started together.
- **Live database proxy:** row-level rollback of SQL as it runs against Postgres or MySQL,
  including queries the app makes on its own. Destructive SQL the agent runs is already refused,
  and SQLite is guarded row by row.
- **A real-repo CovenantBench.** The harness, metrics and six synthetic tasks exist. The
  proposal's 60–200 tasks from real repos, using commits after the models' training cutoff and
  with annotated ground truth, are curation work.

Found a way around Covenant? Please report it privately; see [SECURITY.md](../SECURITY.md).

---

<sub>Previous: [⌨️ Commands](commands.md) · [Back to the docs](README.md)</sub>
