"""Extraction: instruction -> goal + candidate clauses.

Two extractors share one output type:
    LLMExtractor        Claude with schema-constrained output (the default)
    HeuristicExtractor  phrase patterns, no network; covers the common phrasings
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Protocol
from urllib.parse import urlparse

from .ground import RepoFacts
from .schema import INTENT_HELP, CandidateClause, Extraction, Intent

DEFAULT_MODEL = "claude-opus-5-5"


class CompileError(RuntimeError):
    pass


class Extractor(Protocol):
    name: str

    def __call__(self, instruction: str, facts: RepoFacts) -> Extraction: ...


SYSTEM_PROMPT = f"""You turn a developer's instruction to a coding agent into a task goal and a list of constraints. \
A deterministic system checks your output against the instruction and binds each constraint to a real verifier, \
so precision matters more than coverage of things the instruction does not say.

Rules:
- Copy goal_span, every source_span, and every no_constraint_spans entry verbatim from the instruction.
- Every part of the instruction must fall in goal_span, a clause's source_span, or no_constraint_spans.
- Make one clause per distinct constraint. Do not merge two constraints or split one.
- Do not add constraints the instruction does not state, even sensible ones.
- Pick the closest intent. Use "other" for constraints no intent covers (style, readability, process).
- In paths, list only paths, folders, tables or names the instruction itself mentions for that constraint.
- The repository facts help you understand the developer's words; they are not constraints.

