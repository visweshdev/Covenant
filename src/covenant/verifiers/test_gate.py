"""test_gate: run the declared test command and turn the result into a trace event."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..events import TestRun


def run_tests(cmd: str, cwd: Path, timeout: float | None = None) -> tuple[TestRun, str]:
    """Run `cmd` in `cwd`. Returns the event and the combined output."""
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return TestRun(cmd=cmd, exit_code=result.returncode), result.stdout + result.stderr
