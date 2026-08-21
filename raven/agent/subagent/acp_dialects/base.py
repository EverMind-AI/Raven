"""Reading an ACP tool call the way the spec describes it.

An ACP adapter reports a tool call twice over: once machine-readably, in
``kind`` (one of ten spec values) and ``locations``, and once for a human, in
``title``. Only the first is comparable across adapters -- measured, the same
``kind: "execute"`` arrives titled ``"Terminal"`` from claude-agent-acp and
titled with the entire shell command from codex-acp -- so the ``kind`` is what
this maps and the title is kept only as the fallback for a call that carries no
argument at all.

What it maps *to* is a raven tool name (``exec``, ``read_file``, ...). That is
the whole point of the layer: a direct chat is rendered by the same code that
renders raven's own turns, and that code reads a tool name to choose a verb and
an argument to show beside it. Handing it an adapter's title put a 100-character
pipeline where a verb belongs.

Adapters that answer differently from the spec subclass this; see
:mod:`raven.agent.subagent.acp_dialects.claude_code` and
:mod:`raven.agent.subagent.acp_dialects.codex`. Anything not measured is left to
this class rather than guessed at per adapter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# ACP's ToolKind -> the raven tool whose rendering fits it. The three with no
# raven equivalent (`delete`, `move`, `switch_mode`) keep a descriptive
# snake_case name: the renderer humanises an unknown name into a verb ("move
# file"), so naming the action honestly reads better than forcing it onto a
# raven tool that does something else.
_KIND_TO_TOOL = {
    "read": "read_file",
    "edit": "edit_file",
    "delete": "delete_file",
    "move": "move_file",
    "search": "grep",
    "execute": "exec",
    "think": "think",
    "fetch": "web_fetch",
    "switch_mode": "switch_mode",
}

# The parameter each raven tool takes its subject in, so a normalised call's
# arguments read like that tool's own -- `exec` takes `command`, `read_file`
# takes `path`. The renderer looks the subject up by this name; a call whose
# tool is not listed falls back to the first string in its arguments.
ARGUMENT_KEY = {
    "exec": "command",
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "list_dir": "path",
    "delete_file": "path",
    "move_file": "path",
    "grep": "pattern",
    "find": "pattern",
    "web_fetch": "url",
    "web_search": "query",
}

# Keys an adapter's rawInput is known to carry the subject under, in the order
# they are preferred. Checked before the generic "first string value" sweep so a
# call carrying both a command and a cwd reports the command.
_SUBJECT_KEYS = ("command", "path", "file_path", "abs_path", "filePath", "pattern", "query", "url", "prompt")

_FALLBACK_TOOL = "tool_call"

# How much of a call fits in one label before it stops being one.
_LABEL_CHARS = 120


@dataclass(frozen=True)
class ToolCall:
    """One tool call, in raven's vocabulary.

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
        """The call's arguments, with the subject under raven's own key first.

        Insertion order is load-bearing: a reader that does not know this tool
        takes the first string value it finds, so the subject has to be it.

        A tool with a key of its own gets the subject renamed onto it, and the
        adapter's own spelling of the same value is then dropped rather than
        repeated (``{"path": x}``, not ``{"path": x, "filePath": x}``). A tool
        with no such key keeps the adapter's field names untouched -- inventing
        a generic one beside them would name the subject twice and say nothing
        the reader did not already have.
        """
        key = ARGUMENT_KEY.get(self.name)
        merged: dict[str, Any] = {}
        if self.subject and key is not None:
            merged[key] = self.subject

        for name, value in self.raw_input.items():
            if name in merged or value in (None, "", {}, []):
                continue
            if key is not None and value == self.subject:
                continue
            merged[name] = value

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
        kind = update.get("kind")
        if isinstance(kind, str) and kind in _KIND_TO_TOOL:
            return _KIND_TO_TOOL[kind]
        return _FALLBACK_TOOL

    def argument(self, update: dict[str, Any]) -> str:
        """The call's subject: what a reader needs beside the verb.

        ``locations`` is consulted before the generic sweep and after the named
        keys because it is the spec's own answer for a file-touching call --
        measured, codex-acp's ``read`` sends no ``rawInput`` at all and the path
        exists nowhere else on the frame.
        """
        raw = _dict(update.get("rawInput"))
        for key in _SUBJECT_KEYS:
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


__all__ = ["ARGUMENT_KEY", "AcpDialect", "ToolCall", "ToolResult", "content_texts"]
