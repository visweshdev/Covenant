<p align="center"><img src="assets/headers/safety.svg" width="100%" alt="Safety model: what Covenant protects against, and where it stops."></p>

<p align="center"><sub><a href="../README.md">Covenant</a> › <a href="README.md">Docs</a> › Safety model</sub></p>

Covenant is designed to catch mistakes and shortcuts by cooperative coding agents. Its
coverage depends on the rules you approve and the integration that delivers each event.

**On this page:** [What it does](#what-it-does) · [Limits](#limits) · [Docker verification](#docker-verification) · [Not built yet](#not-built-yet)

---

## What it does

| | |
|---|---|
| 🧱 **Fails closed at covered checkpoints** | A Claude Code preflight hook error denies the tool call. Git hooks pointed at the hook server fail if it is unreachable. Deterministic verifier errors are reported using the rule's violation policy |
| 📌 **Pins the rules** | Starting a task approves the rules and records hashes for selected files. Later checkpoints report changes to pinned inputs and detect `disableAllHooks` or `core.hooksPath` |
| 🚫 **Guards its own files** | Claude Code preflight checks refuse covered edits to rules, hooks and state, and recognized hook-bypass commands |
| 🗂️ **Keeps state outside the repo** | In `~/.covenant/workspaces/<repo>-<hash>` (move it with `COVENANT_HOME`); a file lock keeps parallel hooks from corrupting it |
| 🧑‍⚖️ **Hands over** | After 5 blocks of the same rule, or on a rule set to `ask_human`, the agent is stopped and the task is yours |
| 🔁 **Checks against base rules** | CI uses the rules at the merge base when present and reports changes to them. [CI behavior](ci.md#how-a-pull-request-is-judged) |
| 🌐 **Local service defaults** | The dashboard and daemon bind to loopback. The HTTP hook server binds to loopback by default and requires a bearer token for hook requests |

## Limits

> [!WARNING]
> Local hooks run as your user. They can detect changes to pinned controls at a later checkpoint,
> but an agent with the same filesystem permissions can change local state or interfere with hook
> execution. [`covenant sandbox`](agents.md#sandbox) gives the hook service its own state volume and
> restricts the agent's network. Treat this as defense in depth for a cooperative coding agent.

- **Side effects** are found by comparing snapshots at supported checkpoints.
- **Databases:** SQLite databases are compared row by row. For other databases, Covenant sees the
  SQL the agent runs (commands, `.sql` files, database tools) and migrations, but not queries the
  app makes on its own.
- **Refused commands** recognize documented patterns in tool events. Indirect execution is outside
  the command guard's coverage. Outcome checks can detect changes to protected resources at a later
  checkpoint. Proxy decisions cover requests routed through the proxy.
- **API checks** read source, not compiled artifacts. Go, Rust and Java are read without a
  compiler: a careful approximation. TypeScript uses the TypeScript compiler, and very long types
  are compared by their declaration text.
- **Git hooks** enforce the checkpoints Git invokes. The current installer refuses to
  overwrite another tool's hooks. [Manual Git hook integration](agents.md#git-hooks)
- **Test events** depend on the integration reporting them. The sandbox agent can read the
  hook token from its settings. Use an independent test job when test provenance matters.
- **Replay precision:** a historical review of [1,446 commits across 50 repositories](../bench/README.md#replay-on-50-real-repositories)
  found 72 rule breaks, two false alarms and one unclear case among the 75 flags. The unflagged
  commits were unreviewed, so replay recall is unknown.

## Docker verification

The Compose smoke test builds the generated agent and hook images, starts the proxy and hook
service together, and runs the agent against a disposable Git repository. It checks the state mount
boundary, read-only configuration, allowed and denied proxy requests, HTTPS CONNECT, direct
TCP isolation from the test origin, a blocked Git change, a legitimate commit, an HTTP 502
from a failed upstream connection, and Git hook failure when the hook server stops.

Run `COVENANT_TEST_DOCKER=1 python -m pytest -q tests/test_docker_sandbox.py` with Docker Compose v2 or newer
and access to the image and package registries. See the [compatibility matrix](compatibility.md)
for verification status. Container escape resistance, DNS isolation and the behavior of allowed
remote services remain outside the smoke test's scope.

## Not built yet

- **Live database proxy:** row-level rollback of SQL as it runs against Postgres or MySQL,
  including queries the app makes on its own. Destructive SQL the agent runs is already refused,
  and SQLite is guarded row by row.
- **A real-repo CovenantBench with agents:** the replay covers 50 repositories' history, and live
  trials covered 4 with Claude Code; the proposal's 60–200 annotated agent tasks are curation work.
- **Monorepo-wide API checks:** a monorepo's main package is found, but other packages in it
  need `entries` set by hand.

Found a way around Covenant? Please report it privately; see [SECURITY.md](../SECURITY.md).

---

<table width="100%"><tr>
<td align="left"><a href="commands.md">← Commands</a></td>
<td align="right"><a href="faq.md">FAQ →</a></td>
</tr></table>
