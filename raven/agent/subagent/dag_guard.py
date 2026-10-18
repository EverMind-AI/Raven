# -*- coding: utf-8 -*-
"""The guard language of a DAG node's ``when``: parse, evaluate in three values, check.

A guard is an expression over atoms. An atom is ``<node id>.<route or done>`` and
asks one question of the graph: did that decision node select that route (or, for
``done``, settle at all). The runner evaluates it, never a model, and the result
is three-valued: a route of a decision node that was never executed is UNKNOWN
rather than false, so `not` cannot hold on a judgement nobody made.

This module is pure: no graph, no store, no I/O. `dag_graph` validates against it
and `dag_runner` evaluates with it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from raven.agent.subagent.prompt_errors import DagValidationError

Value = bool | None
"""True, False, or UNKNOWN (``None``): Kleene's third value."""

UNKNOWN: Value = None
DONE = "done"
"""The reserved atom name: the decision node settled, whatever it selected."""

_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_KEYWORDS = {"and", "or", "not"}
# Recursion bounds: 64 atoms give at most 63 and/or levels on any path through the tree,
# and 32 open parentheses or `not`s add at most 32 more, so every recursive pass over a
# guard stays far below Python's default recursion limit.
_MAX_ATOMS = 64
_MAX_DEPTH = 32


class GuardError(DagValidationError):
    """A guard that cannot be read. The message names the position."""


def KLEENE_AND(left: Value, right: Value) -> Value:  # noqa: N802 - the logic's own operator names
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def KLEENE_OR(left: Value, right: Value) -> Value:  # noqa: N802 - the logic's own operator names
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


def KLEENE_NOT(value: Value) -> Value:  # noqa: N802 - the logic's own operator names
    return None if value is None else not value


@dataclass(frozen=True)
class Atom:
    """``<node>.<name>``: one question about one decision node.

    ``pos`` is the atom's first character in the guard's source and ``end`` the
    position just past its last; no whitespace fits inside an atom, so the two
    name its whole extent.
    """

    node: str
    name: str
    pos: int
    end: int


@dataclass(frozen=True)
class _And:
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class _Or:
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class _Not:
    inner: "Expr"


Expr = Atom | _And | _Or | _Not


@dataclass(frozen=True)
class Guard:
    """A parsed guard: its tree, its atoms in source order, and its source text."""

    expr: Expr
    atoms: tuple[Atom, ...]
    source: str


@dataclass(frozen=True)
class _Token:
    kind: str  # "atom" | "and" | "or" | "not" | "(" | ")"
    text: str
    pos: int
    atom: Atom | None = None


