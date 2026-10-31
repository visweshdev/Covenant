"""command_guard: shell commands the agent may not run, checked before they run.

Clause params:
    deny: categories to refuse (see CATEGORIES), e.g. ["force_push", "publish"]
    patterns: extra regular expressions to refuse

The Claude Code PreToolUse hook denies a matching Bash or PowerShell call up front. This is a
guard against mistakes and shortcuts, not a sandbox: an agent determined to hide a command
(by writing it into a script first, say) needs the container setup.
"""

from __future__ import annotations

import re

from ..feedback import ViolationReport
from ..ir import Clause

_GIT = r"\bgit(?:\s+-[cC]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+"

CATEGORIES: dict[str, tuple[str, str, str]] = {
    # name: (what it is, regex, hint)
    "force_push": ("a force push", _GIT + r"push\b[^;&|\n]*(?:\s--force(?:-with-lease)?\b|\s-f\b|\s\+\S)",
                   "Push normally, or ask the human; a force push rewrites shared history"),
    "history_rewrite": ("rewriting git history",
                        _GIT + r"(?:reset\s+--hard|rebase\b|commit\b[^;&|\n]*--amend|filter-branch|filter-repo|reflog\s+expire|gc\s+--prune)",
                        "Make a new commit instead of rewriting history"),
    "branch_delete": ("deleting a branch or tag", _GIT + r"(?:branch\s+-[dD]\b|push\b[^;&|\n]*(?:\s--delete\b|\s:\S)|tag\s+-d\b)",
                      "Leave branches and tags alone; ask the human"),
    "discard_changes": ("throwing away uncommitted work", _GIT + r"(?:checkout\s+(?:--\s+)?\.(?:\s|$)|restore\s+(?:--\S+\s+)*\.|clean\s+-\w*f|stash\s+(?:drop|clear))",
                        "Don't discard work in progress; ask the human"),
    "sudo": ("running as administrator", r"(?:^|[;&|(\s])(?:sudo|doas|runas)\s|Start-Process\b[^;\n]*-Verb\s+RunAs",
             "Don't use administrator rights; ask the human to run it"),
    "pipe_to_shell": ("piping a download into a shell",
                      r"\b(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b|"
                      r"\b(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n]*\|\s*(?:iex|Invoke-Expression|python3?|node)\b",
                      "Download the script, read it, and ask the human before running it"),
    "recursive_delete": ("deleting a whole folder tree outside the task",
                         r"\brm\s+-(?=\w*r)(?=\w*f)\w+\s+(?:--\s+)?(?:/|~|\$HOME|\.\.|\*|\.(?:\s|$))|"
                         r"Remove-Item\b[^\n]*-Recurse[^\n]*(?:\s[A-Z]:\\?\s|\s~|\s\.\.|\s\$HOME|\s\\\s)",
                         "Delete specific files inside the repo instead"),
    "install_packages": ("installing packages",
                         r"\b(?:pip3?|uv\s+pip|pipx)\s+install\s+(?!-r\b|-e\b|--requirement|\.)\S|\buv\s+add\b|\bpoetry\s+add\b|"
                         r"\b(?:npm|pnpm)\s+(?:install|i|add)\s+(?!-)[\w@]|\byarn\s+add\b|\bcargo\s+(?:add|install)\b|"
                         r"\bgo\s+(?:get|install)\b|\bgem\s+install\b|\b(?:apt|apt-get|brew|choco|winget|scoop)\s+install\b",
                         "Don't install new packages; ask the human"),
    "publish": ("publishing a release",
                r"\bnpm\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bpoetry\s+publish\b|\bgem\s+push\b|"
                r"\bdocker\s+push\b|\bgh\s+release\s+create\b|\buv\s+publish\b|\bvsce\s+publish\b",
                "Don't publish; leave releases to the human"),
    "deploy": ("deploying or changing infrastructure",
               r"\bkubectl\s+(?:apply|delete|replace|patch|scale|rollout)\b|\bterraform\s+(?:apply|destroy|import)\b|"
               r"\bhelm\s+(?:install|upgrade|uninstall|delete)\b|\bpulumi\s+(?:up|destroy)\b|\bfirebase\s+deploy\b|"
               r"\bvercel\b[^\n]*--prod\b|\bfly\s+deploy\b|\bserverless\s+deploy\b|\baws\s+\S+\s+(?:delete|terminate|put|create)\S*",
               "Don't deploy or change infrastructure; ask the human"),
    "network": ("network access",
                r"\b(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod|ssh|scp|sftp|rsync|nc|ncat|telnet|ftp)\b",
                "This task runs without network access; ask the human"),
    "read_secrets": ("reading secret files",
                     r"(?:^|[\s'\"=/\\])(?:\.env(?:\.[\w.-]+)?|id_rsa|id_ed25519|\.npmrc|\.pypirc|\.netrc|credentials(?:\.json)?)(?=$|[\s'\";|&)])|"
                     r"[~/\\]\.(?:ssh|aws|gnupg|kube|docker)[/\\]",
                     "Don't read secret files; ask the human for what you need"),
    "database_cli": ("running database commands directly",
                     r"\b(?:psql|mysql|mariadb|mongo(?:sh)?|redis-cli|sqlcmd|sqlite3)\b",
                     "Change the database through the app's migrations, not a database shell"),
    "kill_processes": ("killing processes", r"\b(?:kill|pkill|killall|taskkill)\b|\bStop-Process\b",
                       "Don't stop other processes; ask the human"),
    "system_changes": ("changing system settings",
                       r"\b(?:chmod\s+-R\s+777|chown\s+-R|systemctl|launchctl|reg\s+(?:add|delete)|Set-ExecutionPolicy|setx|crontab)\b",
                       "Don't change system settings; ask the human"),
}
_SAFE_ENV = re.compile(r"\.env(?:\.[\w-]+)*\.(?:example|sample|template|dist)\b")


def matches(clause: Clause, command: str) -> list[tuple[str, str, str]]:
    """(category, what it is, hint) for each denied category the command hits."""
    hits = []
    for name in clause.params.get("deny", []):
        spec = CATEGORIES.get(name)
        if not spec:
            continue
        what, rx, hint = spec
        text = _SAFE_ENV.sub("", command) if name == "read_secrets" else command
        if re.search(rx, text, re.I if name != "recursive_delete" else 0):
            hits.append((name, what, hint))
    for rx in clause.params.get("patterns", []):
        if re.search(rx, command):
            hits.append(("pattern", f"a command matching {rx}", "This command isn't allowed in this task; ask the human"))
    return hits


def check(clause: Clause, command: str) -> list[ViolationReport]:
    return [
        ViolationReport.for_clause(clause, {"command": command[:300], "category": name, "what": what}, hints=[hint])
        for name, what, hint in matches(clause, command)
    ]


def unknown_categories(clause: Clause) -> list[str]:
    return [n for n in clause.params.get("deny", []) if n not in CATEGORIES]
