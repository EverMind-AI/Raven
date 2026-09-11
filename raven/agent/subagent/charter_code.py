"""Loading a charter's own judge, and the gate it has to pass to run.

A declarative rule says "this argument must start with that". Some judgements
cannot be said that way -- parse this argument, count these calls, compare two
of them -- and for those a charter may carry Python. This is what decides
whether that Python is allowed to run, and what runs it.

Two things make the decision tractable here rather than frightening:

- **Where it runs.** Inside the worker's own process, on the worker's own
  calls. That process is already an agent holding ``exec``; a judge loaded into
  it holds no authority the agent did not already have, and a judge that
  misbehaves costs one dispatch rather than the conversation that asked for it.
- **What it may be.** Not arbitrary Python: an allow-listed subset, checked on
  the parse tree before anything is compiled. No imports, no loops, no dunder
  access, no calls except a named handful. A judgement about one call needs
  none of what is refused.

The loop ban is the one worth naming. Nothing here can stop a judge from taking
forever once it is running -- the dispatch it belongs to has no deadline by
default, and that is deliberate, because a long job is a long job. Refusing
``while`` and ``for`` at the gate is what makes running forever impossible to
reach rather than merely unlikely.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Mapping, Sequence
from typing import Any

from loguru import logger

MAX_SOURCE_CHARS = 8000

ALLOWED_NODES: frozenset[type[ast.AST]] = frozenset(
    {
        ast.Module,
        ast.FunctionDef,
        ast.arguments,
        ast.arg,
        ast.Return,
        ast.If,
        ast.Assign,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Constant,
        ast.Compare,
        ast.BoolOp,
        ast.UnaryOp,
        ast.BinOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Mod,
        ast.And,
        ast.Or,
        ast.Not,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.In,
        ast.NotIn,
        ast.Is,
        ast.IsNot,
        ast.List,
        ast.Tuple,
        ast.Dict,
        ast.Set,
        ast.Subscript,
        ast.Slice,
        ast.Call,
        ast.Attribute,
        ast.Expr,
        ast.IfExp,
        ast.ListComp,
        ast.comprehension,
        ast.GeneratorExp,
        ast.JoinedStr,
        ast.FormattedValue,
        ast.keyword,
    }
)
"""Everything a judgement about one call needs, and nothing else.

``ListComp`` and ``GeneratorExp`` are admitted while ``For`` and ``While`` are
not: a comprehension walks something already in hand and ends, which is the
shape "does any earlier call match" actually takes.
"""

ALLOWED_CALLS: frozenset[str] = frozenset(
    {"len", "str", "int", "bool", "any", "all", "sorted", "set", "list", "dict", "tuple", "min", "max", "abs"}
)

ALLOWED_METHODS: frozenset[str] = frozenset(
    {"startswith", "endswith", "lower", "upper", "strip", "split", "get", "items", "keys", "values", "count", "join"}
)

ENTRY = "judge"
"""The function a charter's code must define: ``judge(name, params, prior)``,
answering with the refusal sentences for this call."""


class CharterCodeError(Exception):
    """The source did not pass the gate. The message is what to tell its author."""


def _check_tree(tree: ast.AST) -> None:
    """Walk the whole tree, refusing on the first thing outside the subset."""
    for node in ast.walk(tree):
        if type(node) not in ALLOWED_NODES:
            raise CharterCodeError(f"{type(node).__name__} is not allowed in a charter judge")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise CharterCodeError(f"attribute {node.attr!r} is not allowed")
            continue
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise CharterCodeError(f"name {node.id!r} is not allowed")
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                if target.id not in ALLOWED_CALLS:
                    raise CharterCodeError(f"calling {target.id!r} is not allowed")
            elif isinstance(target, ast.Attribute):
                if target.attr not in ALLOWED_METHODS:
                    raise CharterCodeError(f"calling .{target.attr}() is not allowed")
            else:
                raise CharterCodeError("only plain calls are allowed")


def _safe_builtins() -> dict[str, Any]:
    """The names a judge may reach, and no way to reach any other.

    An explicit mapping rather than a deletion list: a judge that tries for
    something absent gets a ``NameError`` it cannot catch its way out of,
    whereas a denylist is only as complete as the day it was written.
    """
    return {name: getattr(builtins, name) for name in ALLOWED_CALLS}


def compile_judge(source: str) -> Any:
    """Compile a charter's judge, or raise :class:`CharterCodeError`.

    The gate runs on the parse tree, before anything is compiled, so a refused
    source was never executable rather than executable-but-unlucky.
    """
    if not source.strip():
        raise CharterCodeError("empty source")
    if len(source) > MAX_SOURCE_CHARS:
        raise CharterCodeError(f"source over {MAX_SOURCE_CHARS} characters")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise CharterCodeError(f"syntax error: {exc.msg}") from exc
    if any(not isinstance(node, ast.FunctionDef) for node in tree.body):
        raise CharterCodeError("a charter judge is function definitions and nothing else")
    _check_tree(tree)
    if ENTRY not in [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]:
        raise CharterCodeError(f"must define {ENTRY}(name, params, prior)")
    namespace: dict[str, Any] = {"__builtins__": _safe_builtins()}
    code = compile(tree, "<charter>", "exec")
    exec(code, namespace)  # noqa: S102 - the gate above is the boundary, not this line
    return namespace[ENTRY]


def run_judge(
    judge: Any,
    name: str,
    params: Mapping[str, Any],
    prior: Sequence[tuple[str, Mapping[str, Any]]],
) -> list[str]:
    """Run a compiled judge, treating any failure as "no opinion".

    A judge that raises has said nothing -- not "allow", not "refuse". Letting
    it refuse would let one bad line stop every call the worker makes; letting
    the exception out would cost the dispatch. The declarative rules beside it
    still apply, which is what makes silence here safe rather than a hole.

    Copies go in. A judge that mutated the arguments it was shown would be
    editing the call it was asked to judge.
    """
    try:
        out = judge(name, dict(params), [(n, dict(p)) for n, p in prior])
    except Exception as exc:  # noqa: BLE001 - a broken judge must not cost the call
        logger.warning("charter judge raised ({}); ignoring its opinion for this call", exc)
        return []
    if isinstance(out, str):
        return [out] if out.strip() else []
    if isinstance(out, (list, tuple)):
        return [str(item) for item in out if str(item).strip()]
    return []


__all__ = [
    "ALLOWED_CALLS",
    "ALLOWED_METHODS",
    "ALLOWED_NODES",
    "ENTRY",
    "MAX_SOURCE_CHARS",
    "CharterCodeError",
    "compile_judge",
    "run_judge",
]
