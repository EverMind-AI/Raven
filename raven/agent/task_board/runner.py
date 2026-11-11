"""The post-turn call: once a host turn has answered, its model keeps the Task Board.

One extra call, after the reply has gone out: the turn's own window and tools
array plus one instruction asking for a ``task_board`` call. The instruction ends
with the sentences of the reply that end in a question mark (:func:`asked`), for
the model to sort onto the steps of the matters they ask about: a reply that also
started work too often went on the board as running, its question left off.
Nothing forces the tool -- the choice stays the turn's own -- so an answer in text
is asked again, unless its whole text is the call written out, which is read as
the call. The arguments are checked whole by :func:`apply_update`; a refusal goes
back to the model with every fault it found, up to :data:`MAX_ATTEMPTS` times.
When every attempt is refused, the last update the model sent is still written as
far as the board can take it -- see :func:`salvage` -- and only a fault in a step
or in where its work goes leaves no update to be had. Then the turn's dispatches
are parked in ``unassigned`` and the next update has to place them first -- a
dispatch is never silently dropped. A turn that failed or was stopped makes no
call, and what it dispatched is parked the same way, in its place in the update
queue (:func:`park_dispatches`). A turn known to be about some groups -- a
delegated result's, a Move on's -- has its update confined to them, to wherever
it places its dispatches and to each step that asks the user something, before it
is checked; one that also took in a message the user sent while it ran is not
confined. Each refusal is logged, and each update that never landed.

When the update wrote a step over one whose questions the user never answered,
one more call follows, continuing from the update's own exchange: it decides which
of those questions are still open and records the complete list on the newest
step, so a turn that only explains something cannot bury a pending question.
Updates of one session run one at a time, so the next turn's update is built
on this one's result. Nothing here touches the session's messages.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from typing import Any, NamedTuple

from loguru import logger

from raven.agent.task_board.board import (
    Board,
    Dispatch,
    Pending,
    TaskGroup,
    UpdateRejectedError,
    apply_carry,
    apply_update,
    board_to_dict,
    confine_update,
    owning_groups,
    salvage,
)
from raven.agent.task_board.dispatches import call_target
from raven.agent.task_board.service import TaskBoardService
from raven.agent.task_board.tool import TOOL_NAME
from raven.contracts.llm_provider import LLMResponse
from raven.i18n import prompt

MAX_ATTEMPTS = 3
OPEN_STEPS_SHOWN = 2
ASKED_SHOWN = 8
TEMPLATE = "task_board_update"
SCOPE_TEMPLATE = "task_board_scope"
SCOPE_MID_TURN_TEMPLATE = "task_board_scope_mid_turn"
ASKED_TEMPLATE = "task_board_asked"
CARRY_TEMPLATE = "task_board_carry"

_PROSE = "Answer with a task_board call (action=update), not with text."
_NOT_UPDATE = "Only action=update is accepted here; the board is already in the instruction above."
_REFUSED = (
    "Rejected, and nothing was written. Call task_board again with action=update, sending the whole update with"
    " each of these fixed:"
)
_CARRY_PROSE = "Answer with a task_board call (action=carry), not with text."
_NOT_CARRY = "Only action=carry is accepted here; the board is already in the instruction above."
_CARRY_REFUSED = (
    "Rejected, and nothing was written. Call task_board again with action=carry, sending the whole answer with"
    " each of these fixed:"
)
_ACCEPTED = "The update was accepted and written to the board."


def should_run(status: str, dispatches: Sequence[Dispatch], *, silent: bool = False) -> bool:
    """Only a completed turn makes the call: none follows a provider that just failed, nor a
    turn that was stopped or cut short, and what such a turn dispatched is parked instead
    (``park_dispatches``). A completed turn keeps the board unless it was a Silent Turn
    that dispatched nothing: the host saying the turn has nothing to show leaves no
    progress to record, while work it started still needs placing."""
    return status == "completed" and (bool(dispatches) or not silent)


def _snapshot(board: Board) -> str:
    if not board.groups:
        return "(the board is empty)"
    lines: list[str] = []
    for group in sorted((g for g in board.groups if g.status != "completed"), key=lambda g: g.created_at):
        lines.append(f"- {group.id} [{group.status}] {group.title}" + (f" -- {group.summary}" if group.summary else ""))
        lines.extend(f"    - {step.title}" for step in group.steps[-OPEN_STEPS_SHOWN:])
    done = sorted((g for g in board.groups if g.status == "completed"), key=lambda g: g.created_at)
    if done:
        lines.append("Completed: " + "; ".join(f"{g.id}: {g.title}" for g in done))
    return "\n".join(lines)


def _handles(board: Board, dispatches: Sequence[Dispatch]) -> str:
    pending = {p.handle for p in board.unassigned}
    lines = [f"- {p.handle} -- {p.summary} (left over from an earlier turn)" for p in board.unassigned]
    lines += [f"- {d.handle} -- {d.summary}" for d in dispatches if d.handle not in pending]
    return "\n".join(lines) or "(none)"


def _scope(owners: Sequence[str], mid_turn: bool = False) -> str:
    # Ids alone: a title is text an earlier call wrote, and only the record is fenced.
    if not owners:
        return ""
    template = SCOPE_MID_TURN_TEMPLATE if mid_turn else SCOPE_TEMPLATE
    return f"\n{prompt(template).strip().format(groups=', '.join(owners))}\n"


# A backtick fence's info string holds no backtick (CommonMark), so a line that opens
# with a triple-backtick code span is prose rather than a fence.
_FENCE = re.compile(r"^(`{3,}(?=[^`]*$)|~{3,})(.*)$")
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_FULL_MARKS = "\u3002\uff01\uff1f"
_SENTENCE_MARK = re.compile(r"[\u3002\uff01\uff1f.!?]")
_ASCII_CLOSERS = frozenset("\"')]}>*_")
_CLOSER_CACHE: dict[str, bool] = {}
_LIST_MARK = re.compile(r"^(?:[-*+\u2022]\s+|\d+[.)]\s+|\d+\u3001\s*)")
_QUESTION_MARKS = ("?", "\uff1f")
_QUESTION_MARK_CHARS = "".join(_QUESTION_MARKS)
_EXCLAMATION_MARKS = "!\uff01"
_QUESTION_RECORD_MARKER = re.compile(r"\[(?:BEGIN|END) QUESTION MARKS\]", re.IGNORECASE)
_RECOVERY_MARK = "_recovery_synthetic"


def _without_strong(text: str, marker: str) -> str:
    """Remove paired strong markers in one forward scan, keeping unpaired markers and identifier underscores."""
    out: list[str] = []
    opening: int | None = None
    copied = position = 0
    underscore = marker == "__"
    while (position := text.find(marker, position)) >= 0:
        end = position + len(marker)
        if opening is None:
            if (
                end < len(text)
                and not text[end].isspace()
                and (not underscore or position == 0 or not (text[position - 1].isalnum() or text[position - 1] == "_"))
            ):
                opening = position
                position = end + 1
                continue
        elif not text[position - 1].isspace() and (
            not underscore or end == len(text) or not (text[end].isalnum() or text[end] == "_")
        ):
            out.extend((text[copied:opening], text[opening + len(marker) : position]))
            copied = end
            opening = None
            position = end
            continue
        position += 1
    out.append(text[copied:])
    return "".join(out)


def _without_emphasis(text: str) -> str:
    """The text without its strong emphasis markers, inline code and single emphasis left as
    written: a marker inside a backtick span or an identifier is content, not formatting."""
    pieces = _INLINE_CODE.split(text)
    code = _INLINE_CODE.findall(text)
    out: list[str] = []
    for index, piece in enumerate(pieces):
        out.append(_without_strong(_without_strong(piece, "**"), "__"))
        if index < len(code):
            out.append(code[index])
    return "".join(out)


def _paragraphs(reply: str) -> list[str]:
    """The reply's prose, one entry per paragraph: soft-wrapped lines joined, and a blank
    line, a list item or a quoted line starting a new one. A fenced code block -- backtick
    or tilde, closed or not -- is left out."""
    paragraphs: list[str] = []
    current: list[str] = []
    fence: tuple[str, int] | None = None

    def flush() -> None:
        if current:
            paragraphs.append(" ".join(current))
            current.clear()

    for line in reply.splitlines():
        text = line.strip()
        marker = _FENCE.match(text)
        if fence is not None:
            if (
                marker
                and marker.group(1)[0] == fence[0]
                and len(marker.group(1)) >= fence[1]
                and not marker.group(2).strip()
            ):
                fence = None
            continue
        if marker:
            fence = (marker.group(1)[0], len(marker.group(1)))
            flush()
            continue
        if not text or line.lstrip().startswith(">"):
            flush()
        elif _LIST_MARK.match(text):
            flush()
            current.append(text)
        else:
            current.append(text)
    flush()
    return paragraphs


def _is_closer(char: str) -> bool:
    """A mark that closes the sentence before it: a closing bracket or final quote in Unicode
    (``Pe`` / ``Pf``), the strong markers, the ASCII quotes, or a half-width ``>``."""
    cached = _CLOSER_CACHE.get(char)
    if cached is None:
        if char in _ASCII_CLOSERS:
            cached = True
        elif char < "\x80":
            cached = False
        else:
            cached = unicodedata.category(char) in {"Pe", "Pf"}
        _CLOSER_CACHE[char] = cached
    return cached


def _rstrip_closers(text: str) -> str:
    end = len(text)
    while end and _is_closer(text[end - 1]):
        end -= 1
    return text[:end]


def _rstrip_exclamations(text: str) -> str:
    end = len(text)
    while end and text[end - 1] in _EXCLAMATION_MARKS:
        end -= 1
    return text[:end]


def _without_exclamation_after_question(sentence: str) -> str:
    """The sentence without the exclamation marks that follow its final question mark,
    closing marks after them kept: a full-width pair keeps its question mark only."""
    if max(sentence.rfind(mark) for mark in _EXCLAMATION_MARKS) < max(sentence.rfind(mark) for mark in _QUESTION_MARKS):
        return sentence
    end = len(sentence)
    while end and _is_closer(sentence[end - 1]):
        end -= 1
    start = end
    while start and sentence[start - 1] in _EXCLAMATION_MARKS:
        start -= 1
    if start == end or start == 0 or sentence[start - 1] not in _QUESTION_MARKS:
        return sentence
    return sentence[:start] + sentence[end:]


def _sentences(paragraph: str) -> list[str]:
    """The paragraph's sentences: a full-width mark ends one after any closing marks, and an
    ASCII mark ends one only when whitespace follows, the closing marks included."""
    out: list[str] = []
    start = cursor = 0
    end_of_text = len(paragraph)
    while (match := _SENTENCE_MARK.search(paragraph, cursor)) is not None:
        index = match.start()
        end = index + 1
        while end < end_of_text and _is_closer(paragraph[end]):
            end += 1
        if paragraph[index] in _FULL_MARKS:
            out.append(paragraph[start:end])
            start = cursor = end
            continue
        if end < end_of_text and paragraph[end].isspace():
            out.append(paragraph[start:end])
            while end < end_of_text and paragraph[end].isspace():
                end += 1
            start = cursor = end
            continue
        cursor = end
    if start < end_of_text:
        out.append(paragraph[start:])
    return out


def _questions(message: str) -> list[str]:
    found: list[str] = []
    for paragraph in _paragraphs(message):
        for part in _sentences(_without_emphasis(paragraph)):
            sentence = _LIST_MARK.sub("", part.strip()).strip()
            closed = _rstrip_exclamations(_rstrip_closers(sentence))
            if closed.endswith(_QUESTION_MARKS) and closed.rstrip(_QUESTION_MARK_CHARS + _EXCLAMATION_MARKS).strip():
                found.append(_without_exclamation_after_question(sentence))
    return found


def asked(reply: str | Sequence[str]) -> list[str]:
    """The sentences of a reply that end in a question mark, each once, the last ``ASKED_SHOWN`` of them.

    A question mark still ends one when closing emphasis, a bracket or a quote follows it, and
    an exclamation mark after it still leaves a question, dropped the way a full-width pair
    drops it. A reply given as its messages is read one message at a time, the way the reader
    was shown it: a line with no closing mark, or a fence left open, ends with its message. A
    fenced code block and a quoted line are left out: neither was put to the user.
    """
    messages = [reply] if isinstance(reply, str) else reply
    return list(dict.fromkeys(q for message in messages for q in _questions(message)))[-ASKED_SHOWN:]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _message_tool_words(call: Any, answered: Collection[str]) -> str:
    """What a message call that answered this conversation sent, if it is that call --
    forwarded through ``tool_call`` or not. Only this tool's argument is words put to the
    user; another tool's arguments hold code, URLs or paths."""
    if not isinstance(call, Mapping) or str(call.get("id") or "") not in answered:
        return ""
    name, arguments = call_target(call)
    if name != "message":
        return ""
    content = arguments.get("content")
    return content.strip() if isinstance(content, str) else ""


