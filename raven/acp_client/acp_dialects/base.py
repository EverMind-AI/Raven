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
:mod:`raven.acp_client.acp_dialects.claude_code` and
:mod:`raven.acp_client.acp_dialects.codex`. Anything not measured is left to
this class rather than guessed at per adapter.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_FALLBACK_TOOL = "tool_call"

# The kind an adapter sends for a call none of the other nine describe. It is
# the one value that names nothing, so a call badged with it is the only one
# worth looking elsewhere for a name for.
_UNSPECIFIC_KIND = "other"

# Where a raven-family adapter puts the tool's real name, on the terms
# claude-agent-acp puts its own in ``_meta.claudeCode.toolName``.
_RAVEN_TOOL_NAME = "raven.toolName"

# A leading identifier in a title, alone or ahead of ``": <subject>"`` -- the
# shape the ACP entrance's title_for builds. Anchored and bounded so
# prose cannot match: a title of "Ran the tests" has a space where this wants a
# colon or the end of the string.
_TITLE_NAME = re.compile(r"\A([A-Za-z_][A-Za-z0-9_.]*)(?::\s|\Z)")

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
class DialectResult:
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


def _named_by_title(update: dict[str, Any]) -> str | None:
    """The tool name a title states outright, if it states one.

    Last resort, and only for a call badged ``other``: a title is written for a
    person and most adapters spend it on prose. A raven-family adapter does not
    -- ``title_for`` writes ``"<name>: <subject>"`` -- so the one adapter whose
    ``kind`` table has fallen behind its tool list is still readable.
    """
    title = update.get("title")
    if not isinstance(title, str):
        return None
    match = _TITLE_NAME.match(title.strip())
    return match.group(1) if match else None


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

    plan_tool_name = "plan"
    """What to call the tool behind a ``sessionUpdate: "plan"`` frame."""

    def tool_name(self, update: dict[str, Any]) -> str:
        """The transport's own name for the call, at the finest grain it gives.

        The spec's ``kind`` verbatim: mapping it into raven's vocabulary is the
        read boundary's job (:mod:`raven.agent.subagent.tool_vocabulary`),
        because a record that renamed it could never be read back for what the
        agent actually ran.

        ``other`` is the exception, because it is the one kind that names
        nothing: a tool absent from the adapter's own kind table is badged with
        it, and the row then reads "other" for what was a ``glob``. A
        raven-family adapter states the real name on ``_meta`` and, on a build
        predating that, in its title -- both finer than the kind, and both
        still the transport's own vocabulary rather than a rename.
        """
        named = _dict(update.get("_meta")).get(_RAVEN_TOOL_NAME)
        if isinstance(named, str) and named:
            return named
        kind = update.get("kind")
        if not isinstance(kind, str) or not kind:
            return _FALLBACK_TOOL
        if kind != _UNSPECIFIC_KIND:
            return kind
        return _named_by_title(update) or kind

    def argument(self, update: dict[str, Any]) -> str:
        """The call's subject: what a reader needs beside the verb.

        ``locations`` is consulted before the generic sweep and after the named
        keys because it is the spec's own answer for a file-touching call --
        measured, codex-acp's ``read`` sends no ``rawInput`` at all and the path
        exists nowhere else on the frame.
        """
        from raven.agent.subagent.tool_vocabulary import SUBJECT_KEYS

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

    def result(self, update: dict[str, Any]) -> DialectResult:
        """What the call returned. ``content`` is what the adapter chose to show."""
        text = "".join(content_texts(update.get("content")))
        if not text:
            text = self._raw_output_text(update.get("rawOutput"))
        return DialectResult(text=text, ok=update.get("status") == "completed")

    @staticmethod
    def _raw_output_text(raw: Any) -> str:
        if isinstance(raw, str):
            return raw
        if raw is None:
            return ""
        return json.dumps(raw, ensure_ascii=False, default=str)

    def names_call(self, update: dict[str, Any]) -> bool:
        """Whether this frame carries enough to name the call it revises.

        A ``tool_call_update`` replaces the fields it carries, and codex-acp
        does not repeat ``kind`` on one. Re-reading the name from such a frame
        returned :data:`_FALLBACK_TOOL` and overwrote a name the opening frame
        had right -- measured, ``kind: "search"`` became ``tool_call``.
        """
        kind = update.get("kind")
        return isinstance(kind, str) and bool(kind)

    def subject_from_result(self, update: dict[str, Any], *, name: str | None = None) -> str | None:
        """A subject that exists only in the completed frame, if this adapter has one.

        ``name`` is the call's own name from its opening frame, offered because
        the completed frame this reads usually cannot supply it itself --
        measured, codex's own completing frames repeat none of ``tool_name``'s
        discriminators. An override that only applies to specific tools reads
        this to gate itself; an unnamed call is the safe default and returns
        ``None``, so a caller must supply the name to get any match at all.
        """
        return None

    def permission_command(self, params: dict[str, Any]) -> str | None:
        """The command a ``session/request_permission`` says the call will run.

        The spec puts it on the embedded tool call. Its value here is that an
        adapter may badge a call as something other than what it ran; this is
        the frame that still has the command.
        """
        command = _dict(_dict(params.get("toolCall")).get("rawInput")).get("command")
        return command.strip() if isinstance(command, str) and command.strip() else None

    def plan_rows(self, update: dict[str, Any]) -> tuple[str, str]:
        """One plan snapshot as a subject and a checklist.

        The frame is the whole list every time, so a reader shows the current
        state rather than a diff. The subject is the step being worked on,
        because that is the one thing a folded row can usefully say.
        """
        entries = update.get("entries")
        rows: list[str] = []
        current = ""
        for entry in entries if isinstance(entries, list) else []:
            fields = _dict(entry)
            content = fields.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            status = fields.get("status")
            mark = {"completed": "x", "in_progress": ">"}.get(status if isinstance(status, str) else "", " ")
            if mark == ">" and not current:
                current = content.strip()
            rows.append(f"[{mark}] {content.strip()}")
        return current or (f"{len(rows)} steps" if rows else ""), "\n".join(rows)

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

    def pair_fields(self, fields: list[Any]) -> list[Any]:
        """Merge properties an agent emits as one question.

        The spec pairs nothing, and this dialect stands in for every agent nobody
        has measured, so it pairs nothing on a guess: a property named
        ``<question>_custom`` may be a notes field of its own, and folding it away
        would drop an answer the form asked for. The one pairing it reads is the
        one stated on the property itself -- ``_meta.raven.customAnswerFor``
        naming the question -- which raven's own ACP server writes beside every
        ``ask_user`` with choices, so a person who types an answer of their own
        has it carried to the agent instead of being asked again until the form
        gives up.
        """
        return fold_pairs(fields, paired_by_marker(fields))


