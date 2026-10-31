"""Network allowlists: which hosts a command or a web tool call would reach.

`command_guard` with `deny: [network]` refuses network access. Add `allow_hosts` and only the
hosts not on the list are refused:

    params: {deny: [network], allow_hosts: [pypi.org, github.com]}

A host matches itself and its subdomains (github.com covers api.github.com). Package managers
reach their registry even when no URL is written, so `pip install x` needs pypi.org.
A network command whose destination can't be worked out is refused, since it can't be checked.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

NETWORK_TOOLS = re.compile(
    r"\b(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod|ssh|scp|sftp|rsync|nc|ncat|telnet|ftp|http|httpie|aria2c)\b",
    re.I,
)
_URL = re.compile(r"\b(?:https?|ftp|wss?|git|ssh)://[^\s'\"<>|;)]+", re.I)
_SCP_LIKE = re.compile(r"\b[\w.-]+@([\w.-]+\.[a-z]{2,})(?::|\s|$)", re.I)  # git@github.com:org/repo
_BARE_HOST = re.compile(r"(?<![\w./-])((?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|ai|co|app|cloud|sh|gov|edu|me|xyz|info|so|us|uk|de|in))\b(?![\w-])", re.I)
REGISTRIES: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(r"\b(?:pip3?|uv\s+pip|pipx|poetry|uv)\s+(?:install|add|sync|lock|download)\b", re.I), ["pypi.org", "files.pythonhosted.org"]),
    (re.compile(r"\b(?:npm|pnpm|yarn|npx|bun)\s+(?:install|i|add|ci|update|exec|dlx)\b|\bnpx\s", re.I), ["registry.npmjs.org"]),
    (re.compile(r"\bcargo\s+(?:install|add|build|update|fetch)\b", re.I), ["crates.io", "static.crates.io", "index.crates.io"]),
    (re.compile(r"\bgo\s+(?:get|install|mod\s+download)\b", re.I), ["proxy.golang.org", "sum.golang.org"]),
    (re.compile(r"\bgem\s+install\b|\bbundle\s+install\b", re.I), ["rubygems.org"]),
    (re.compile(r"\bdocker\s+(?:pull|push|login)\b", re.I), ["registry-1.docker.io"]),
]
REGISTRY_USE = re.compile("|".join(rx.pattern for rx, _ in REGISTRIES), re.I)
GIT_REMOTE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(clone|fetch|pull|push|ls-remote)\b([^;&|\n]*)", re.I)


def _remote_host(repo: Path | None, args: str) -> str | None:
    """The host of the git remote a fetch, pull or push talks to (origin unless named)."""
    if repo is None:
        return None
    words = [w for w in args.split() if not w.startswith("-")]
    remote = words[0] if words else "origin"
    try:
        url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", remote],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not url:
        return None
    return urlparse(url).hostname or (_SCP_LIKE.findall(url + " ") or [None])[0]


def host_allowed(host: str, allowed: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowed:
        entry = entry.lower().lstrip("*.").rstrip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return False


def hosts_in_command(command: str, repo: Path | None = None) -> list[str]:
    hosts = [urlparse(u).hostname or "" for u in _URL.findall(command)]
    hosts += _SCP_LIKE.findall(command)
    for rx, registry in REGISTRIES:
        if rx.search(command) and not re.search(r"--(?:index-url|registry)\b", command):
            hosts += registry
    if NETWORK_TOOLS.search(command) and not hosts:
        hosts += _BARE_HOST.findall(command)  # `ssh example.com`, `curl example.com/x`
    for verb, args in GIT_REMOTE.findall(command):
        if verb.lower() != "clone" and not _URL.search(args) and not _SCP_LIKE.search(args + " "):
            hosts.append(_remote_host(repo, args) or "")
    return [h.lower() for h in dict.fromkeys(hosts) if h]


def uses_network(command: str) -> bool:
    return bool(NETWORK_TOOLS.search(command) or _URL.search(command) or REGISTRY_USE.search(command)
                or GIT_REMOTE.search(command))


def blocked_hosts(command: str, allowed: list[str], repo: Path | None = None) -> list[str] | None:
    """Hosts the command would reach that aren't allowed; None when it uses the network but
    the destination can't be worked out (refused, since it can't be checked)."""
    if not uses_network(command):
        return []
    hosts = hosts_in_command(command, repo)
    if not hosts:
        return None
    if GIT_REMOTE.search(command) and any(v.lower() != "clone" for v, _ in GIT_REMOTE.findall(command)):
        unknown = [a for v, a in GIT_REMOTE.findall(command) if v.lower() != "clone"
                   and not _URL.search(a) and not _SCP_LIKE.search(a + " ") and not _remote_host(repo, a)]
        if unknown:
            return None
    return [h for h in hosts if not host_allowed(h, allowed)]