# The loop writes this assistant text only to keep a tool result from being followed by a
# user message (a 400 on most providers); nobody read it as the reply. `turn_path.py` writes
# it for the post-tool nudge and for a reply cut at the output ceiling with nothing in it.
_EMPTY_PLACEHOLDER = "(empty)"


def _reply(rows: Sequence[dict], answered: Collection[str]) -> list[str]:
    """The turn's reply, one entry per message the reader was shown: every assistant message
    among ``rows``, in the order it wrote them, and what each message call that answered this
    conversation sent. The rows are the turn's own (``TurnLog.added``), so a user message among
    them was sent while the turn ran and does not open a new one. What the loop wrote to get an
    answer going again (``_recovery_synthetic``: the part of an answer a cut stream left, or an
    empty placeholder) opens the entry of the message that follows it as a soft-wrapped line, the
    way the reader saw a cut answer and its continuation as one; the placeholder is not part of
    the reply."""
    entries: list[str] = []
    carried: list[str] = []
    for message in rows:
        if message.get("role") != "assistant":
            continue
        text = _text(message.get("content")).strip()
        if message.get(_RECOVERY_MARK):
            if text and text != _EMPTY_PLACEHOLDER:
                carried.append(text)
            continue
        if text:
            carried.append(text)
        if carried:
            entries.append("\n".join(carried))
            carried = []
        for call in message.get("tool_calls") or ():
            if sent := _message_tool_words(call, answered):
                entries.append(sent)
    if carried:
        entries.append("\n".join(carried))
    return entries


