"""Trace monitor for TemporalObligation clauses.

Formulas use a small past-time LTL fragment, evaluated incrementally one event
at a time (each subformula keeps only its value at the previous step).

    G(phi)      phi must hold at every step (top level only; implied if omitted)
    !a  a & b  a | b  a -> b
    a S b       "a since b": b held at some step, and a has held at every step after it
    Y a         a held at the previous step
    O a         a held at some step so far
    H a         a has held at every step so far

Bounded forms count steps back from the current one (0 = this step):
    O[<=n] a    a held within the last n steps
    H[<=n] a    a held at each of the last n steps (and this one)
    a S[<=n] b  b held at most n steps ago, and a has held at every step after it
    a S[>=n] b  the same with b at least n steps ago; deadlines read as
                !(!test S[>=3] edit): no edit has gone 3 steps without a test
    #a <= n     a has held at most n times so far (also <, >=, >, ==)

Atoms are predicates over one event: edit, run, test, test_pass, test_fail,
commit, push, effect.

Examples:
    G(commit -> (!edit S test_pass))   every commit follows a passing test run, no edit in between
    G(#push <= 1)                      push at most once
    G(!(!test S[>=5] edit))            run the tests within 5 steps of any edit
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..events import Event

ATOMS: dict[str, Callable[[Event], bool]] = {
    "edit": lambda e: e.type == "edit",
    "run": lambda e: e.type == "run",
    "test": lambda e: e.type == "test",
    "test_pass": lambda e: e.type == "test" and e.exit_code == 0,
    "test_fail": lambda e: e.type == "test" and e.exit_code != 0,
    "commit": lambda e: e.type == "commit",
    "push": lambda e: e.type == "push",
    "effect": lambda e: e.type == "effect",
}

_TOKEN = re.compile(r"->|<=|>=|==|[()!&|\[\]<>#]|\d+|[A-Za-z_][A-Za-z0-9_]*")
_CMP = {
    "<=": lambda a, b: a <= b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b, "==": lambda a, b: a == b,
}
_ATOM = re.compile(r"[a-z_][a-z0-9_]*")


class FormulaError(ValueError):
    pass


@dataclass(frozen=True)
class Node:
    op: str  # atom, true, false, !, &, |, ->, S, Y, O, H, #
    children: tuple[Node, ...] = ()
    atom: str | None = None
    cmp: str | None = None  # bounded S and #: the comparison and its number
    n: int | None = None


def _tokenize(text: str) -> list[str]:
    tokens, pos = [], 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        m = _TOKEN.match(text, pos)
        if not m:
            raise FormulaError(f"unexpected character at {pos}: {text[pos:]!r}")
        tokens.append(m.group(0))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]):
        self.tokens, self.i = tokens, 0

    def peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str:
        tok = self.peek()
        if tok is None or (expected is not None and tok != expected):
            raise FormulaError(f"expected {expected or 'more input'}, got {tok!r}")
        self.i += 1
        return tok

    def parse(self) -> Node:
        if self.peek() == "G":
            self.take()
            self.take("(")
            node = self.implies()
            self.take(")")
        else:
            node = self.implies()
        if self.peek() is not None:
            raise FormulaError(f"unexpected {self.peek()!r} after end of formula")
        return node

    def implies(self) -> Node:
        left = self.or_()
        if self.peek() == "->":
            self.take()
            return Node("->", (left, self.implies()))
        return left

    def or_(self) -> Node:
        left = self.and_()
        while self.peek() == "|":
            self.take()
            left = Node("|", (left, self.and_()))
        return left

    def and_(self) -> Node:
        left = self.since()
        while self.peek() == "&":
            self.take()
            left = Node("&", (left, self.since()))
        return left

    def since(self) -> Node:
        left = self.unary()
        while self.peek() == "S":
            self.take()
            bound = self.bound(("<=", "<", ">=", ">"))
            cmp, n = bound
            left = Node("S", (left, self.unary()), cmp=cmp, n=n)
        return left

    def number(self) -> int:
        tok = self.take()
        if not tok.isdigit():
            raise FormulaError(f"expected a number, got {tok!r}")
        return int(tok)

    def bound(self, allowed: tuple[str, ...]) -> tuple[str | None, int | None]:
        """An optional [cmp n] after S, O or H."""
        if self.peek() != "[":
            return None, None
        self.take()
        cmp = self.take()
        if cmp not in allowed:
            raise FormulaError(f"expected one of {' '.join(allowed)} in the bound, got {cmp!r}")
        n = self.number()
        self.take("]")
        return cmp, n

    def unary(self) -> Node:
        tok = self.peek()
        if tok == "!":
            self.take()
            return Node("!", (self.unary(),))
        if tok == "Y":
            self.take()
            return Node(tok, (self.unary(),))
        if tok in ("O", "H"):
            self.take()
            cmp, n = self.bound(("<=", "<"))
            sub = self.unary()
            if cmp is None:
                return Node(tok, (sub,))
            if tok == "O":  # O[c] a  =  true S[c] a
                return Node("S", (Node("true"), sub), cmp=cmp, n=n)
            # H[c] a  =  !O[c] !a
            return Node("!", (Node("S", (Node("true"), Node("!", (sub,))), cmp=cmp, n=n),))
        if tok == "#":
            self.take()
            sub = self.unary()
            cmp = self.take()
            if cmp not in _CMP:
                raise FormulaError(f"expected a comparison such as <= after #, got {cmp!r}")
            return Node("#", (sub,), cmp=cmp, n=self.number())
        if tok == "(":
            self.take()
            node = self.implies()
            self.take(")")
            return node
        if tok in ("true", "false"):
            self.take()
            return Node(tok)
        if tok == "G":
            raise FormulaError("G is only allowed around the whole formula")
        if tok is not None and _ATOM.fullmatch(tok):
            if tok not in ATOMS:
                raise FormulaError(f"unknown atom {tok!r}; known: {', '.join(sorted(ATOMS))}")
            self.take()
            return Node("atom", atom=tok)
        raise FormulaError(f"unexpected {tok!r}")


def parse_formula(text: str) -> Node:
    return _Parser(_tokenize(text)).parse()


@dataclass
class TraceMonitor:
    formula: str
    _nodes: list[Node] = field(init=False, repr=False)
    _kids: list[tuple[int, ...]] = field(init=False, repr=False)
    _prev: list[bool] = field(init=False, repr=False)
    _aux: list[int | None] = field(init=False, repr=False)  # step distances and counts
    _first: bool = field(init=False, default=True)

    def __post_init__(self) -> None:
        self._nodes, self._kids = [], []
        self._flatten(parse_formula(self.formula))
        self._prev = [False] * len(self._nodes)
        self._aux = [None] * len(self._nodes)

    def _flatten(self, node: Node) -> int:
        kids = tuple(self._flatten(c) for c in node.children)
        self._nodes.append(node)
        self._kids.append(kids)
        return len(self._nodes) - 1

    def peek(self, event: Event) -> tuple[bool, tuple[list[bool], list[int | None]]]:
        """Evaluate the formula as if `event` happened next, without recording it."""
        now = [False] * len(self._nodes)
        aux: list[int | None] = [None] * len(self._nodes)
        first, prev, prev_aux = self._first, self._prev, self._aux
        for i, (node, kids) in enumerate(zip(self._nodes, self._kids)):
            c = [now[k] for k in kids]
            op = node.op
            if op == "atom":
                v = ATOMS[node.atom](event)
            elif op == "true":
                v = True
            elif op == "false":
                v = False
            elif op == "!":
                v = not c[0]
            elif op == "&":
                v = c[0] and c[1]
            elif op == "|":
                v = c[0] or c[1]
            elif op == "->":
                v = (not c[0]) or c[1]
            elif op == "Y":
                v = (not first) and prev[kids[0]]
            elif op == "O":
                v = c[0] or ((not first) and prev[i])
            elif op == "H":
                v = c[0] and (first or prev[i])
            elif op == "S" and node.cmp is None:
                v = c[1] or (c[0] and (not first) and prev[i])
            elif op == "S":
                # Steps back to the b that the chain of a's reaches: the nearest one for
                # an upper bound, the oldest one for a lower bound.
                chain = None if first or not c[0] or prev_aux[i] is None else prev_aux[i] + 1
                if node.cmp in ("<=", "<"):
                    d = 0 if c[1] else chain
                else:
                    d = chain if chain is not None else (0 if c[1] else None)
                aux[i] = d
                v = d is not None and _CMP[node.cmp](d, node.n)
            elif op == "#":
                aux[i] = (0 if first else prev_aux[i]) + int(c[0])
                v = _CMP[node.cmp](aux[i], node.n)
            else:  # pragma: no cover - parser only builds known ops
                raise FormulaError(op)
            now[i] = v
        return now[-1], (now, aux)

    def advance(self, state: tuple[list[bool], list[int | None]]) -> None:
        """Record an event whose state came from `peek`."""
        (self._prev, self._aux), self._first = state, False

    def step(self, event: Event) -> bool:
        ok, state = self.peek(event)
        self.advance(state)
        return ok
