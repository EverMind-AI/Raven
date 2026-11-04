"""The post-turn call: once a host turn has answered, its model keeps the Task Board.

One extra call, after the reply has gone out: the turn's own window and tools
array plus one instruction asking for a ``task_board`` call. Nothing forces the
tool -- the choice stays the turn's own -- so an answer in text is asked again. The
arguments are checked whole by :func:`apply_update`; a refusal goes back to the
model with every fault it found, up to :data:`MAX_ATTEMPTS` times. When every
attempt is refused, the last update the model sent is still written as far as
the board can take it -- see :func:`salvage` -- and only a fault in a step or in
where its work goes leaves no update to be had. Then the turn's dispatches are
parked in ``unassigned`` and the next update has to place them first -- a
dispatch is never silently dropped. A turn that failed or was stopped makes no
call, and what it dispatched is parked the same way, in its place in the update
queue (:func:`park_dispatches`). A turn known to be about some groups -- a
delegated result's, a Move on's -- has its update confined to them, and to
wherever it places its dispatches, before it is checked. Each refusal is logged,
and each update that never landed.

When the update wrote a step over one whose questions the user never answered,
one more call follows, continuing from the update's own exchange: it decides which
of those questions are still open and records the complete list on the newest
step, so a turn that only explains something cannot bury a pending question.
Updates of one session run one at a time, so the next turn's update is built
on this one's result. Nothing here touches the session's messages.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

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
from raven.agent.task_board.service import TaskBoardService
from raven.agent.task_board.tool import TOOL_NAME
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.i18n import prompt

MAX_ATTEMPTS = 3
OPEN_STEPS_SHOWN = 2
TEMPLATE = "task_board_update"
SCOPE_TEMPLATE = "task_board_scope"
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


def _scope(owners: Sequence[str]) -> str:
    # Ids alone: a title is text an earlier call wrote, and only the record is fenced.
    return f"\n{prompt(SCOPE_TEMPLATE).strip().format(groups=', '.join(owners))}\n" if owners else ""


def build_instruction(board: Board, dispatches: Sequence[Dispatch], owners: Sequence[str] = ()) -> str:
    return prompt(TEMPLATE).format(board=_snapshot(board), dispatches=_handles(board, dispatches), scope=_scope(owners))


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


def _assistant_row(response: LLMResponse, call: ToolCallRequest) -> dict[str, Any]:
    row: dict[str, Any] = {
        "role": "assistant",
        "content": response.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
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
) -> tuple[dict[str, Any], bool, list[dict]] | None:
    """Up to MAX_ATTEMPTS asks; the arguments of an update the board accepted, whether
    they are a salvage, whose text past a limit stands, and the exchange the asks left
    -- the question pass continues from it. ``None`` when no update was had.

    A turn that belongs to ``owners`` has its update confined to them before it is
    checked, so what would be dropped anyway never costs a refusal. When the last
    attempt is refused too, the last update sent is salvaged: its work is placed and
    its steps written unless a fault lies there.
    """
    rows = list(window) + [{"role": "user", "content": build_instruction(board, dispatches, owners)}]
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
        tool_call = next((c for c in response.tool_calls if c.name == TOOL_NAME), None)
        if tool_call is None:
            _refused(session_key, attempt, "answered in text")
            rows += [{"role": "assistant", "content": response.content or ""}, {"role": "user", "content": _PROSE}]
            continue
        args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        if args.get("action") != "update":
            _refused(session_key, attempt, f"called action={args.get('action')}")
            result = _NOT_UPDATE
        else:
            confined = confine_update(args, owners)
            try:
                apply_update(board, confined, dispatches, turn_at=turn_at, turn_id=turn_id, now_ms=now_ms())
            except UpdateRejectedError as exc:
                _refused(session_key, attempt, _faults(exc))
                last = confined
                result = _refusal(exc)
            else:
                if confined != args:
                    logger.info(
                        "Task board update for {} kept to {}; the rest was about other matters", session_key, owners
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
        tool_call = next((c for c in response.tool_calls if c.name == TOOL_NAME), None)
        if tool_call is None:
            _refused(session_key, attempt, "answered in text", what="questions")
            rows += [
                {"role": "assistant", "content": response.content or ""},
                {"role": "user", "content": _CARRY_PROSE},
            ]
            continue
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
    delegated: Sequence[str] = (),
    moved_on: Sequence[str] = (),
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
    ones the turn belongs to, and its update is confined to them and to wherever
    it places the work it started. They are resolved on the board the queue
    hands over, where the update before this one has placed its runs.

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