RAVEN_CUSTOM_FOR = "customAnswerFor"
"""The key under a property's ``_meta.raven`` naming the question it is the free-text box for."""


def paired_by_marker(fields: list[Any]) -> list[tuple[str, str]]:
    """(question, free-text sibling) for every property that declares its question."""
    by_name = {f.name: f for f in fields}
    pairs: list[tuple[str, str]] = []
    for f in fields:
        named = _dict(_dict(f.meta).get("raven")).get(RAVEN_CUSTOM_FOR)
        if isinstance(named, str) and named in by_name and named != f.name:
            pairs.append((named, f.name))
    return pairs


def paired_by_name(fields: list[Any]) -> list[tuple[str, str]]:
    """(question, free-text sibling) for every ``<name>_custom`` property present.

    A measured convention, not a spec one: only a dialect that has seen its agent
    write the pair this way (Claude Code does) may pair on the name.
    """
    by_name = {f.name: f for f in fields}
    return [(f.name, f"{f.name}_custom") for f in fields if f"{f.name}_custom" in by_name]


def fold_pairs(fields: list[Any], pairs: list[tuple[str, str]]) -> list[Any]:
    """Fold each free-text sibling into its question and drop it from the list.

    Every fold is decided before the list is rebuilt. Deciding one while walking
    the properties in order cannot see a sibling written ahead of its own
    question, and that one survives as a question of its own -- the same thing
    asked twice, with the free-text box as a bare prompt. A property takes part
    in at most one pairing, on either side; a sibling that is not a plain
    optional string, or a question without options, is left as it is.
    """
    by_name = {f.name: f for f in fields}
    folded: set[str] = set()
    paired: set[str] = set()
    for question_name, custom_name in pairs:
        question, custom = by_name.get(question_name), by_name.get(custom_name)
        if question is None or custom is None:
            continue
        if question_name in paired or custom_name in paired:
            continue
        if not question.options or custom.type != "string" or custom.options:
            continue
        if custom.required:
            # Folding keeps only the survivor's `required`, so a demanded sibling
            # would go missing from an accepted form. Asking twice is the lesser
            # cost of the two.
            continue
        question.custom_name = custom.name
        paired.update((question_name, custom.name))
        folded.add(custom.name)
    return [f for f in fields if f.name not in folded]


__all__ = [
    "AcpDialect",
    "ToolCall",
    "DialectResult",
    "content_texts",
    "fold_pairs",
    "paired_by_marker",
    "paired_by_name",
]