def _asked_block(questions: Sequence[str]) -> str:
    listed: list[str] = []
    for question in questions:
        cleaned = " ".join(_QUESTION_RECORD_MARKER.sub("", question).split())
        if cleaned.rstrip(_QUESTION_MARK_CHARS).strip():
            listed.append(cleaned)
    if not listed:
        return ""
    return "\n\n" + prompt(ASKED_TEMPLATE).strip().format(questions="\n".join(f"- {q}" for q in listed))


def build_instruction(
    board: Board,
    dispatches: Sequence[Dispatch],
    owners: Sequence[str] = (),
    *,
    asked: Sequence[str] = (),
    mid_turn: bool = False,
) -> str:
    return prompt(TEMPLATE).format(
        board=_snapshot(board),
        dispatches=_handles(board, dispatches),
        scope=_scope(owners, mid_turn),
        asked=_asked_block(asked),
    )


def carry_candidates(before: Board, after: Board, turn_at: str) -> list[tuple[TaskGroup, TaskGroup]]:
    """The groups whose latest step already asked something and this turn updated.

    A question is open only while it sits on the group's latest step and the group is
    ``needs_you`` -- the rule recall reads -- so ``before`` is read both ways.
    ``after`` says which of those groups the turn touched -- a step written, or the
    group's own title, summary or status rewritten -- and each pair is that group as
    it stood and as the update left it.
    """
    prior = {
        group.id: group
        for group in before.groups
        if group.status == "needs_you" and group.steps and group.steps[-1].questions
    }
    waiting = []
    for group in after.groups:
        old = prior.get(group.id)
        if old is None:
            continue
        newest = group.steps[-1] if group.steps else None
        touched = (
            (newest is not None and newest.turn_at == turn_at)
            or len(group.steps) != len(old.steps)
            or (group.title, group.summary, group.status) != (old.title, old.summary, old.status)
        )
        if touched:
            waiting.append((old, group))
    return waiting


