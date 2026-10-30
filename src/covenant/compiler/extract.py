"""Extraction: instruction -> goal + candidate clauses.

Two extractors share one output type:
    LLMExtractor        Claude with schema-constrained output (the default)
    HeuristicExtractor  phrase patterns, no network; covers the common phrasings
"""

from __future__ import annotations

import os
import re
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


# ----- offline extractor -----------------------------------------------------

_NEG = r"(?:don'?t|do not|never|must not|mustn'?t|shouldn'?t|without|avoid|no|not)\b"
_SPLIT = re.compile(
    r"\s*[;\n]\s*|\.\s+|,\s*(?:but\s+|and\s+|then\s+)?|\s+but\s+|\s+and\s+(?=(?:also\s+)?(?:don'?t|do not|never|run|always|make sure|only|keep|ensure)\b)",
    re.I,
)
_PATHS = re.compile(r"(?:~|\.)?[\w.-]*/[\w./*-]*|\b[\w-]+\.(?:py|ts|tsx|js|json|ya?ml|toml|db|sqlite3?|md|lock|cfg|ini|env)\b")

_RULES: list[tuple[Intent, re.Pattern[str]]] = [
    (Intent.PUBLIC_API_UNCHANGED, re.compile(rf"{_NEG}.*\b(apis?|interfaces?|signatures?|exports?)\b|\bkeep\b.*\b(api|backward[s]? compat)", re.I)),
    (Intent.NO_DESTRUCTIVE_MIGRATIONS, re.compile(r"\bdestructive\s+migrations?\b|\bdrop\s+(?:any\s+)?(?:columns?|tables?)\b", re.I)),
    (Intent.NO_DATA_LOSS, re.compile(rf"{_NEG}.*\b(delete|remove|wipe|lose|drop|destroy|erase|overwrite|truncate|clear|rm)\b", re.I)),
    (Intent.PROTECT_PATHS, re.compile(rf"{_NEG}\s*(?:\w+\s+)?(touch|modify|edit|change|alter)\b", re.I)),
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
