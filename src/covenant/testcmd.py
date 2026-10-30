"""Does a shell command run the repo's test suite, in a way whose exit status can be trusted?

Agents rarely type the test command exactly as configured: they add `-x`, `-q`, `-p
no:cacheprovider` or `2>&1`, or write `pytest` for `python -m pytest`. Those runs still test
everything, so they count. A run that selects only some tests (a file path, `-k`, `-m`,
`--lf`) or hides the exit status (a pipe, `||`, background `&`) doesn't count, and the agent
is told why.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

# `|`, `||` and a lone `&` hide the test's exit status; `2>&1`, `&>` and `>&2` are only redirects.
_HIDES_STATUS = re.compile(r"\|\||(?<![>&])\|(?!\|)|(?<![>&<0-9])&(?![>&])")
_REDIRECT = re.compile(r"^(?:\d*>>?|&>>?|<)(?:&\d+)?\S*$|^\d*>&\d+$")

# Spellings of the same runner.
_RUNNERS = [
    (("python", "-m", "pytest"), "pytest"),
    (("python3", "-m", "pytest"), "pytest"),
    (("py", "-m", "pytest"), "pytest"),
    (("pytest",), "pytest"),
    (("py.test",), "pytest"),
]

# Flags that change output, speed or when to stop on failure, but never which tests are selected.
_PYTEST_FLAGS = re.compile(
    r"^(?:-[qvsxl]+|--quiet|--verbose|--exitfirst|--showlocals|--no-header|--no-summary|--strict-markers|"
    r"--tb=\S+|-r\w*|--color=\S+|--code-highlight=\S+|--durations(?:-min)?=\d+(?:\.\d+)?|--maxfail=\d+|"
    r"-p(?:no:\S+|\S+)?|--cov(?:=\S*)?|--cov-report=\S+|--cov-append|--cov-branch|--no-cov|--capture=\S+|"
    r"-n(?:\d+|auto|logical)?|--numprocesses=\S+|--dist=\S+|-W\S*|--disable-warnings|--import-mode=\S+|--timeout=\d+)$"
)
_PYTEST_TAKES_VALUE = {"-p", "-n", "-W", "--tb", "--maxfail", "--durations", "--color", "--capture",
                       "--numprocesses", "--dist", "--cov-report", "--timeout", "--import-mode"}
_GENERIC_FLAGS = re.compile(r"^(?:-v|--verbose|-q|--quiet|--silent|--color(?:=\S+)?|--no-color|-count=\d+|-race|-failfast)$")


@dataclass
class Match:
    cmd: str | None  # the configured test command this run counts as
    why_not: str | None = None  # set when it looks like a test run but doesn't count


def _split(command: str) -> list[str] | None:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def _runner(words: list[str]) -> tuple[str, list[str]]:
    for prefix, name in _RUNNERS:
        if tuple(words[: len(prefix)]) == prefix:
            return name, words[len(prefix):]
    return " ".join(words[:2]), words[2:]  # e.g. "npm test", "go test", "cargo test"


def _essential(runner: str, args: list[str]) -> list[str]:
    """The arguments that decide which tests run, with harmless flags and redirects removed."""
    out, skip = [], False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if re.fullmatch(r"\d*>>?|&>>?|<", a):  # `> out.txt`: the file name is the next word
            skip = True
            continue
        if _REDIRECT.match(a):
            continue
        if runner == "pytest":
            if a in _PYTEST_TAKES_VALUE and i + 1 < len(args):
                skip = True
                continue
            if _PYTEST_FLAGS.match(a):
                continue
        elif _GENERIC_FLAGS.match(a):
            continue
        out.append(a)
    return out


def _segments(command: str) -> list[str]:
    return [s.strip() for s in re.split(r"&&|;|\n", command) if s.strip()]


def match(command: str, test_cmds: list[str]) -> Match:
    configured = []
    for cmd in test_cmds:
        words = _split(cmd)
        if words:
            runner, args = _runner(words)
            configured.append((cmd, runner, _essential(runner, args)))
    if not configured:
        return Match(None)

    near_miss = None
    text = re.sub(r"\s+\d*>&\d+|\s+&>\S+", " ", command)
    hidden = _HIDES_STATUS.search(text)
    if hidden:  # judge the part before the pipe; its status is what gets hidden
        text = text[: hidden.start()]
    for segment in _segments(text):
        words = _split(segment)
        if not words:
            continue
        runner, args = _runner(words)
        for cmd, want_runner, want in configured:
            if runner != want_runner:
                continue
            got = _essential(runner, args)
            if got == want:
                if hidden:
                    return Match(None, f"its exit status is hidden by a pipe, `||` or `&`; run `{cmd}` on its own")
                return Match(cmd)
            extra = [a for a in got if a not in want] or got
            near_miss = Match(None, f"it runs only some tests ({' '.join(extra)}); run the whole suite with `{cmd}`")
    return near_miss or Match(None)