def _carry_record(groups: Sequence[tuple[TaskGroup, TaskGroup]]) -> str:
    lines: list[str] = []
    for before, after in groups:
        asked, newest = before.steps[-1], after.steps[-1]
        lines.append(f"- {after.id} {after.title}")
        lines.append(f"    the step that asked: {asked.title} -- " + "; ".join(asked.questions))
        lines.append(f"    the step now newest: {newest.title} ({after.status})")
        lines.append("    questions it lists: " + ("; ".join(newest.questions) or "(none)"))
    return "\n".join(lines)


def build_carry_instruction(groups: Sequence[tuple[TaskGroup, TaskGroup]]) -> str:
    return prompt(CARRY_TEMPLATE).format(groups=_carry_record(groups))


def _still_open(waiting: Sequence[tuple[TaskGroup, TaskGroup]], board: Board) -> list[str]:
    """The groups whose asked step and status the reader has not touched since the preview.

    The question pass is a model round trip, and the reader can set a question aside
    or complete the group while it runs; writing the model's list back would undo that.
    """
    groups = {group.id: group for group in board.groups}
    kept = []
    for before, group in waiting:
        asked = before.steps[-1]
        current = groups.get(group.id)
        step = next((s for s in current.steps if s.id == asked.id), None) if current is not None else None
        if step is not None and step.questions == asked.questions and current.status == group.status:
            kept.append(group.id)
    return kept


