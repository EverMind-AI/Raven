"""One ACP turn's ordered events, reduced from the frames that carried it.

Two readers turn a run's ``session/update`` frames into the same event list --
what the agent said, thought and ran, in the order it reported them: the live
collector (``acp_agent._TurnCollector``) while the turn is in flight, and the
journal replay (``raven.trajectory.frames_replay``) for a run read back later.
This is that reduction, in one place so the two cannot drift. The collector
adds only its side effects: the answer and its budget, the live view, and the
account of the files a call wrote.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from raven.acp_client.acp_dialects import AcpDialect, ToolCall, content_texts

# Text-bearing update kinds, and the ones whose text is the answer rather than
# commentary. Only `usage_update` was observed on a live server; the rest follow
# the ACP contract and are handled defensively -- an unrecognised shape is
# recorded raw and skipped, never guessed at.
ANSWER_UPDATES = ("agent_message_chunk",)
THOUGHT_UPDATES = ("agent_thought_chunk",)
# What the person said mid-turn. Only a steer produces one during a live turn:
# every other user message reached the agent as the prompt and is already on
# the caller's side, so this frame is the one record of a steer landing.
USER_UPDATES = ("user_message_chunk",)

# One synthetic id for the turn's plan. The frame carries no call id of its
# own, and a plan frame re-sends the whole list on every change -- five times
# for one plan in codex's capture, the only adapter measured sending one -- so
# a row per frame would be five near-identical rows. The branch this feeds is
# dialect-independent: any adapter's plan frame lands here.
PLAN_CALL_ID = "acp-plan"

_RESULT_TEXT_CAP = 2000


@dataclass(frozen=True)
class Applied:
    """What one update did, for a caller that drives side effects off it.

    ``texts`` are the chunk's text parts (an answer or a thought), ``steered``
    says a person's words landed mid-turn, and ``opened_plan`` says this update
    opened the turn's plan row rather than moving one already open.
    """

    texts: list[str] = field(default_factory=list)
    steered: bool = False
    opened_plan: bool = False


class TurnEvents:
    """The ordered events of one turn: says, thoughts, steers, calls and results.

    ``at`` is the caller's: the live collector stamps an update as it arrives,
    the replay with the journal's own timestamp for the frame.
    """

    def __init__(self, dialect: AcpDialect | None = None, prompt: str | None = None) -> None:
        # The spec by default, so a reader built without one (every test that
        # only cares about answer text) still reads tool calls correctly.
        self.dialect = dialect or AcpDialect()
        # The prompt this turn was opened with. A ``user_message_chunk`` that
        # repeats it is an agent echoing the question back (the spec uses the
        # frame for replays), not a steer, and must not become a user row.
        self.prompt = (prompt or "").strip()
        self.events: list[dict[str, Any]] = []

    @property
    def calls(self) -> list[ToolCall]:
        return [ev["call"] for ev in self.events if ev["t"] == "call"]

    def apply(self, update: dict[str, Any], at: str | None) -> Applied:
        """Fold one ``session/update`` into the event list."""
        kind = update.get("sessionUpdate")
        if kind in ANSWER_UPDATES:
            texts = content_texts(update.get("content"))
            said = "".join(texts)
            if said:
                # Merged with the previous burst only when nothing came between:
                # a `call` event in between is exactly what makes this a new
                # burst, and the boundary is what puts each one on the step it
                # was said before instead of at the end with the rest.
                if self.events and self.events[-1].get("t") == "say":
                    self.events[-1]["text"] += said
                else:
                    self.events.append({"t": "say", "text": said, "at": at})
            return Applied(texts=texts)
        if kind in USER_UPDATES:
            said = "".join(content_texts(update.get("content")))
            if said and said.strip() != self.prompt:
                self.events.append({"t": "user", "text": said, "at": at})
                return Applied(steered=True)
            return Applied()
        if kind in THOUGHT_UPDATES:
            chunks = content_texts(update.get("content"))
            for c in chunks:
                if self.events and self.events[-1].get("t") == "thought":
                    self.events[-1]["text"] += c
                else:
                    self.events.append({"t": "thought", "text": c, "at": at})
            return Applied(texts=chunks)
        if kind == "tool_call":
            call = self.dialect.call(update)
            if call.id and any(e.get("t") == "call" and e.get("id") == call.id for e in self.events):
                # A second opening frame for a call already announced. The spec
                # revises with `tool_call_update`, but codebuddy re-sends
                # `tool_call` once it knows the kind, and appending again
                # rendered one read as two rows with only one result to pair.
                # Treated as the revision it is, which also lets the finer
                # `kind` on this frame name the call.
                self._revise_call(update)
            else:
                self.events.append(
                    {"t": "call", "id": call.id or f"call-{len(self.calls) + 1}", "call": call, "at": at}
                )
            return Applied()
        if kind == "tool_call_update":
            self._revise_call(update)
            if update.get("status") in ("completed", "failed"):
                outcome = self.dialect.result(update)
                self.events.append(
                    {
                        "t": "result",
                        "id": str(update.get("toolCallId") or ""),
                        "ok": outcome.ok,
                        "text": outcome.text[:_RESULT_TEXT_CAP],
                        "at": at,
                    }
                )
                self._backfill_subject(str(update.get("toolCallId") or ""), update)
            return Applied()
        if kind == "plan":
            return Applied(opened_plan=self._plan(update, at))
        return Applied()

    def permission(self, params: dict[str, Any]) -> str:
        """Fold one ``session/request_permission`` in; the id of the call it asks about.

        Subject only, never the name: this frame reports `kind: "execute"` even
        for a call the session update badged `read`, and reading it for a name
        would erase the distinction codex drew.
        """
        command = self.dialect.permission_command(params)
        tool = params.get("toolCall")
        call_id = str(tool.get("toolCallId") or "") if isinstance(tool, dict) else ""
        if command and call_id:
            self._backfill_permission_command(call_id, command)
        return call_id

    def _revise_call(self, update: dict[str, Any]) -> None:
        """Re-read a call's arguments from a later frame.

        The opening ``tool_call`` announces the call before the arguments are
        known: measured on claude-agent-acp and opencode, it carries
        ``rawInput: {}`` and the real input follows on a ``tool_call_update``.
        Until that lands, the only thing a reader has is the title -- which is
        why the fallback to it is resolved at render time (``ToolCall.subject``)
        rather than stored here.

        The newest input wins, because that is what a ``tool_call_update``
        means: the fields it carries replace the call's current ones, so an
        adapter that revises an argument has revised what the tool actually ran
        with. The whole call is re-parsed rather than only its input, since the
        same frame also carries the ``kind`` and ``_meta`` a dialect names the
        tool from -- but only when it does: ``names_call`` is what decides, and
        a frame that answers False leaves the name alone.
        """
        if not self.dialect.revises_call(update):
            return
        call_id = str(update.get("toolCallId") or "")
        for event in reversed(self.events):
            if event.get("t") == "call" and event.get("id") == call_id:
                revised = self.dialect.call(update)
                previous: ToolCall = event["call"]
                event["call"] = ToolCall(
                    id=previous.id,
                    # Guarded like the fields below it: a frame that carries no
                    # kind cannot name the call, and reading one anyway is what
                    # renamed codex's searches to the fallback.
                    name=revised.name if self.dialect.names_call(update) else previous.name,
                    argument=revised.argument or previous.argument,
                    # An update need not repeat the title, and losing it would
                    # leave a no-argument call with nothing to show at all.
                    title=revised.title or previous.title,
                    raw_input=revised.raw_input or previous.raw_input,
                )
                return

    def _backfill_subject(self, call_id: str, update: dict[str, Any]) -> None:
        """Set a call's subject from its own result.

        The frame that opened the call did not have it: codex sends
        ``apply_patch`` as the command and names the files it patched only in
        the output. Never overwrites a subject already known -- a later frame
        revising an argument is ``_revise_call``'s business, not this.

        Passes the call's own name, established when it opened, into
        ``subject_from_result`` as the tool the completed frame belongs to: that
        frame typically carries none of ``tool_name``'s own discriminators, so
        without it a dialect could not tell an ``apply_patch`` completion from
        any other tool's own -- including one whose output merely looks like an
        apply_patch envelope.
        """
        for event in reversed(self.events):
            if event.get("t") == "call" and event.get("id") == call_id:
                previous: ToolCall = event["call"]
                if previous.argument:
                    return
                subject = self.dialect.subject_from_result(update, name=previous.name)
                if not subject:
                    return
                # The new subject must win a `path` collision -- an
                # `imageGeneration` call's own `rawInput` can already carry one
                # -- so it seeds the dict and `previous.raw_input` is copied in
                # behind it, the same order `CodexDialect.call` merges with.
                raw_input = {"path": subject}
                for key, value in previous.raw_input.items():
                    if key == "path":
                        continue
                    # `apply_patch`'s own `command` is the tool's name, not an
                    # argument -- a reader that picks a subject by scanning
                    # known key names rather than by position (as
                    # `ui-tui`'s `callSubject` does) would otherwise take it
                    # over the subject just recovered here. A real recovered
                    # command never equals the call's own name, so this never
                    # drops one.
                    if key == "command" and value == previous.name:
                        continue
                    raw_input[key] = value
                event["call"] = ToolCall(
                    id=previous.id,
                    name=previous.name,
                    argument=subject,
                    title=previous.title,
                    raw_input=raw_input,
                )
                return

    def _backfill_permission_command(self, call_id: str, command: str) -> None:
        """Set a call's subject to the command a permission request recovered.

        Guarded on ``raw_input`` already carrying a ``command`` key rather than
        on ``argument`` being non-empty: a ``commandExecution.read`` opens with
        its ``locations`` path already filling ``argument``, and that
        placeholder is exactly what this must replace. A real
        ``commandExecution`` -- and ``apply_patch``, whose ``rawInput.command``
        is its own name -- already carry their command there, and overwriting
        either would either restate the same value or, for ``apply_patch``,
        block the file-derived subject its own result still owes it.
        """
        for event in reversed(self.events):
            if event.get("t") == "call" and event.get("id") == call_id:
                previous: ToolCall = event["call"]
                if previous.raw_input.get("command"):
                    return
                event["call"] = ToolCall(
                    id=previous.id,
                    name=previous.name,
                    argument=command,
                    title=previous.title,
                    raw_input={"command": command, **previous.raw_input},
                )
                return

    def _plan(self, update: dict[str, Any], at: str | None) -> bool:
        """Open the turn's plan row, or move the one already open; True when it opened.

        Only the opening frame breaks the message. A revision that broke it too
        would split the narration around a plan that changes four times.
        """
        subject, checklist = self.dialect.plan_rows(update)
        if not checklist:
            return False
        call = ToolCall(
            id=PLAN_CALL_ID,
            name=self.dialect.plan_tool_name,
            argument=subject,
            title="",
            raw_input={"argument": subject} if subject else {},
        )
        opened = False
        for event in self.events:
            if event.get("t") == "call" and event.get("id") == PLAN_CALL_ID:
                event["call"] = call
                break
        else:
            opened = True
            self.events.append({"t": "call", "id": PLAN_CALL_ID, "call": call, "at": at})
        for event in self.events:
            if event.get("t") == "result" and event.get("id") == PLAN_CALL_ID:
                event["text"] = checklist
                return opened
        self.events.append({"t": "result", "id": PLAN_CALL_ID, "ok": True, "text": checklist, "at": at})
        return opened


__all__ = ["ANSWER_UPDATES", "PLAN_CALL_ID", "THOUGHT_UPDATES", "USER_UPDATES", "Applied", "TurnEvents"]
