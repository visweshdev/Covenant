# Compatibility and verification

## Published `covenant-agents` 0.1.2

The PyPI wheel declares Python 3.11 or later. It includes the Anthropic SDK in the default
installation. Published hook installation replaces existing default Git hooks. Use
`covenant init --no-git` and [manual integration](agents.md#git-hooks) in a repo with existing hooks.
[PyPI release and provenance](https://pypi.org/project/covenant-agents/0.1.2/)

On November 22, 2026, clean installations on Windows with CPython 3.13.15 and Git
2.55.0.windows.5 passed the 0.1.1 to 0.1.2 upgrade, blocking and allowing real commits, manual
integration with an existing default-directory hook, and removal.

## Current checkout

| Surface | Versions or environment | Scope |
|---|---|---|
| Python and Git | Verified locally on Windows with CPython 3.13.15 and Debian with CPython 3.11.2. CI uses Python 3.11 and 3.13 on Ubuntu, macOS and Windows. | CLI and verifier suite. A separate Python 3.13 matrix installs the PyPI and candidate wheels and exercises real Git hooks. The candidate wheel passed the Windows smoke test without the Anthropic SDK. |
| Claude Code | Hook protocol fixtures, including Bash and PowerShell. The historical live trial reports Claude Sonnet 5 and did not record the client version. | File and shell preflight, post-action checks and task-end events when the configured client invokes them. |
| MCP | The bundled JSON-RPC server negotiates `2025-06-18`, `2025-03-26` and `2024-11-05`. The Python suite uses protocol fixtures. | Client-initiated tools. Git hooks enforce Git checkpoints. External MCP clients still need version-specific verification. |
| VS Code | Extension declares VS Code `^1.100.0`. The integration suite passed on Linux with VS Code 1.100.3 under Xvfb. | Activation, diagnostics, protected-file status and read-only previews. Unit tests and packaging passed on Windows. The Windows integration run reached the preview before its extension host exited. |
| Docker | Docker Engine 29.9.2 and Compose 5.6.1 on Docker Desktop for Windows, with Claude Code 2.1.270 in the agent image | The three-service smoke test passed. It exercised the mounted state boundary, proxy allow and deny, CONNECT, blocked direct TCP access, Git enforcement and service failure handling. See the [safety model](safety.md#docker-verification) for limits. |

The current checkout installs the Anthropic SDK through the `anthropic` extra. Its Git hook
installer preserves existing hooks and custom `core.hooksPath` settings. On early Python 3.11
builds that lack the standard archive data filter, benchmark and TypeScript Git archives support
regular files and directories. Archives containing links require a newer Python patch release.
See the [CI workflow](../.github/workflows/ci.yml) for the matrix and the
[evaluation results](../bench/README.md) for benchmarks separate from compatibility checks.
