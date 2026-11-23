"""Check the installed CLI and real Git hooks from a clean virtual environment."""

import argparse
import os
import shutil
import subprocess
import tempfile
import venv
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "mylib"


def verify(root: Path, package: str, upgrade_from: str | None, candidate: bool) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(
        COVENANT_HOME=str(root / "state"),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        PYTHONUTF8="1",
    )
    venv.EnvBuilder(with_pip=True).create(root / "venv")
    scripts = root / "venv" / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    python = scripts / f"python{suffix}"
    cli = scripts / f"covenant{suffix}"
    repo = root / "scratch repo"
    shutil.copytree(
        EXAMPLE, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
    )

    def run(*args: str | Path, expected: int = 0) -> str:
        result = subprocess.run(
            args,
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=360,
        )
        output = result.stdout + result.stderr
        if result.returncode != expected:
            raise RuntimeError(
                f"{args} exited {result.returncode}, expected {expected}\n{output}"
            )
        return output

    def git(*args: str, expected: int = 0) -> str:
        return run(
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            *args,
            expected=expected,
        )

    rules = repo / "covenant.yaml"
    rules.write_text(
        rules.read_text(encoding="utf-8").split("    - id: C2", 1)[0], encoding="utf-8"
    )
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "baseline")
    run(
        python,
        "-m",
        "pip",
        "install",
        "--no-compile",
        "--only-binary=:all:",
        upgrade_from or package,
    )
    print(run(cli, "--version").strip(), flush=True)
    run(cli, "compile", "Don't change public APIs", "--offline", "--dry-run")
    run(cli, "init")
    if upgrade_from:
        run(
            python,
            "-m",
            "pip",
            "install",
            "--no-compile",
            "--only-binary=:all:",
            "--upgrade",
            package,
        )
        print(run(cli, "--version").strip(), flush=True)
    run(python, "-m", "pip", "check")
    settings = repo / ".claude" / "settings.json"
    assert settings.is_file()
    core = repo / "mylib" / "core.py"
    original = core.read_text(encoding="utf-8")
    core.write_text(
        original.replace("strict: bool = False) -> Doc:", "strict: bool) -> Doc:"),
        encoding="utf-8",
    )
    git("add", "mylib/core.py")
    assert "C1" in git("commit", "-qm", "changed API", expected=1)
    core.write_text(original, encoding="utf-8")
    (repo / "notes.txt").write_text("allowed change\n", encoding="utf-8")
    git("add", "mylib/core.py", "notes.txt")
    git("commit", "-qm", "add notes")

    hooks = repo / ".git" / "hooks"
    for name in ("pre-commit", "pre-push"):
        hook = hooks / name
        assert b"Installed by covenant" in hook.read_bytes()
        hook.unlink()
    custom = hooks / "pre-commit"
    custom.write_bytes(b"#!/bin/sh\necho existing-hook >&2\n")
    custom.chmod(0o755)

    if candidate:
        run(
            python,
            "-c",
            "import importlib.util; assert importlib.util.find_spec('anthropic') is None",
        )
        assert "existing Git hook" in run(cli, "install-hooks", expected=3)
        assert not (hooks / "pre-push").exists()
        state = root / "sandbox-state"
        run(cli, "--state-dir", state, "sandbox", "--print")
        assert (state / "sandbox" / "covenant" / "__main__.py").is_file()
        assert (state / "sandbox" / "covenant" / "dashboard.html").is_file()
    original_hook = b"#!/bin/sh\necho existing-hook >&2\n"
    assert custom.read_bytes() == original_hook
    env["PATH"] = str(scripts) + os.pathsep + env["PATH"]
    shared_hook = original_hook + b"exec covenant hook pre-commit\n"
    custom.write_bytes(shared_hook)
    run(cli, "init", "--no-git")
    assert custom.read_bytes() == shared_hook
    assert "existing-hook" in git(
        "commit", "--allow-empty", "-qm", "with a shared hook"
    )
    custom.write_bytes(original_hook)
    settings.unlink()
    run(python, "-m", "pip", "uninstall", "-y", "covenant-agents")
    assert not cli.exists()
    assert "existing-hook" in git("commit", "--allow-empty", "-qm", "after removal")
    print(
        "Clean-wheel, Git enforcement, existing-hook compatibility and removal checks passed.",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "package", help="PyPI requirement or path to a locally built wheel"
    )
    parser.add_argument(
        "--upgrade-from",
        help="Install this PyPI wheel before upgrading with hooks in place",
    )
    parser.add_argument(
        "--candidate",
        type=Path,
        help="Also check an unreleased base wheel in a separate environment",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="covenant-wheel-") as temp:
        verify(Path(temp), args.package, args.upgrade_from, candidate=False)
    if args.candidate:
        with tempfile.TemporaryDirectory(prefix="covenant-candidate-") as temp:
            verify(Path(temp), str(args.candidate.resolve()), None, candidate=True)
