"""The compiler: plain-English instruction -> approved Covenant file.

    extract   an LLM (or the offline extractor) splits the instruction into a goal and
              candidate clauses, each tied to its exact words
    ground    deterministic: find the real public API, user data and test command
    bind      deterministic: turn each candidate into a typed clause with a checker,
              or mark it unenforceable
    clarify   ask at most three concrete questions where the repo leaves real ambiguity
    validate  static checks, coverage and contradictions
    emit      write covenant.yaml and a summary for the developer to approve

The LLM proposes; the deterministic passes dispose.
"""

from .pipeline import CompileResult, compile_instruction

__all__ = ["CompileResult", "compile_instruction"]