class _Call(NamedTuple):
    id: str
    arguments: Any
    written: bool


def _the_call(response: LLMResponse, action: str, written_id: str) -> _Call | None:
    """The ``task_board`` call a reply made, or the one it wrote out as its whole text instead
    -- a JSON object naming ``action``, bare or in a fence -- filed under ``written_id``;
    ``None`` for a reply that did neither."""
    made = next((c for c in response.tool_calls if c.name == TOOL_NAME), None)
    if made is not None:
        return _Call(made.id, made.arguments, False)
    text = (response.content or "").strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].removeprefix("json").strip()
    try:
        args = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(args, dict) or args.get("action") != action:
        return None
    return _Call(written_id, args, True)


def _assistant_row(response: LLMResponse, call: _Call) -> dict[str, Any]:
    # A call written out stands as the call alone: its text is the call, and an attempt
    # shown its own text again tends to write it out again.
    row: dict[str, Any] = {
        "role": "assistant",
        "content": "" if call.written else response.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": TOOL_NAME, "arguments": json.dumps(call.arguments)},
            }
        ],
    }
    if response.reasoning_content:
        row["reasoning_content"] = response.reasoning_content
    if response.thinking_blocks:
        row["thinking_blocks"] = response.thinking_blocks
    return row


def _refusal(exc: UpdateRejectedError) -> str:
    return "\n".join([_REFUSED, *(f"{n}. {problem}" for n, problem in enumerate(exc.problems, 1))])


def _carry_refusal(exc: UpdateRejectedError) -> str:
    return "\n".join([_CARRY_REFUSED, *(f"{n}. {problem}" for n, problem in enumerate(exc.problems, 1))])


def _faults(exc: UpdateRejectedError) -> str:
    return " | ".join(map(str, exc.problems))


def _refused(session_key: str, attempt: int, why: str, *, what: str = "update") -> None:
    logger.info("Task board {} for {}, attempt {} of {}, refused: {}", what, session_key, attempt, MAX_ATTEMPTS, why)


def _read_as_call(session_key: str, attempt: int, *, what: str = "update") -> None:
    logger.info(
        "Task board {} for {}, attempt {} of {}, answered in text; read as its call",
        what,
        session_key,
        attempt,
        MAX_ATTEMPTS,
    )


def _as_placed(service: TaskBoardService, session_key: str, board: Board, handles: Sequence[str]) -> list[str]:
    """Delegated runs as the board placed them: a replan's successor and a later stint
    round were never placed themselves, and belong to the run they descend from."""
    if not any(handle.startswith("dag:") for handle in handles):
        return list(handles)
    lineage = service.lineage(session_key, board)
    placed = []
    for handle in handles:
        kind, _, ident = handle.partition(":")
        placed.append(f"dag:{lineage.get(ident, ident)}" if kind == "dag" else handle)
    return placed