Intents:
{chr(10).join(f"- {i.value}: {h}" for i, h in INTENT_HELP.items())}"""


def _anthropic() -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise CompileError("The Anthropic SDK isn't installed (pip install anthropic), or use --offline.") from exc
    return anthropic


def endpoint_note(env: Any = None) -> str | None:
    """A warning when the Anthropic SDK would send compile requests somewhere other than Anthropic.

    Shells started by other tools (an agent CLI, a proxy setup) often export
    ANTHROPIC_BASE_URL; the SDK follows it silently, and a proxy may not support the model
    or the structured output Covenant asks for.
    """
    env = os.environ if env is None else env
    if env.get("COVENANT_ANTHROPIC_BASE_URL"):
        return None
    url = env.get("ANTHROPIC_BASE_URL")
    if not url:
        return None
    host = urlparse(url).hostname or url
    if host == "anthropic.com" or host.endswith(".anthropic.com"):
        return None
    return (
        f"ANTHROPIC_BASE_URL points at {host}, so the instruction is sent there, not to Anthropic. "
        "Set COVENANT_ANTHROPIC_BASE_URL=https://api.anthropic.com to override it for Covenant only."
    )


class LLMExtractor:
    name = "claude"

    def __init__(self, client: Any = None, model: str = DEFAULT_MODEL, effort: str = "high"):
        self._client = client
        self.model = model
        self.effort = effort

    @property
    def client(self) -> Any:
        if self._client is None:
            base_url = os.environ.get("COVENANT_ANTHROPIC_BASE_URL")
            self._client = _anthropic().Anthropic(**({"base_url": base_url} if base_url else {}))
        return self._client

    def __call__(self, instruction: str, facts: RepoFacts) -> Extraction:
        anthropic = _anthropic()

        user = f"<instruction>\n{instruction}\n</instruction>\n\n<repository>\n{facts.summary()}\n</repository>"
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM_PROMPT,
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": Extraction.model_json_schema()},
                },
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AuthenticationError as exc:
            raise CompileError("Anthropic API credentials are missing or invalid. Set ANTHROPIC_API_KEY, or use --offline.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise CompileError(f"The API key can't use {self.model}: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise CompileError(f"Model {self.model!r} not found: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise CompileError("Rate limited by the Anthropic API; try again shortly.") from exc
        except anthropic.BadRequestError as exc:
            raise CompileError(f"The API rejected the request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise CompileError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise CompileError("Can't reach the Anthropic API. Check the network, or use --offline.") from exc
        except anthropic.AnthropicError as exc:  # e.g. no credentials configured at all
            raise CompileError(f"{exc}. Set ANTHROPIC_API_KEY, or use --offline.") from exc

        if response.stop_reason == "refusal":
            raise CompileError("The model declined to process this instruction.")
        if response.stop_reason == "max_tokens":
            raise CompileError("The extraction was cut off (max_tokens); shorten the instruction.")
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
        if not text:
            raise CompileError("The model returned no extraction.")
        try:
            return Extraction.model_validate_json(text)
        except ValueError as exc:
            raise CompileError(f"The model's extraction did not match the schema: {exc}") from exc


# ----- Claude through the Claude Code CLI (uses its login, no API key) --------

_CMD_UNSAFE = set('&|<>^%"\n')  # characters a Windows .cmd shim may mangle in an argument


class ClaudeCodeExtractor:
    """Runs the extraction through `claude -p` so a Claude subscription login is enough.

    `--restricted` ignores the repo's own settings (so Covenant's hooks don't run inside the
    compile) and removes every tool that can run code; `--tools ""` removes the rest.
    """

    name = "claude-code"

    def __init__(self, model: str | None = None, exe: str | None = None, runner: Any = None, timeout: int = 300):
        self.model = model
        self.exe = exe or shutil.which("claude")
        self.runner = runner or subprocess.run
        self.timeout = timeout

    def __call__(self, instruction: str, facts: RepoFacts) -> Extraction:
        if not self.exe:
            raise CompileError("Claude Code isn't installed (the `claude` command), so set ANTHROPIC_API_KEY or use --offline.")
        user = f"<instruction>\n{instruction}\n</instruction>\n\n<repository>\n{facts.summary()}\n</repository>"
        schema = json.dumps(Extraction.model_json_schema())
        cmd = [self.exe, "-p", "--restricted", "--strict-mcp-config", "--tools", "", "--no-session-persistence",
               "--output-format", "json", "--json-schema", schema]
        if self.exe.lower().endswith((".cmd", ".bat")) and _CMD_UNSAFE & set(SYSTEM_PROMPT):
            user = f"{SYSTEM_PROMPT}\n\n{user}"  # cmd.exe could mangle it as an argument; send it with the input
        else:
            cmd += ["--system-prompt", SYSTEM_PROMPT]
        if self.model:
            cmd += ["--model", self.model]
        with tempfile.TemporaryDirectory(prefix="covenant-compile-") as empty:  # nothing for file tools to see
            try:
                r = self.runner(cmd, input=user, capture_output=True, text=True, encoding="utf-8",
                                timeout=self.timeout, cwd=empty)
            except subprocess.TimeoutExpired as exc:
                raise CompileError(f"Claude Code didn't answer within {self.timeout}s; try again or use --offline.") from exc
        try:
            out = json.loads(r.stdout)
        except ValueError:
            detail = (r.stderr or r.stdout).strip()[-300:]
            raise CompileError(f"Claude Code failed (exit {r.returncode}): {detail or 'no output'}") from None
        if out.get("is_error"):
            message = str(out.get("result") or out.get("api_error_status") or "unknown error")
            if "login" in message.lower():
                raise CompileError("Claude Code isn't logged in. Run `claude` and /login, set ANTHROPIC_API_KEY, or use --offline.")
            raise CompileError(f"Claude Code reported an error: {message}")
        data = out.get("structured_output")
        if data is None:
            raise CompileError("Claude Code returned no structured extraction; try again or use --offline.")
        try:
            return Extraction.model_validate(data)
        except ValueError as exc:
            raise CompileError(f"The model's extraction did not match the schema: {exc}") from exc


def choose_extractor(backend: str = "auto", model: str | None = None, env: Any = None) -> Extractor:
    """`auto`: the API when a key is set, otherwise the Claude Code login, otherwise ask for --offline."""
    env = os.environ if env is None else env
    if backend == "offline":
        return HeuristicExtractor()
    if backend == "api" or (backend == "auto" and (env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"))):
        return LLMExtractor(**({"model": model} if model else {}))
    if backend in ("auto", "claude-code"):
        if backend == "auto" and not shutil.which("claude"):
            raise CompileError("No way to reach Claude: set ANTHROPIC_API_KEY, install and log in to Claude Code, or use --offline.")
        return ClaudeCodeExtractor(model=model)
    raise CompileError(f"unknown backend {backend!r} (auto, api, claude-code, offline)")


# ----- offline extractor -----------------------------------------------------

_NEG = r"(?:don'?t|do not|never|must not|mustn'?t|shouldn'?t|without|avoid|no|not)\b"
_SPLIT = re.compile(
    r"\s*[;\n]\s*|\.\s+|,\s*(?:but\s+|and\s+|then\s+)?|\s+but\s+|\s+and\s+(?=(?:also\s+)?(?:don'?t|do not|never|run|always|make sure|only|keep|ensure)\b)",
    re.I,
)
_PATHS = re.compile(r"(?:~|\.)?[\w.-]*/[\w./*-]*|\b[\w-]+\.(?:py|ts|tsx|js|json|ya?ml|toml|db|sqlite3?|md|lock|cfg|ini|env)\b")

_RULES: list[tuple[Intent, re.Pattern[str]]] = [
    # Checked first: these phrasings also look like the older, broader rules below.
    (Intent.FORBIDDEN_COMMANDS, re.compile(
        rf"{_NEG}.*\b(force[ -]?push\w*|push\s+--force|push\s+-f\b|rewrite\w*\s+(?:git\s+)?history|rebase|amend|reset\s+--hard|"
        r"sudo|rm\s+-rf|curl|wget|deploy\w*|publish\w*|kubectl|terraform|install\w*\s+(?:any\s+)?(?:new\s+)?(?:packages?|deps|dependenc\w*|librar\w*|anything)|"
        r"network|internet|go\s+online|delete\s+(?:any\s+)?branch\w*|kill\w*|discard\w*|database\s+(?:shell|cli|directly)|system\s+settings)|"
        rf"{_NEG}.*\b(read|open|look\s+at|access|cat|print|show)\b.*(\.env\b|\bsecrets?\b|\bcredentials?\b|\bssh\b)", re.I)),
    (Intent.NO_SECRETS, re.compile(
        rf"(?:{_NEG}|\bkeep\b|\bhard-?cod).*\b(secrets?|credentials?|api[ -]?keys?|tokens?|passwords?)\b|(?:{_NEG}|\bkeep\b).*\.env\b", re.I)),
    (Intent.KEEP_TESTS, re.compile(
        rf"{_NEG}.*\b(delet|remov|skip|disabl|weaken|comment\w*\s+out|xfail|break)\w*\b.*\btests?\b|"
        rf"{_NEG}.*\btests?\b.*\b(weaker|skipped|disabled)\b|\bkeep\b.*\b(all|every|existing)\b.*\btests?\b", re.I)),
    (Intent.NO_NEW_DEPENDENCIES, re.compile(
        rf"{_NEG}.*\b(add|introduce|new|pull\s+in)\b.*\b(dependenc\w*|packages?|librar\w*|deps)\b|\bno\s+new\s+(dependenc|packages?|librar|deps)", re.I)),
    (Intent.ONLY_PATHS, re.compile(
        r"\b(only|just)\s+(?:change|touch|edit|modify|work\s+(?:in|on))\b|\boutside\s+(?:of\s+)?\S*/|"
        r"\blimit\s+(?:the\s+|your\s+)?(?:changes?|edits?)\s+to\b|\bkeep\s+(?:all\s+)?(?:the\s+|your\s+)?(?:changes?|edits?)\s+(?:with)?in\b", re.I)),
    (Intent.SMALL_CHANGE, re.compile(
        r"\b(small|minimal|focused|tiny|surgical)\s+(change|diff|pr|patch|edit)s?\b|\bunder\s+\d+\s+lines\b|"
        r"\b(at most|no more than|fewer than|less than|max(?:imum)?(?: of)?)\s+\d+\s+(lines|files)\b|\bkeep\s+(?:the\s+)?(?:change|diff|pr)s?\s+small", re.I)),
    (Intent.NO_DEBUG_CODE, re.compile(
        r"\bdebug(?:ging)?\s+(?:code|prints?|statements?|output|logs?|leftovers?)\b|console\.log|\bprint\s+statements?\b|"
        r"\bbreakpoints?\b|\bdebugger\b|\bpdb\b", re.I)),
    (Intent.NO_SUPPRESSIONS, re.compile(
        r"\b(suppress|silenc|disabl|ignor|mute)\w*\b.*\b(warnings?|lint\w*|type\s*(?:check\w*|errors?)|mypy|eslint|pylint|errors)\b|"
        r"type:\s*ignore|\bnoqa\b|ts-ignore|eslint-disable", re.I)),
    (Intent.NO_LARGE_FILES, re.compile(
        r"\b(large|big|huge)\s+(?:files?|binar\w*|assets?|blobs?)\b|\bbinar(?:y|ies)\b|\bfiles?\s+(?:over|larger than|bigger than)\s+\d+", re.I)),
    (Intent.PUBLIC_API_UNCHANGED, re.compile(rf"{_NEG}.*\b(apis?|interfaces?|signatures?|exports?)\b|\bkeep\b.*\b(api|backward[s]? compat)", re.I)),
    (Intent.NO_DESTRUCTIVE_MIGRATIONS, re.compile(r"\bdestructive\s+migrations?\b|\bdrop\s+(?:any\s+)?(?:columns?|tables?)\b", re.I)),
    (Intent.NO_DATA_LOSS, re.compile(rf"{_NEG}.*\b(delete|remove|wipe|lose|drop|destroy|erase|overwrite|truncate|clear|rm)\b", re.I)),
    (Intent.PROTECT_PATHS, re.compile(rf"{_NEG}\s*(?:\w+\s+)?(touch|modify|edit|change|alter)\b", re.I)),
    (Intent.ACTION_LIMIT, re.compile(
        r"\b(commit|push)\w*\b.*\b(at most|no more than|more than|only|max(?:imum)?(?: of)?)\s+(once|twice|(?:\w+\s+){1,2}times?|\d+)\b", re.I)),
    (Intent.TESTS_WITHIN_STEPS, re.compile(r"\btests?\b.*\bwithin\s+\w+\s+(steps?|actions?|tool calls?)\b", re.I)),
    (Intent.TESTS_BEFORE_PUSH, re.compile(r"\btests?\b.*\bbefore\b.*\bpush|\bpush\b.*\bonly after\b.*\btests?\b", re.I)),
    (Intent.TESTS_BEFORE_COMMIT, re.compile(r"\btests?\b.*\bbefore\b.*\bcommit|\bcommit\b.*\bonly after\b.*\btests?\b|\btests?\b.*\bpass\b.*\bbefore\b", re.I)),
    (Intent.NO_PUSH, re.compile(rf"{_NEG}\s*push\b", re.I)),
    (Intent.NO_COMMIT, re.compile(rf"{_NEG}\s*commit\b", re.I)),
    (Intent.DEPENDENCY_SCOPE, re.compile(
        rf"\b(only|just)\b.*\b(dependenc|packages?|versions?)|\b(only|just)\s+(update|upgrade|bump)\b|{_NEG}.*\bmajor (version|bump|upgrade)",
        re.I,
    )),
    (Intent.OTHER, re.compile(rf"{_NEG}|\b(always|make sure|ensure|keep|must)\b", re.I)),
]


def _snake(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:4]
    return "_".join(words) or "task"


class HeuristicExtractor:
    name = "offline"

    def __call__(self, instruction: str, facts: RepoFacts) -> Extraction:
        segments = [s.strip(" .,") for s in _SPLIT.split(instruction) if s and s.strip(" .,")]
        goal_span, clauses, filler = "", [], []
        for seg in segments:
            intent = next((i for i, pattern in _RULES if pattern.search(seg)), None)
            if intent is None:
                if not goal_span:
                    goal_span = seg
                else:
                    filler.append(seg)
                continue
            paths = [q for q in (p.rstrip(".,") for p in _PATHS.findall(seg)) if q and not re.fullmatch(r"[./]+", q)]
            only_databases = all(p.endswith((".db", ".sqlite", ".sqlite3")) for p in paths)
            if (intent is Intent.NO_DATA_LOSS and paths and not only_databases
                    and not re.search(r"\b(data|records?|database)\b", seg, re.I)):
                intent = Intent.PROTECT_PATHS
            if intent is Intent.PROTECT_PATHS and not paths:
                intent = Intent.OTHER
            clauses.append(CandidateClause(source_span=seg, intent=intent, paths=paths, reading=seg))
        if not goal_span and segments:
            goal_span = segments[0]
        return Extraction(goal_action=_snake(goal_span), goal_span=goal_span, clauses=clauses, no_constraint_spans=filler)
