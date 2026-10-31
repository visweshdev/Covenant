"""Sandbox mode: run the agent in a Docker container it can't get out of.

    covenant sandbox                       # Claude Code in the sandbox, interactive
    covenant sandbox -- claude -p "..."    # any command
    covenant sandbox --print               # only write the files and show the commands

Three containers on a private Docker network that has no route to the internet:

    agent   your repo at /work, Claude Code, git. Its only ways out are the two below.
    hooks   `covenant serve`: judges every hook call. The task's state (trace, snapshots,
            approved rules) lives here, in a volume the agent can't see or write.
    proxy   the only door to the internet, and only to allowed hosts (api.anthropic.com plus
            any `allow_hosts` in your rules, plus --allow). Everything else gets 403.

What this adds over plain hooks: an agent that writes a script to dodge a command rule still
can't reach a host that isn't allowed, and can't touch Covenant's state. What it doesn't do:
the hook token is in the agent's settings file, so a determined agent could post made-up hook
events; the server still re-checks commits, pushes and diffs itself, which a made-up event
can't change, but a made-up "tests passed" event would count. Run `covenant test` from
outside the sandbox when that matters.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml

from .ir import ClauseKind, Covenant
from .paths import default_state_dir

DEFAULT_ALLOW = ["api.anthropic.com", "claude.ai", "statsig.anthropic.com"]
HOOK_PORT = 8787
PROXY_PORT = 3128

AGENT_DOCKERFILE = """\
# The agent's image: Claude Code, git, Python and curl (git hooks ask the hook server with curl).
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates python3 python3-pip python3-venv \\
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
RUN useradd -m agent
USER agent
WORKDIR /work
"""

HOOKS_DOCKERFILE = """\
# The hook server's image: Covenant itself, from the copy this sandbox was made with.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY covenant-src /opt/covenant
RUN pip install --no-cache-dir /opt/covenant
RUN git config --system --add safe.directory '*'
WORKDIR /work
"""


def hosts_from_rules(covenant: Covenant) -> list[str]:
    hosts: list[str] = []
    for c in covenant.enforced(ClauseKind.EFFECT_PROHIBITION):
        if c.checker == "command_guard" and "network" in c.params.get("deny", []):
            hosts += c.params.get("allow_hosts", [])
    return hosts


def compose(repo: Path, sandbox_dir: Path, allow: list[str], token: str) -> dict:
    """The docker compose file for the three containers."""
    proxy_cmd = ["python", "/proxy/sandbox_proxy.py", "--port", str(PROXY_PORT)]
    for host in allow:
        proxy_cmd += ["--allow", host]
    proxy_url = f"http://proxy:{PROXY_PORT}"

    def bind(source: Path, target: str, read_only: bool = False) -> dict:
        # Long syntax: a Windows path's drive colon would confuse the short "src:dst" form.
        return {"type": "bind", "source": str(source), "target": target, "read_only": read_only}

    return {
        "name": "covenant-sandbox",
        "services": {
            "hooks": {
                "build": {"context": str(sandbox_dir), "dockerfile": "Dockerfile.hooks"},
                "command": ["covenant", "--repo", "/work", "--state-dir", "/state", "serve",
                            "--bind", "0.0.0.0", "--port", str(HOOK_PORT), "--url", f"http://hooks:{HOOK_PORT}"],
                "volumes": [bind(repo, "/work"), {"type": "volume", "source": "covenant-state", "target": "/state"}],
                "environment": {"COVENANT_HOOK_TOKEN": token, "COVENANT_SNAPSHOTS": "1"},
                "networks": ["sandbox"],
            },
            "proxy": {
                "image": "python:3.12-slim",
                "command": proxy_cmd,
                "volumes": [bind(sandbox_dir / "proxy", "/proxy", True)],
                "networks": ["sandbox", "outside"],
            },
            "agent": {
                "build": {"context": str(sandbox_dir), "dockerfile": "Dockerfile.agent"},
                "depends_on": ["hooks", "proxy"],
                "volumes": [bind(repo, "/work"),
                            bind(sandbox_dir / "claude-settings.json", "/home/agent/.claude/settings.json", True),
                            bind(sandbox_dir / "git-hooks", "/covenant-git-hooks", True)],
                "environment": {
                    "HTTPS_PROXY": proxy_url, "HTTP_PROXY": proxy_url, "https_proxy": proxy_url, "http_proxy": proxy_url,
                    "NO_PROXY": "hooks", "no_proxy": "hooks",
                    # Git inside the sandbox asks the hook server; the repo's own .git/config isn't touched.
                    "GIT_CONFIG_COUNT": "2",
                    "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/covenant-git-hooks",
                    "GIT_CONFIG_KEY_1": "safe.directory", "GIT_CONFIG_VALUE_1": "/work",
                    "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY:-}",
                    "CLAUDE_CODE_OAUTH_TOKEN": "${CLAUDE_CODE_OAUTH_TOKEN:-}",
                    "COVENANT_AGENT": "${COVENANT_AGENT:-}",
                },
                "stdin_open": True, "tty": True,
                "networks": ["sandbox"],
            },
        },
        "networks": {"sandbox": {"internal": True}, "outside": {}},
        "volumes": {"covenant-state": {}},
    }


def prepare(repo: Path, covenant: Covenant, extra_allow: list[str], state_dir: Path | None = None) -> dict:
    """Write everything the sandbox needs into <state>/sandbox/. Returns paths and commands."""
    from . import server

    repo = repo.resolve()
    sandbox_dir = (state_dir or default_state_dir(repo)) / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    allow = list(dict.fromkeys(DEFAULT_ALLOW + hosts_from_rules(covenant) + extra_allow))
    token = server.ensure_token(sandbox_dir)

    (sandbox_dir / "Dockerfile.agent").write_text(AGENT_DOCKERFILE, encoding="utf-8")
    (sandbox_dir / "Dockerfile.hooks").write_text(HOOKS_DOCKERFILE, encoding="utf-8")
    src = Path(__file__).resolve().parent.parent.parent  # the project: pyproject.toml + src/
    target = sandbox_dir / "covenant-src"
    shutil.rmtree(target, ignore_errors=True)
    if (src / "pyproject.toml").is_file():
        shutil.copytree(src, target, ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", "*.vsix",
                                                                   ".venv", "bench", "vscode-extension", "tests"))
    (sandbox_dir / "proxy").mkdir(exist_ok=True)
    shutil.copy2(Path(__file__).with_name("sandbox_proxy.py"), sandbox_dir / "proxy" / "sandbox_proxy.py")
    settings = server.http_hooks_settings(f"http://hooks:{HOOK_PORT}", token)
    (sandbox_dir / "claude-settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    git_hooks = sandbox_dir / "git-hooks"
    git_hooks.mkdir(exist_ok=True)
    for name in ("pre-commit", "pre-push"):
        path = git_hooks / name
        path.write_text(server.git_hook_script(name, f"http://hooks:{HOOK_PORT}", token), encoding="utf-8", newline="\n")
        os.chmod(path, 0o755)
    compose_file = sandbox_dir / "docker-compose.yml"
    compose_file.write_text(yaml.safe_dump(compose(repo, sandbox_dir, allow, token), sort_keys=False), encoding="utf-8")
    return {"dir": sandbox_dir, "compose": compose_file, "allow": allow}


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def commands(compose_file: Path, agent_cmd: list[str]) -> list[list[str]]:
    base = ["docker", "compose", "-f", str(compose_file)]
    return [base + ["up", "-d", "--build", "hooks", "proxy"],
            base + ["run", "--rm", "--build", "agent", *(agent_cmd or ["claude"])],
            base + ["down"]]