def _park(board: Board, dispatches: Sequence[Dispatch], turn_at: str, turn_id: str | None) -> Board:
    pending = {p.handle for p in board.unassigned}
    fresh = [Pending(d.handle, d.summary, turn_at, turn_id) for d in dispatches if d.handle not in pending]
    return Board(board.groups, board.unassigned + fresh, board.version) if fresh else board


async def _ask(
    board: Board,
    *,
    session_key: str,
    call: Callable[[list[dict]], Awaitable[LLMResponse]],
    window: list[dict],
    dispatches: list[Dispatch],
    turn_at: str,
    turn_id: str | None,
    now_ms: Callable[[], int],
    owners: list[str],
    reply_rows: Sequence[dict],
    mid_turn: bool = False,
    answered_message_calls: Collection[str] = (),
) -> tuple[dict[str, Any], bool, list[dict]] | None:
    """Up to MAX_ATTEMPTS asks; the arguments of an update the board accepted, whether
    they are a salvage, whose text past a limit stands, and the exchange the asks left
    -- the question pass continues from it. ``None`` when no update was had.

    A turn that belongs to ``owners`` has its update confined to them before it is
    checked, so what would be dropped anyway never costs a refusal -- unless it took in a
    message the user sent while it ran (``mid_turn``): only the model can tell which
    matter that message was about, so the instruction names it and nothing is dropped.
    When the last attempt is refused too, the last update sent is salvaged: its work is
    placed and its steps written unless a fault lies there.
    """
    confined_to = () if mid_turn else owners
    instruction = build_instruction(
        board,
        dispatches,
        owners,
        asked=asked(_reply(reply_rows, answered_message_calls)),
        mid_turn=mid_turn,
    )
    rows = list(window) + [{"role": "user", "content": instruction}]
    last: dict[str, Any] | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await call(rows)
        except Exception as exc:
            logger.warning("Task board update for {} failed at the provider: {}", session_key, exc)
            return None
        if response.finish_reason == "error":
            logger.warning("Task board update for {} failed at the provider: {}", session_key, response.content)
            return None
        tool_call = _the_call(response, "update", f"written_update_{attempt}")
        if tool_call is None:
            _refused(session_key, attempt, "answered in text")
            rows += [{"role": "assistant", "content": response.content or ""}, {"role": "user", "content": _PROSE}]
            continue
        if tool_call.written:
            _read_as_call(session_key, attempt)
        args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        if args.get("action") != "update":
            _refused(session_key, attempt, f"called action={args.get('action')}")
            result = _NOT_UPDATE
        else:
            confined = confine_update(args, confined_to)
            try:
                apply_update(board, confined, dispatches, turn_at=turn_at, turn_id=turn_id, now_ms=now_ms())
            except UpdateRejectedError as exc:
                _refused(session_key, attempt, _faults(exc))
                last = confined
                result = _refusal(exc)
            else:
                if confined != args:
                    logger.info(
                        "Task board update for {} kept to {}; the rest was about other matters",
                        session_key,
                        confined_to,
                    )
                rows += [
                    _assistant_row(response, tool_call),
                    {"role": "tool", "tool_call_id": tool_call.id, "name": TOOL_NAME, "content": _ACCEPTED},
                ]
                return confined, False, rows
        rows += [
            _assistant_row(response, tool_call),
            {"role": "tool", "tool_call_id": tool_call.id, "name": TOOL_NAME, "content": result},
        ]
    if last is None:
        return None
    kept = salvage(board, last)
    try:
        apply_update(board, kept, dispatches, turn_at=turn_at, turn_id=turn_id, now_ms=now_ms(), allow_long=True)
    except UpdateRejectedError:
        return None
    return kept, True, rows


