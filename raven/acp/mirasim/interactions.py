"""What a Mirasim turn stops to ask, put to an ACP client, and the reply read back as an answer.

A turn that needs a person -- a question the agent asks, or a tool it wants
approved -- shows up in the turn's snapshot as an *interaction* and waits there
until ``ui-cli answer`` resolves it. This module is the pure half of relaying
one: reading the interaction, building the request that puts it in front of
the client, and reading the client's reply back into an answer. The shapes
were read from Mirasim 0.0.465's host code and seen live:

- an interaction is ``{promptId, kind, title, hint, questions, currentIndex,
  step}``; ``kind`` is ``"permission"`` for a tool approval and
  ``"user-input"`` for everything else, an MCP server's approval included;
- a question is ``{id, header, text, type, choices, multiSelect,
  allowFreeform, allowEmpty}``, ``type`` being ``"select"`` or ``"text"`` and
  each choice ``{label, description}``;
- an ask of several questions is put one question at a time, each under a
  prompt id of its own, and ``currentIndex`` names the question an answer goes
  to;
- the answer is one string whatever the action, so several choices travel
  joined -- the way Claude Code's ``AskUserQuestion`` joins them itself.

A tool approval goes out as ``session/request_permission``, the request every
ACP client answers; anything else goes out as ``elicitation/create``, which a
client answers only if it declared form support.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

PERMISSION = "permission"

ANSWER_FIELD = "answer"
CUSTOM_FIELD = f"{ANSWER_FIELD}_custom"
CUSTOM_FOR_META = "customAnswerFor"
"""The key under a property's ``_meta.raven`` naming the question it is the free-text box for.

The marker raven's own ACP server writes beside an ``ask_user`` with choices,
and the one raven's client folds into the question, so a person who types an
answer of their own is asked once rather than twice."""

_REFUSING = {"deny", "decline", "reject", "no", "cancel"}


@dataclass(frozen=True)
class Reply:
    """One ``ui-cli answer``: its ``--action`` and, for an answer, its ``--value``."""

    action: str
    value: str | None = None


def pending(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """The interactions a turn's snapshot holds that can be answered, in order."""
    items = snapshot.get("interactions")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and _text(item.get("promptId"))]


def question(interaction: dict[str, Any]) -> dict[str, Any]:
    """The question an answer goes to: the current one, else the first; empty when there is none."""
    questions = interaction.get("questions")
    if not isinstance(questions, list) or not questions:
        return {}
    index = interaction.get("currentIndex")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(questions):
        picked = questions[index]
    else:
        picked = questions[0]
    return picked if isinstance(picked, dict) else {}


def elicitation_request(session_id: str, interaction: dict[str, Any]) -> dict[str, Any]:
    """The ``elicitation/create`` params that put ``interaction`` to the client as a one-question form."""
    asked = question(interaction)
    labels = _labels(asked)
    selects = asked.get("type") == "select" and bool(labels)
    title = _text(asked.get("text")) or _text(asked.get("header")) or _text(interaction.get("title")) or "Your answer"
    field: dict[str, Any] = {"type": "string", "title": title}
    if selects and asked.get("multiSelect"):
        field = {"type": "array", "title": title, "items": {"type": "string", "enum": labels}}
    elif selects:
        field["enum"] = labels
    properties: dict[str, Any] = {ANSWER_FIELD: field}
    required: list[str] = [] if asked.get("allowEmpty") else [ANSWER_FIELD]
    if selects and asked.get("allowFreeform"):
        properties[CUSTOM_FIELD] = {
            "type": "string",
            "title": "Your own answer, when none of the options fits",
            "_meta": {"raven": {CUSTOM_FOR_META: ANSWER_FIELD}},
        }
        # A required enum could never be satisfied by an answer off the list,
        # and the person fills one property or the other.
        required = []
    return {
        "sessionId": session_id,
        "mode": "form",
        "message": _message(interaction, asked),
        "requestedSchema": {"type": "object", "properties": properties, "required": required},
    }


