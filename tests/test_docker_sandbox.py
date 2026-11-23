"""Opt-in Compose integration test using a scratch repo and an isolated test origin."""

import os
import subprocess

import pytest
import yaml

from covenant import policy, sandbox

pytestmark = pytest.mark.skipif(
    os.environ.get("COVENANT_TEST_DOCKER") != "1",
    reason="set COVENANT_TEST_DOCKER=1 to run Compose",
)


def test_sandbox_in_docker(mylib_repo, tmp_path):
    rules_path = mylib_repo / "covenant.yaml"
    rules = policy.load(rules_path)[0]
    rules.clauses = [clause for clause in rules.clauses if clause.id == "C1"]
    rules.dump(rules_path)
    # The test fixture belongs to the host runner. Permit edits by the unprivileged agent.
    if os.name == "posix" and os.getuid() == 0:
        for path in [mylib_repo, *mylib_repo.rglob("*")]:
            path.chmod(path.stat().st_mode | (0o777 if path.is_dir() else 0o666))

    plan = sandbox.prepare(mylib_repo, rules, ["allowed.test"], tmp_path / "state")
    spec = yaml.safe_load(plan["compose"].read_text(encoding="utf-8"))
    spec["services"]["origin"] = {
        "image": spec["services"]["proxy"]["image"],
        "command": ["python", "-m", "http.server", "8000", "--directory", "/tmp"],
        "networks": {"outside": {"aliases": ["allowed.test", "denied.test"]}},
        "healthcheck": {
            "test": [
                "CMD",
                "python",
                "-c",
                "import socket; socket.create_connection(('127.0.0.1', 8000)).close()",
            ],
            "interval": "2s",
            "timeout": "3s",
            "retries": 15,
        },
    }
    plan["compose"].write_text(yaml.safe_dump(spec), encoding="utf-8")
    base = ["docker", "compose", "-f", str(plan["compose"])]
    env = {**os.environ, "ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": ""}

    def docker(*args, check=True, timeout=90):
        result = subprocess.run(
            [*base, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def agent(*args, check=True):
        return docker("run", "-T", "--rm", "--no-deps", "agent", *args, check=check)

    try:
        docker("build", "agent", "hooks", timeout=900)
        docker(
            "up",
            "--wait",
            "--wait-timeout",
            "60",
            "hooks",
            "proxy",
            "origin",
            timeout=600,
        )
        docker("exec", "-T", "hooks", "test", "-f", "/state/session.json")
        agent(
            "sh",
            "-ec",
            "test $(id -u) -ne 0; test ! -e /state; test ! -S /var/run/docker.sock; "
            "test -r /home/agent/.claude/settings.json; "
            "test ! -w /home/agent/.claude/settings.json; test ! -w /covenant-git-hooks/pre-commit; "
            "curl -fsS --max-time 5 http://hooks:8787/health",
        )
        assert "2.1.270 (Claude Code)" in agent("claude", "--version").stdout
        assert agent(
            "curl", "-fsS", "--max-time", "5", "http://allowed.test:8000/"
        ).stdout
        denied = agent(
            "curl",
            "-sS",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--max-time",
            "5",
            "http://denied.test:8000/",
        )
        assert denied.stdout == "403"
        tunnel = agent(
            "curl",
            "-fsS",
            "--proxytunnel",
            "--max-time",
            "5",
            "http://allowed.test:8000/",
        )
        assert "Directory listing" in tunnel.stdout
        origin = docker("ps", "-q", "origin").stdout.strip()
        address = subprocess.check_output(
            [
                "docker",
                "inspect",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                origin,
            ],
            text=True,
            env=env,
            timeout=10,
        ).strip()
        assert address
        assert agent(
            "curl",
            "--noproxy",
            "*",
            "--max-time",
            "3",
            f"http://{address}:8000/",
            check=False,
        ).returncode

        core = mylib_repo / "mylib" / "core.py"
        source = core.read_text(encoding="utf-8")
        core.write_text(
            source.replace("strict: bool = False) -> Doc:", "strict: bool) -> Doc:"),
            encoding="utf-8",
        )
        agent("git", "add", "mylib/core.py")
        blocked = agent("git", "hook", "run", "pre-commit", check=False)
        assert blocked.returncode and "C1" in blocked.stderr, blocked.stderr
        core.write_text(source, encoding="utf-8")
        (mylib_repo / "notes.txt").write_text("legitimate change\n", encoding="utf-8")
        agent("git", "add", "mylib/core.py", "notes.txt")
        agent(
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "add notes",
        )

        docker("stop", "origin")
        unavailable = agent(
            "curl",
            "-sS",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--max-time",
            "15",
            "http://allowed.test:8000/",
        )
        assert unavailable.stdout == "502"
        docker("stop", "hooks")
        failed = agent("git", "hook", "run", "pre-commit", check=False)
        assert failed.returncode and "server unreachable" in failed.stderr, (
            failed.stderr
        )
    finally:
        docker("down", "--volumes", "--remove-orphans", check=False)
