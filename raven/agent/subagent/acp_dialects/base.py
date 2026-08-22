"""Reading an ACP tool call the way the spec describes it.

An ACP adapter reports a tool call twice over: once machine-readably, in
``kind`` (one of ten spec values) and ``locations``, and once for a human, in
``title``. Only the first is comparable across adapters -- measured, the same
``kind: "execute"`` arrives titled ``"Terminal"`` from claude-agent-acp and
titled with the entire shell command from codex-acp -- so the ``kind`` is what
this reports and the title is kept only as the fallback for a call that carries
no argument at all.

What it reports is the transport's own name for the call, at the finest grain
the transport gives. Naming it in raven's vocabulary is the read boundary's job
(:mod:`raven.agent.subagent.tool_vocabulary`): presentation is recoverable from
provenance and provenance is not recoverable from presentation, so a record that
renamed the call could never be read back for what the agent actually ran.
Handing a renderer the adapter's *title* instead is the one thing that is not on
the table -- it put a 100-character pipeline where a verb belongs.

Adapters that answer differently from the spec subclass this; see
:mod:`raven.agent.subagent.acp_dialects.claude_code` and
:mod:`raven.agent.subagent.acp_dialects.codex`. Anything not measured is left to
this class rather than guessed at per adapter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from raven.agent.subagent.tool_vocabulary import SUBJECT_KEYS

_FALLBACK_TOOL = "tool_call"

# How much of a call fits in one label before it stops being one.
_LABEL_CHARS = 120


@dataclass(frozen=True)
class ToolCall:
    """One tool call, in the transport's own vocabulary.

    ``title`` is kept alongside rather than folded into ``argument`` because the
    two are not interchangeable: an adapter that titles a call ``"Terminal"``
    says less than its argument, and one that titles it with the command says
    exactly the same thing twice.
    """

    id: str
    name: str
    argument: str
    title: str
    raw_input: dict[str, Any]

    @property
    def subject(self) -> str:
        """What to show beside the verb: the argument, or the title if there is none.

        A property rather than something baked into ``argument`` at parse time,
        because the argument usually arrives *after* the call is announced --
        measured, claude-agent-acp opens every call with ``rawInput: {}``. Baking
        the title in made it a real argument that a later frame could no longer
        replace, and every claude call was titled "Terminal".
        """
        return self.argument or self.title

    @property
    def label(self) -> str:
        """One short phrase naming the call, for a trace attribute or a flat list.

        Verb and target together, because either alone loses half of it: a list
        of eighteen ``exec`` says nothing about what ran, and the command alone
        does not survive being read as a label. Clipped, since a shell pipeline
        has no length a span attribute can rely on.
        """
        subject = self.subject.replace("\n", " ").strip()
        if not subject:
            return self.name
        room = _LABEL_CHARS - len(self.name) - 1
        clipped = subject if len(subject) <= room else f"{subject[: room - 1]}\u2026"
        return f"{self.name} {clipped}"

    def arguments_json(self) -> str:
        """The call's arguments, in the adapter's own spelling.

        Renaming the subject onto raven's key moved to the read boundary along
        with the tool name: the two shared one lookup, so they had to move
        together or a row would be named one way and keyed the other.

        A subject that reached the frame outside ``rawInput`` -- the spec's
        ``locations``, or the adapter's title -- has no field of its own to be
        stored under, so it keeps the literal key ``argument``, which
        ``SUBJECT_KEYS`` carries for the read boundary to lift back off.
        """
        merged = {k: v for k, v in self.raw_input.items() if v not in (None, "", {}, [])}
        if not merged and self.subject:
            merged["argument"] = self.subject
        return json.dumps(merged, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class ToolResult:
    """What a finished call returned, and whether it worked."""

    text: str
    ok: bool


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def content_texts(content: Any) -> list[str]:
    """Every text string in an ACP content value, whatever shape it arrived in.

    A tool call's content nests one level deeper than a message's --
    ``{"type": "content", "content": <block>}`` -- so a dict carrying no text of
    its own is followed into whatever it wraps.
    """
    if isinstance(content, str):
        return [content]
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return [text]
        return content_texts(content.get("content"))
    if isinstance(content, list):
        return [t for item in content for t in content_texts(item)]
    return []


def _first_location(update: dict[str, Any]) -> str:
    locations = update.get("locations")
    if not isinstance(locations, list):
        return ""
    for entry in locations:
        path = _dict(entry).get("path")
        if isinstance(path, str) and path:
            return path
    return ""


class AcpDialect:
    """How to read one adapter's tool-call updates. The base is the spec."""

    key = ""
    """Substring of ``agentInfo.name`` that selects this dialect."""

    def tool_name(self, update: dict[str, Any]) -> str:
        """The transport's own name for the call, at the finest grain it gives.

        The spec's ``kind`` verbatim: mapping it into raven's vocabulary is the
        read boundary's job (:mod:`raven.agent.subagent.tool_vocabulary`),
        because a record that renamed it could never be read back for what the
        agent actually ran.
        """
        kind = update.get("kind")
        return kind if isinstance(kind, str) and kind else _FALLBACK_TOOL

    def argument(self, update: dict[str, Any]) -> str:
        """The call's subject: what a reader needs beside the verb.

        ``locations`` is consulted before the generic sweep and after the named
        keys because it is the spec's own answer for a file-touching call --
        measured, codex-acp's ``read`` sends no ``rawInput`` at all and the path
        exists nowhere else on the frame.
        """
        raw = _dict(update.get("rawInput"))
        for key in SUBJECT_KEYS:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        located = _first_location(update)
        if located:
            return located

        for value in raw.values():
            if isinstance(value, str) and value.strip():
                return value.strip()

        return ""

    def result(self, update: dict[str, Any]) -> ToolResult:
        """What the call returned. ``content`` is what the adapter chose to show."""
        text = "".join(content_texts(update.get("content")))
        if not text:
            text = self._raw_output_text(update.get("rawOutput"))
        return ToolResult(text=text, ok=update.get("status") == "completed")

    @staticmethod
    def _raw_output_text(raw: Any) -> str:
        if isinstance(raw, str):
            return raw
        if raw is None:
            return ""
        return json.dumps(raw, ensure_ascii=False, default=str)

    def call(self, update: dict[str, Any]) -> ToolCall:
        title = update.get("title")
        return ToolCall(
            id=str(update.get("toolCallId") or ""),
            name=self.tool_name(update),
            argument=self.argument(update),
            title=title.strip() if isinstance(title, str) else "",
            raw_input=_dict(update.get("rawInput")),
        )

    def revises_call(self, update: dict[str, Any]) -> bool:
        """Whether this ``tool_call_update`` carries argument fields worth re-reading.

        A ``tool_call_update`` replaces the fields it carries, so one bearing a
        non-empty ``rawInput`` or ``locations`` has revised what the call ran
        with. An *empty* ``rawInput`` is not a revision to nothing: every adapter
        measured sends one on the opening frame.
        """
        return bool(_dict(update.get("rawInput"))) or bool(_first_location(update))


__all__ = ["AcpDialect", "ToolCall", "ToolResult", "content_texts"]