def read_elicitation(interaction: dict[str, Any], result: Any) -> Reply:
    """The answer a client's ``elicitation/create`` reply stands for.

    ``decline`` is a person choosing not to answer, which Mirasim calls a skip,
    and so is a form accepted with nothing in it. ``cancel`` -- and a reply in
    no shape at all -- dismisses the question.
    """
    if not isinstance(result, dict):
        return Reply("cancel")
    action = result.get("action")
    if action == "decline":
        return Reply("skip")
    if action != "accept":
        return Reply("cancel")
    content = result.get("content")
    if not isinstance(content, dict):
        return Reply("skip")
    asked = question(interaction)
    chosen = _said(content.get(ANSWER_FIELD))
    typed = _said(content.get(CUSTOM_FIELD))
    if typed and not (chosen in _labels(asked) and not asked.get("multiSelect")):
        chosen = typed
    if not chosen:
        return Reply("skip")
    return Reply("select" if asked.get("type") == "select" else "text", chosen)


def permission_request(session_id: str, interaction: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The ``session/request_permission`` params for a tool approval, and the label each option id stands for.

    Option ids are minted per request, so an id from an earlier request, or one
    a client made up, selects nothing.
    """
    asked = question(interaction)
    offered: dict[str, str] = {}
    options: list[dict[str, Any]] = []
    for index, label in enumerate(_labels(asked) or ["Allow", "Deny"]):
        option_id = f"choice-{index}-{uuid4().hex}"
        offered[option_id] = label
        kind = "reject_once" if label.lower() in _REFUSING else "allow_once"
        options.append({"optionId": option_id, "name": label, "kind": kind})
    title = _text(asked.get("text")) or _text(interaction.get("title")) or "Approve tool use?"
    params = {
        "sessionId": session_id,
        "toolCall": {
            "toolCallId": f"mirasim-{_text(interaction.get('promptId'))}",
            "title": title,
            "kind": "other",
            "status": "pending",
        },
        "options": options,
    }
    return params, offered


def read_permission(result: Any, offered: dict[str, str]) -> Reply:
    """The answer a client's ``session/request_permission`` reply stands for: the label it selected, or a dismissal."""
    outcome = result.get("outcome") if isinstance(result, dict) else None
    if isinstance(outcome, dict) and outcome.get("outcome") == "selected":
        label = offered.get(outcome.get("optionId"))
        if label:
            return Reply("select", label)
    return Reply("cancel")


def describe(interaction: dict[str, Any]) -> str:
    """One interaction in words, for a reply that has to carry it instead of the client."""
    asked = question(interaction)
    said = _text(asked.get("text")) or _text(asked.get("header"))
    title = _text(interaction.get("title"))
    line = f"{title}: {said}" if title and said else title or said or "a question"
    if labels := _labels(asked):
        line += f" ({' / '.join(labels)})"
    if hint := _text(interaction.get("hint")):
        line += f"\n  {hint}"
    return line


def _message(interaction: dict[str, Any], asked: dict[str, Any]) -> str:
    lines = [_text(interaction.get("title")) or "The agent asks"]
    if hint := _text(interaction.get("hint")):
        lines.append(hint)
    step = interaction.get("step")
    total = step.get("total") if isinstance(step, dict) else None
    index = step.get("index") if isinstance(step, dict) else None
    if isinstance(total, int) and isinstance(index, int) and total > 1:
        lines.append(f"Question {index + 1} of {total}")
    for choice in asked.get("choices") if isinstance(asked.get("choices"), list) else ():
        if isinstance(choice, dict) and _text(choice.get("label")) and _text(choice.get("description")):
            lines.append(f"- {_text(choice.get('label'))}: {_text(choice.get('description'))}")
    return "\n".join(lines)


def _labels(asked: dict[str, Any]) -> list[str]:
    choices = asked.get("choices")
    if not isinstance(choices, list):
        return []
    return [label for choice in choices if isinstance(choice, dict) and (label := _text(choice.get("label")))]


def _said(value: Any) -> str:
    """A form value as the one string ``ui-cli answer`` takes; empty for no answer."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(item.strip() for item in value if isinstance(item, str) and item.strip())
    return value.strip() if isinstance(value, str) else ""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "ANSWER_FIELD",
    "CUSTOM_FIELD",
    "CUSTOM_FOR_META",
    "PERMISSION",
    "Reply",
    "describe",
    "elicitation_request",
    "pending",
    "permission_request",
    "question",
    "read_elicitation",
    "read_permission",
]
