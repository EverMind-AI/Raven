"""Read a pre-merge ``action.checks`` block as the ``judge`` that replaced it.

A Harness saved before the judgement seat became one surface carries a
declarative rule table, and sometimes a judge beside it. Both still have to
load: an artifact is durable, and a reader that refused one would take a
Persona out of service for a field the writer never chose.

The table is translated rather than kept, because keeping it would be the
second surface again. Every operator it had maps onto something ``judge``
already reads -- ``prior`` is the call history the rules matched on -- so the
translation is total rather than best-effort, and the generated source passes
the same gate a model-written judge does.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

MIGRATED_NOTE = "migrated from action.checks"


def _literal(value: str) -> str:
    """One string as a Python literal the gate admits."""
    return repr(str(value))


def _when_clause(when: dict[str, Any]) -> str:
    """The ``when`` narrowing, as conditions on ``params``."""
    return "".join(
        f" and str(params.get({_literal(key)}, '')) == {_literal(value)}" for key, value in sorted(when.items())
    )


def _grouped(rules: list[Any]) -> dict[tuple[str, str], dict[str, list[tuple[str, str]]]]:
    """Rules by (tool, when), then by operator, as (value, message) pairs.

    Grouped before anything is written because a table is mostly the same few
    shapes repeated: thirty-two forbidden fragments for one tool are one list
    and one comprehension, not thirty-two blocks. The source has to stay under
    the ceiling a charter carries, and a translation that cannot be dispatched
    has preserved nothing.
    """
    out: dict[tuple[str, str], dict[str, list[tuple[str, str]]]] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        tool = str(rule.get("tool") or "")
        if not tool:
            continue
        when = rule.get("when") if isinstance(rule.get("when"), dict) else {}
        key = (tool, _when_clause(when))
        by_operator = out.setdefault(key, {})
        prefix = str(rule.get("pathPrefix") or rule.get("path_prefix") or "")
        if prefix:
            message = str(rule.get("message") or f"{tool}: path must start with {prefix}")
            by_operator.setdefault("prefix", []).append((prefix, message))
        forbid = str(rule.get("forbid") or "")
        if forbid:
            message = str(rule.get("message") or f"{tool}: this call may not contain {forbid!r}")
            by_operator.setdefault("forbid", []).append((forbid, message))
        earlier = str(rule.get("requiresPrior") or rule.get("requires_prior") or "")
        if earlier:
            message = str(rule.get("message") or f"{tool}: run {earlier} first")
            match_param = str(rule.get("matchParam") or rule.get("match_param") or "")
            by_operator.setdefault("prior", []).append((f"{earlier}\x00{match_param}", message))
    return out


def _pairs(values: list[tuple[str, str]]) -> str:
    return "[" + ", ".join(f"({_literal(value)}, {_literal(message)})" for value, message in values) + "]"


def judge_from_rules(rules: list[Any]) -> str:
    """The rule table as one ``judge``, or "" when it refuses nothing."""
    blocks: list[str] = []
    for (tool, when), by_operator in _grouped(rules).items():
        body: list[str] = []
        if prefixes := by_operator.get("prefix"):
            body += [
                "        path = str(params.get('path', '') or params.get('file_path', ''))",
                f"        refusals = refusals + [m for p, m in {_pairs(prefixes)} if path and not path.startswith(p)]",
            ]
        if forbidden := by_operator.get("forbid"):
            body += [
                "        seen = ' '.join([str(v) for v in params.values()])",
                f"        refusals = refusals + [m for f, m in {_pairs(forbidden)} if f in seen]",
            ]
        for encoded, message in by_operator.get("prior", []):
            earlier, _, match_param = encoded.partition("\x00")
            same = f" and p.get({_literal(match_param)}) == params.get({_literal(match_param)})" if match_param else ""
            body += [
                f"        if not [p for n, p in prior if n == {_literal(earlier)}{same}]:",
                f"            refusals = refusals + [{_literal(message)}]",
            ]
        if body:
            blocks += [f"    if name == {_literal(tool)}{when}:", *body]
    if not blocks:
        return ""
    return "\n".join(["def judge(name, params, prior):", "    refusals = []", *blocks, "    return refusals"])


def migrated_action(action: dict[str, Any]) -> dict[str, Any]:
    """``action`` with any ``checks`` block read as ``functions.judge``.

    ``code`` wins when both are present: it is what its author wrote, and the
    table beside it is translated and appended to the same seat only when
    there is no hand-written judge to keep whole. The pair is rare enough --
    a worker with code and rules both -- that losing neither matters more than
    merging them into one function the gate would then have to admit.
    """
    checks = action.get("checks")
    if not isinstance(checks, dict):
        return action
    migrated = {key: value for key, value in action.items() if key != "checks"}
    functions = dict(migrated.get("functions") or {})
    code = str(checks.get("code") or "").strip()
    rules = checks.get("rules") if isinstance(checks.get("rules"), list) else []
    if "judge" not in functions:
        if code:
            functions["judge"] = code
            if judge_from_rules(rules):
                # Said rather than dropped quietly: the pair is rare, and an
                # operator whose rule table stopped applying should read why
                # here rather than infer it from a call that is no longer
                # refused.
                logger.warning(
                    "playbook: a seat carried both action.checks.code and {} rule(s); the hand-written "
                    "judge is kept whole and the rules are not merged into it. State them in that judge "
                    "if they still apply.",
                    len([rule for rule in rules if isinstance(rule, dict)]),
                )
        elif generated := judge_from_rules(rules):
            functions["judge"] = generated
    if functions:
        migrated["functions"] = functions
    return migrated


__all__ = ["MIGRATED_NOTE", "judge_from_rules", "migrated_action"]