def _tokenize(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    while i < len(source):
        ch = source[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "()":
            tokens.append(_Token(ch, ch, i))
            i += 1
            continue
        if ch in _NAME_CHARS:
            start = i
            while i < len(source) and source[i] in _NAME_CHARS:
                i += 1
            word = source[start:i]
            # A dot right after the word makes it a node id whatever it spells: `and`,
            # `or` and `not` are legal ids, and only the word standing alone is an operator.
            if i >= len(source) or source[i] != ".":
                if word in _KEYWORDS:
                    tokens.append(_Token(word, word, start))
                    continue
                raise GuardError(f"guard: expected '.' after '{word}' at position {start}")
            i += 1
            name_start = i
            while i < len(source) and source[i] in _NAME_CHARS:
                i += 1
            name = source[name_start:i]
            if not name:
                raise GuardError(f"guard: expected a route name after '{word}.' at position {name_start}")
            tokens.append(_Token("atom", source[start:i], start, Atom(node=word, name=name, pos=start, end=i)))
            continue
        raise GuardError(f"guard: unexpected character {ch!r} at position {i}")
    return tokens


class _Parser:
    """Recursive descent: ``or`` over ``and`` over ``not`` over atoms and parens."""

    def __init__(self, tokens: list[_Token], source: str) -> None:
        self._tokens = tokens
        self._source = source
        self._i = 0
        self._depth = 0

    def _peek(self) -> _Token | None:
        return self._tokens[self._i] if self._i < len(self._tokens) else None

    def _take(self) -> _Token:
        token = self._peek()
        if token is None:
            raise GuardError(f"guard: expression ends early at position {len(self._source)}")
        self._i += 1
        return token

    def _enter(self, token: _Token) -> None:
        self._depth += 1
        if self._depth > _MAX_DEPTH:
            raise GuardError(f"guard: nested more than {_MAX_DEPTH} levels deep at position {token.pos}")

    def _leave(self) -> None:
        self._depth -= 1

    def parse(self) -> Expr:
        if not self._tokens:
            raise GuardError("guard: the expression is empty")
        expr = self._or()
        trailing = self._peek()
        if trailing is not None:
            raise GuardError(f"guard: unexpected {trailing.text!r} at position {trailing.pos}")
        return expr

    def _or(self) -> Expr:
        expr = self._and()
        while (token := self._peek()) is not None and token.kind == "or":
            self._take()
            expr = _Or(expr, self._and())
        return expr

    def _and(self) -> Expr:
        expr = self._not()
        while (token := self._peek()) is not None and token.kind == "and":
            self._take()
            expr = _And(expr, self._not())
        return expr

    def _not(self) -> Expr:
        token = self._peek()
        if token is not None and token.kind == "not":
            self._take()
            self._enter(token)
            inner = self._not()
            self._leave()
            return _Not(inner)
        return self._primary()

    def _primary(self) -> Expr:
        token = self._take()
        if token.kind == "atom":
            assert token.atom is not None  # noqa: S101 - the tokenizer sets an Atom on every atom token
            return token.atom
        if token.kind == "(":
            self._enter(token)
            expr = self._or()
            self._leave()
            closing = self._take()
            if closing.kind != ")":
                raise GuardError(f"guard: expected ')' at position {closing.pos}")
            return expr
        raise GuardError(f"guard: unexpected {token.text!r} at position {token.pos}")


def parse_guard(source: str) -> Guard:
    """Parse a guard, or raise :class:`GuardError` naming the position."""
    tokens = _tokenize(source)
    if sum(1 for token in tokens if token.kind == "atom") > _MAX_ATOMS:
        raise GuardError(f"guard: more than {_MAX_ATOMS} atoms -- split the decision instead")
    expr = _Parser(tokens, source).parse()
    atoms: list[Atom] = []

    def walk(node: Expr) -> None:
        if isinstance(node, Atom):
            atoms.append(node)
        elif isinstance(node, (_And, _Or)):
            walk(node.left)
            walk(node.right)
        else:
            walk(node.inner)

    walk(expr)
    return Guard(expr=expr, atoms=tuple(atoms), source=source)


def evaluate(guard: Guard, value_of: Callable[[Atom], Value]) -> Value:
    """The guard's value, reading each atom through ``value_of``.

    ``and`` short-circuits on False and ``or`` on True, so an atom whose node the
    caller cannot answer is only reached when its value could still matter.
    """

    def walk(node: Expr) -> Value:
        if isinstance(node, Atom):
            return value_of(node)
        if isinstance(node, _And):
            left = walk(node.left)
            return False if left is False else KLEENE_AND(left, walk(node.right))
        if isinstance(node, _Or):
            left = walk(node.left)
            return True if left is True else KLEENE_OR(left, walk(node.right))
        return KLEENE_NOT(walk(node.inner))

    return walk(guard.expr)


def positive_atoms_under_and(guard: Guard) -> Iterable[tuple[Atom, ...]]:
    """One tuple per maximal ``and`` chain: the chain's conjuncts that are bare atoms.

    A chain is an ``and`` subtree flattened into its conjuncts. Its positive atoms are
    the conjuncts that are atoms themselves -- an ``or`` among them is not one, and is
    searched for chains of its own. Nothing under a ``not`` is yielded: a chain there
    is negated, so it cannot make the guard impossible. A bare atom outside any ``and``
    yields a one-tuple.
    """

    def conjuncts(node: Expr) -> list[Expr]:
        if isinstance(node, _And):
            return conjuncts(node.left) + conjuncts(node.right)
        return [node]

    def walk(node: Expr) -> Iterator[tuple[Atom, ...]]:
        if isinstance(node, Atom):
            yield (node,)
        elif isinstance(node, _And):
            parts = conjuncts(node)
            positive = tuple(part for part in parts if isinstance(part, Atom))
            if positive:
                yield positive
            for part in parts:
                if not isinstance(part, Atom):
                    yield from walk(part)
        elif isinstance(node, _Or):
            yield from walk(node.left)
            yield from walk(node.right)

    yield from walk(guard.expr)


def negated_atoms(guard: Guard) -> tuple[Atom, ...]:
    """Atoms negated directly -- ``not <node>.<name>`` -- in source order."""
    found: list[Atom] = []

    def walk(node: Expr) -> None:
        if isinstance(node, _Not):
            if isinstance(node.inner, Atom):
                found.append(node.inner)
            else:
                walk(node.inner)
        elif isinstance(node, (_And, _Or)):
            walk(node.left)
            walk(node.right)

    walk(guard.expr)
    return tuple(found)
