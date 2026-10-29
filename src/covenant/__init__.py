"""Covenant: a policy compiler and runtime for coding agents."""

from .ir import Clause, ClauseKind, ClauseStatus, Covenant, Goal, OnViolation

__all__ = ["Clause", "ClauseKind", "ClauseStatus", "Covenant", "Goal", "OnViolation"]
__version__ = "0.1.0"