async def _ask_carry(
    board: Board,
    waiting: Sequence[tuple[TaskGroup, TaskGroup]],
    *,
    session_key: str,
    call: Callable[[list[dict]], Awaitable[LLMResponse]],
    exchange: list[dict],
    turn_at: str,
    now_ms: Callable[[], int],
) -> dict[str, Any] | None:
    """Up to MAX_ATTEMPTS asks for the questions a group's latest step keeps.

    It continues from ``exchange``, the update call's own rows, so the model reads
    what it just recorded before it decides which earlier questions are still open.
    ``None`` when no answer was had: the update then stands as it was written.
    """
    eligible = [group.id for _before, group in waiting]
    rows = list(exchange) + [{"role": "user", "content": build_carry_instruction(waiting)}]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await call(rows)
        except Exception as exc:
            logger.warning("Task board questions for {} failed at the provider: {}", session_key, exc)
            return None
        if response.finish_reason == "error":
            logger.warning("Task board questions for {} failed at the provider: {}", session_key, response.content)
            return None
        tool_call = _the_call(response, "carry", f"written_carry_{attempt}")
        if tool_call is None:
            _refused(session_key, attempt, "answered in text", what="questions")
            rows += [
                {"role": "assistant", "content": response.content or ""},
                {"role": "user", "content": _CARRY_PROSE},
            ]
            continue
        if tool_call.written:
            _read_as_call(session_key, attempt, what="questions")
        args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        if args.get("action") != "carry":
            _refused(session_key, attempt, f"called action={args.get('action')}", what="questions")
            result = _NOT_CARRY
        else:
            try:
                apply_carry(board, args, eligible, turn_at=turn_at, now_ms=now_ms())
            except UpdateRejectedError as exc:
                _refused(session_key, attempt, _faults(exc), what="questions")
                result = _carry_refusal(exc)
            else:
                return args
        rows += [
            _assistant_row(response, tool_call),
            {"role": "tool", "tool_call_id": tool_call.id, "name": TOOL_NAME, "content": result},
        ]
    return None


async def park_dispatches(
    service: TaskBoardService,
    *,
    session_key: str,
    dispatches: Sequence[Dispatch],
    turn_at: str,
    turn_id: str | None,
    generation: int,
) -> Board | None:
    """Park what a turn that failed or was stopped dispatched, for the next update to place.

    No call follows a provider that just failed, nor a stop the reader asked for. The park
    waits in the update queue as an update would: an update still in its call is checked,
    at its last step, against every handle the board holds unplaced, and one parked beside
    it would be a handle it was never told of. A clear or an undo since the turn ended
    leaves nothing to park.
    """
    async with service.queue(session_key), service.lock(session_key):
        if service.generation(session_key) != generation or service.was_dropped(session_key, turn_at):
            return None
        try:
            board = service.load(session_key)
            parked = _park(board, dispatches, turn_at, turn_id)
            if parked is not board:
                logger.warning(
                    "Task board for {}: the turn failed or was stopped; {} dispatch(es) parked for the next turn",
                    session_key,
                    len(parked.unassigned) - len(board.unassigned),
                )
                await service.write(session_key, parked)
            return parked
        except Exception:
            logger.opt(exception=True).warning("Task board for {} could not be written", session_key)
            return None


