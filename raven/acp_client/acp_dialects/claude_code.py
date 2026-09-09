"""claude-agent-acp: a precise tool name, and a result sent twice.

Three things this adapter does that the spec does not describe:

- It names the tool it actually ran in ``_meta.claudeCode.toolName`` (``Bash``,
  ``Read``, ...). That is finer than ``kind``, which cannot tell ``Glob`` from
  ``Grep`` -- both are ``kind: "search"``.
- It sends the same output twice: ``rawOutput`` carries the text, and
  ``content`` carries that same text wrapped in a markdown fence. The fence is
  for a client that renders markdown; a transcript row is not one, and the
  literal ```` ```console ```` was appearing in the rendered output.
- It never emits a ``tool_call`` for ``TodoWrite`` or the Task tools. Their
  state goes to a ``sessionUpdate: "plan"`` frame instead, so the plan row is
  the only place they appear, and it is named for the tool that produced it.

``Bash`` and ``Read`` are the two names measured on the wire (v0.66.0), and any
other name is reported exactly as sent rather than checked against a list: the
record keeps the transport's own vocabulary, so a tool this adapter adds or
renames upstream is recorded for what it is without an entry anywhere.
Those names now reach a client unchanged: the read boundary renames only the
ACP spec kinds, so a claude_code row is rendered under Claude Code's own
vocabulary.
"""

from __future__ import annotations

import re
from typing import Any

from raven.acp_client.acp_dialects.base import (
    AcpDialect,
    DialectResult,
    _dict,
    content_texts,
    fold_pairs,
    paired_by_name,
)

# A fence the adapter added, not one the tool's own output contained: it wraps
# the whole payload, so an inner fence (a result that really is markdown) never
# matches and is left alone.
_FENCED = re.compile(r"\A```[\w-]*\n(?P<body>.*?)\n?```\Z", re.DOTALL)


def _unfence(text: str) -> str:
    match = _FENCED.match(text.strip())
    return match.group("body") if match else text


# What the adapter stamps on the free-text half of an `AskUserQuestion`, naming
# the property it belongs to. Measured on the wire, not documented.
_CUSTOM_ANSWER_META = "_askUserQuestionCustomAnswer"


def _custom_answer_for(field: Any) -> str | None:
    """The question a property declares itself the free-text half of, if it does.

    The marker sits on the sibling rather than on the question, so the pairing
    is stated by the half a reader would otherwise have to guess about.
    """
    named = _dict(_dict(field.meta).get(_CUSTOM_ANSWER_META)).get("questionId")
    return named if isinstance(named, str) and named else None


class ClaudeCodeDialect(AcpDialect):
    key = "claude-agent-acp"
    plan_tool_name = "TodoWrite"

    def tool_name(self, update: dict[str, Any]) -> str:
        named = _dict(_dict(update.get("_meta")).get("claudeCode")).get("toolName")
        if isinstance(named, str) and named:
            return named
        return super().tool_name(update)

    def result(self, update: dict[str, Any]) -> DialectResult:
        ok = update.get("status") == "completed"
        raw = update.get("rawOutput")
        if isinstance(raw, str) and raw.strip():
            return DialectResult(text=raw, ok=ok)
        return DialectResult(text=_unfence("".join(content_texts(update.get("content")))), ok=ok)

    def pair_fields(self, fields: list[Any]) -> list[Any]:
        """Fold a free-text sibling into its question: choices plus an "Other" box.

        The adapter renders each `AskUserQuestion` as an enum property plus an
        optional free-text sibling, mirroring the CLI's per-question "Other" box.
        Asked separately they are two prompts for what the user experiences as one
        question -- and the clarify sheet already offers exactly this shape.

        Which sibling belongs to which question is read from the adapter's own
        marker where it is stamped, and only from the `<name>_custom` spelling
        where nothing in the request is marked. The spelling alone also matches an
        independent free-text property that merely shares a prefix, and folding
        that one loses a question the schema did ask: it is never put to the user,
        and its answer surfaces only if the enum half happens to be answered
        off-enum. Against a marked request an unmarked lookalike is the adapter
        saying it is not a pair, which is worth more than the guess.
        """
        by_name = {f.name: f for f in fields}
        marked = [(q, f.name) for f in fields if (q := _custom_answer_for(f)) and q in by_name]
        return fold_pairs(fields, marked or paired_by_name(fields))


__all__ = ["ClaudeCodeDialect"]
