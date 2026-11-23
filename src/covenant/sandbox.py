"""Sandbox mode: run the agent with a restricted Docker network and separate state.

    covenant sandbox                       # Claude Code in the sandbox, interactive
    covenant sandbox -- claude -p "..."    # any command
    covenant sandbox --print               # only write the files and show the commands

Three containers share an internal Docker network:

    agent   your repo at /work, Claude Code, and git. It reaches the services below.
    hooks   `covenant serve`: judges every hook call. The task's state (trace, snapshots,
            approved rules) lives here, in a volume the agent can't see or write.
    proxy   connects to the outside network and forwards to allowed hosts (Anthropic services,
            any `allow_hosts` in your rules, and --allow). Other proxy requests get 403.

What this adds over plain hooks: an agent that writes a script to dodge a command rule still
can't reach a host that isn't allowed, and can't touch Covenant's state. What it doesn't do:
the hook token is in the agent's settings file, so a determined agent could post made-up hook
events; the server still re-checks commits, pushes and diffs itself, which a made-up event
can't change, but a made-up "tests passed" event would count. Run `covenant test` from
outside the sandbox when that matters.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from importlib.metadata import requires
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
RUN npm install -g --no-audit --no-fund --fetch-retries=1 --fetch-timeout=180000 @anthropic-ai/claude-code@2.1.270
ARG AGENT_UID=1000
ARG AGENT_GID=1000
RUN userdel node \
    && (getent group "$AGENT_GID" >/dev/null || groupadd -g "$AGENT_GID" agent) \
    && useradd -m -u "$AGENT_UID" -g "$AGENT_GID" agent
ENV HOME=/home/agent
USER agent
WORKDIR /work
"""

HOOKS_DOCKERFILE = """\
# The hook server uses the same Covenant package as the host.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /opt/covenant/requirements.txt
RUN pip install --no-cache-dir -r /opt/covenant/requirements.txt
COPY covenant /opt/covenant/covenant
ENV PYTHONPATH=/opt/covenant
RUN git config --system --add safe.directory '*'
WORKDIR /work
"""


def hosts_from_rules(covenant: Covenant) -> list[str]:
    hosts: list[str] = []
    for c in covenant.enforced(ClauseKind.EFFECT_PROHIBITION):
        if c.checker == "command_guard" and "network" in c.params.get("deny", []):
            hosts += c.params.get("allow_hosts", [])
    return hosts


def compose(repo: Path, sandbox_dir: Path, allow: list[str], token: str, policy_file: str = "covenant.yaml") -> dict:
    """The docker compose file for the three containers."""
    proxy_cmd = ["python", "/proxy/sandbox_proxy.py", "--port", str(PROXY_PORT)]
    for host in allow:
        proxy_cmd += ["--allow", host]
    proxy_url = f"http://proxy:{PROXY_PORT}"
    project_id = hashlib.sha256(str(sandbox_dir.resolve()).encode()).hexdigest()[:16]
    agent_ids = {}
    if os.name == "posix" and os.getuid() != 0:
        agent_ids = {"AGENT_UID": str(os.getuid()), "AGENT_GID": str(os.getgid())}

    def bind(source: Path, target: str, read_only: bool = False) -> dict:
        # Long syntax: a Windows path's drive colon would confuse the short "src:dst" form.
        return {"type": "bind", "source": str(source), "target": target, "read_only": read_only}

    return {
        "name": f"covenant-sandbox-{project_id}",
        "services": {
            "hooks": {
                "build": {"context": str(sandbox_dir), "dockerfile": "Dockerfile.hooks"},
                "command": ["python", "-m", "covenant", "--repo", "/work", "--state-dir", "/state",
                            "--covenant", f"/work/{policy_file}", "serve", "--start-task",
                            "--bind", "0.0.0.0", "--port", str(HOOK_PORT), "--url", f"http://hooks:{HOOK_PORT}"],
                "volumes": [bind(repo, "/work"), {"type": "volume", "source": "covenant-state", "target": "/state"}],
                "environment": {"COVENANT_HOOK_TOKEN": token, "COVENANT_SNAPSHOTS": "1"},
                "healthcheck": {
                    "test": ["CMD", "python", "-c", f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{HOOK_PORT}/health', timeout=2).close()"],
                    "interval": "2s", "timeout": "3s", "retries": 20,
                },
                "networks": ["sandbox"],
            },
            "proxy": {
                "image": "python:3.12-slim",
                "command": proxy_cmd,
                "volumes": [bind(sandbox_dir / "proxy", "/proxy", True)],
                "healthcheck": {
                    "test": ["CMD", "python", "-c", f"import socket; socket.create_connection(('127.0.0.1', {PROXY_PORT}), timeout=2).close()"],
                    "interval": "2s", "timeout": "3s", "retries": 20,
                },
                "networks": ["sandbox", "outside"],
            },
            "agent": {
                "build": {"context": str(sandbox_dir), "dockerfile": "Dockerfile.agent", "args": agent_ids},
                "depends_on": {name: {"condition": "service_healthy"} for name in ("hooks", "proxy")},
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
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


def prepare(repo: Path, covenant: Covenant, extra_allow: list[str], state_dir: Path | None = None, *,
            covenant_path: Path | None = None) -> dict:
    """Write everything the sandbox needs into <state>/sandbox/. Returns paths and commands."""
    from . import server

    repo = repo.resolve()
    policy_file = (covenant_path or repo / "covenant.yaml").resolve()
    if not policy_file.is_relative_to(repo):
        raise ValueError("the sandbox Covenant file must be in the mounted repository")
    sandbox_dir = (state_dir or default_state_dir(repo)) / "sandbox"
    if sandbox_dir.resolve().is_relative_to(repo):
        raise ValueError("sandbox state must be outside the mounted repository")
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    allow = list(dict.fromkeys(DEFAULT_ALLOW + hosts_from_rules(covenant) + extra_allow))
    token = server.ensure_token(sandbox_dir)

    (sandbox_dir / "Dockerfile.agent").write_text(AGENT_DOCKERFILE, encoding="utf-8")
    (sandbox_dir / "Dockerfile.hooks").write_text(HOOKS_DOCKERFILE, encoding="utf-8")
    (sandbox_dir / ".dockerignore").write_text("*\n!covenant/\n!covenant/**\n!requirements.txt\n", encoding="utf-8")
    package = Path(__file__).resolve().parent
    target = sandbox_dir / "covenant"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(package, target, ignore=shutil.ignore_patterns("__pycache__"))
    project = package.parent.parent / "pyproject.toml"
    if package.parent.name == "src" and project.is_file():
        with project.open("rb") as f:
            dependencies = tomllib.load(f)["project"]["dependencies"]
    else:
        dependencies = requires("covenant-agents") or []
    (sandbox_dir / "requirements.txt").write_text("\n".join(dependencies) + "\n", encoding="utf-8")
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
    config = compose(repo, sandbox_dir, allow, token, policy_file.relative_to(repo).as_posix())
    compose_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
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
    return [base + ["up", "--wait", "--wait-timeout", "60", "--build", "hooks", "proxy"],
            base + ["run", "--rm", "--no-deps", "--build", "agent", *(agent_cmd or ["claude"])],
            base + ["down"]]
