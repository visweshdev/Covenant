import shutil
import subprocess
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "mylib"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=Covenant Test", "-c", "user.email=test@example.com", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def mylib_repo(tmp_path: Path) -> Path:
    """A fresh git copy of examples/mylib with one baseline commit."""
    repo = tmp_path / "mylib"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".covenant"))
    for args in (("init", "-q"), ("add", "."), ("commit", "-q", "-m", "baseline")):
        assert git(repo, *args).returncode == 0
    return repo
