# Compatibility and verification

Read the row for the build you use. The published wheel and current source both report
version 0.1.2. The install source determines which behavior you get.

| Behavior | PyPI 0.1.2 | Current source |
|---|---|---|
| Install the Anthropic SDK | Included in the base package | Install with `python -m pip install '.[anthropic]'` from the checkout |
| Install Git hooks over an existing hook | Replaces hooks in Git's default directory | Refuses to replace a hook owned by another tool |
| Install when `core.hooksPath` is configured | Use the default hook directory for Covenant's integrity checks | Installer refuses the redirected setup |
| Docker and `serve --start-task` | Earlier sandbox implementation | The hook image uses the installed package, and task approval runs before the agent starts |

See [manual Git hook setup](agents.md#git-hooks) for existing hooks.

## Published `covenant-agents` 0.1.2

The PyPI wheel declares Python 3.11 or later. [PyPI release and provenance](https://pypi.org/project/covenant-agents/0.1.2/)

On November 22, 2026, clean installations on Windows with CPython 3.13.15 and Git
2.55.0.windows.5 passed the 0.1.1 to 0.1.2 upgrade, blocking and allowing real commits, manual
integration with an existing default-directory hook, and removal.

## Current checkout

The November 23, 2026 workflow for `df870f4` passed all 12 jobs, including six Python
matrix jobs, three distribution checks, quality checks, the Docker smoke test and the
Linux VS Code extension job. [GitHub Actions run](https://github.com/visweshdev/Covenant/actions/runs/45180331190)

| Surface | Versions or environment | Scope |
|---|---|---|
| Python and Git | Verified locally on Windows with CPython 3.13.15 and Debian with CPython 3.11.2. CI uses Python 3.11 and 3.13 on Ubuntu, macOS and Windows. | CLI and verifier suite. A separate Python 3.13 matrix installs the PyPI and candidate wheels and exercises real Git hooks. The candidate wheel passed the Windows smoke test without the Anthropic SDK. |
| Claude Code | Hook protocol fixtures, including Bash and PowerShell. The historical live trial reports Claude Sonnet 5 and did not record the client version. | File and shell preflight, post-action checks and task-end events when the configured client invokes them. |
| MCP | The bundled JSON-RPC server negotiates `2025-06-18`, `2025-03-26` and `2024-11-05`. The Python suite uses protocol fixtures. | Client-initiated tools. Git hooks enforce Git checkpoints. External MCP clients still need version-specific verification. |
| VS Code | Extension declares VS Code `^1.100.0`. The integration suite passed on Linux with VS Code 1.100.3 under Xvfb. | Activation, diagnostics, protected-file status and read-only previews. Unit tests and packaging passed on Windows. The Windows integration run reached the preview before its extension host exited. |
| Docker | Docker Engine 29.9.2 and Compose 5.6.1 on Docker Desktop for Windows, with Claude Code 2.1.270 in the agent image | The three-service smoke test passed. It exercised the mounted state boundary, proxy allow and deny, CONNECT, blocked direct TCP access, Git enforcement and service failure handling. See the [safety model](safety.md#docker-verification) for limits. |

On early Python 3.11 builds that lack the standard archive data filter, benchmark and
TypeScript Git archive extraction accepts regular files and directories. Use an updated
Python 3.11 patch release or newer Python if a scanned archive uses symbolic links.
See the [CI workflow](../.github/workflows/ci.yml) for the matrix and the
[evaluation results](../bench/README.md) for benchmarks separate from compatibility checks.