async def run_post_turn(
    service: TaskBoardService,
    *,
    session_key: str,
    call: Callable[[list[dict]], Awaitable[LLMResponse]],
    window: list[dict],
    dispatches: list[Dispatch],
    turn_at: str,
    turn_id: str | None,
    now_ms: Callable[[], int],
    generation: int,
    reply_rows: Sequence[dict],
    delegated: Sequence[str] = (),
    moved_on: Sequence[str] = (),
    mid_turn: bool = False,
    answered_message_calls: Collection[str] = (),
) -> Board | None:
    """Keep the board for the turn that just ended; the board written, or ``None`` when no update was had.

    The model is asked under the session's update queue only; the write lock is
    taken for the last step, on a fresh read of the board, so the reader's own
    actions never wait on the call. Whatever goes wrong on the way -- the
    provider, the model's arguments, the disk -- ends with this turn's dispatches
    parked rather than with an exception in a background task nobody awaits,
    unless a clear or an undo removed the turn meanwhile: then nothing is written.

    ``generation`` is the service's count of clears as the turn ended, read by
    the caller when it schedules this update: an update still waiting for the
    queue when a clear lands belongs to the cleared conversation too.

    ``delegated`` names the run whose result opened the turn -- read as the run the
    board placed when it descends from one -- and ``moved_on`` the groups the
    reader's Move on named: the groups they resolve to are the
    ones the turn belongs to, and its update is confined to them, to wherever
    it places the work it started and to each step that asks the user something.
    They are resolved on the board the queue hands over, where the update before
    this one has placed its runs. ``mid_turn`` says the turn took in a message the
    user sent while it ran: the instruction names that message's matter too, and
    nothing is confined, since only the model can tell what the message was about.

    A step that wrote over one asking questions gets one more call, after the
    update and before the write: an answer that never comes leaves the update as
    it was written, the way it stands today.
    """
    async with service.queue(session_key):
        try:
            board = service.load(session_key)
            answer = await _ask(
                board,
                session_key=session_key,
                call=call,
                window=window,
                dispatches=dispatches,
                turn_at=turn_at,
                turn_id=turn_id,
                now_ms=now_ms,
                owners=owning_groups(board, _as_placed(service, session_key, board, delegated), moved_on),
                reply_rows=reply_rows,
                mid_turn=mid_turn,
                answered_message_calls=answered_message_calls,
            )
        except Exception:
            logger.opt(exception=True).warning("Task board update for {} failed", session_key)
            answer = None
        carry: dict[str, Any] | None = None
        waiting: list[tuple[TaskGroup, TaskGroup]] = []
        if answer is not None:
            try:
                preview = apply_update(
                    board,
                    answer[0],
                    dispatches,
                    turn_at=turn_at,
                    turn_id=turn_id,
                    now_ms=now_ms(),
                    allow_long=answer[1],
                )
            except UpdateRejectedError:
                preview = None
            if preview is not None:
                waiting = carry_candidates(board, preview, turn_at)
                if waiting:
                    carry = await _ask_carry(
                        preview,
                        waiting,
                        session_key=session_key,
                        call=call,
                        exchange=answer[2],
                        turn_at=turn_at,
                        now_ms=now_ms,
                    )
        async with service.lock(session_key):
            if service.generation(session_key) != generation or service.was_dropped(session_key, turn_at):
                return None
            board = service.load(session_key)
            try:
                if answer is not None:
                    args, salvaged, _rows = answer
                    try:
                        updated = apply_update(
                            board,
                            args,
                            dispatches,
                            turn_at=turn_at,
                            turn_id=turn_id,
                            now_ms=now_ms(),
                            allow_long=salvaged,
                        )
                    except UpdateRejectedError as exc:
                        logger.warning(
                            "Task board update for {} no longer fits the board: {}", session_key, _faults(exc)
                        )
                        updated = None
                    if updated is not None:
                        if carry is not None:
                            kept = _still_open(waiting, updated)
                            entries = [entry for entry in carry.get("groups") or [] if entry.get("group") in kept]
                            if len(kept) != len(waiting):
                                logger.info(
                                    "Task board questions for {}: {} group(s) changed while the pass ran; left as the"
                                    " reader set them",
                                    session_key,
                                    len(waiting) - len(kept),
                                )
                            if entries:
                                try:
                                    updated = apply_carry(
                                        updated,
                                        {**carry, "groups": entries},
                                        kept,
                                        turn_at=turn_at,
                                        now_ms=now_ms(),
                                    )
                                except UpdateRejectedError as exc:
                                    logger.warning(
                                        "Task board questions for {} no longer fit the board: {}",
                                        session_key,
                                        _faults(exc),
                                    )
                        if board_to_dict(updated) != board_to_dict(board):
                            await service.write(session_key, updated)
                        if salvaged:
                            logger.warning(
                                "Task board update for {} was never accepted; its last attempt is written without"
                                " the groups changes it got wrong, and any text past its limit as it stands",
                                session_key,
                            )
                        return updated
                parked = _park(board, dispatches, turn_at, turn_id)
                if parked is not board:
                    logger.warning(
                        "Task board update for {} never landed; {} dispatch(es) parked for the next turn",
                        session_key,
                        len(parked.unassigned) - len(board.unassigned),
                    )
                    await service.write(session_key, parked)
                else:
                    logger.warning("Task board update for {} never landed; nothing new to park", session_key)
            except Exception:
                logger.opt(exception=True).warning("Task board for {} could not be written", session_key)
            return None
