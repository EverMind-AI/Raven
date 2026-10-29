"""The post-turn call: once a host turn has answered, its model keeps the Task Board.

One extra call, after the reply has gone out: the turn's own window and tools
array plus one instruction, with the tool choice forced to ``task_board``. The
arguments are checked whole by :func:`apply_update`; a refusal goes back to the
model with its reason, up to :data:`MAX_ATTEMPTS` times. When no update can be
had, the turn's dispatches are parked in ``unassigned`` and the next update has
to place them first -- a dispatch is never silently dropped.

The whole run holds the session's lock, so the next turn's update is built on
this one's result. Nothing here touches the session's messages.
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
    UpdateRejectedError,
    apply_update,
    board_to_dict,
)
from raven.agent.task_board.service import TaskBoardService
from raven.agent.task_board.tool import TOOL_NAME
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.i18n import prompt

MAX_ATTEMPTS = 3
OPEN_STEPS_SHOWN = 2
TEMPLATE = "task_board_update"

_PROSE = "Answer with a task_board call (action=update), not with text."
_NOT_UPDATE = "Only action=update is accepted here; the board is already in the instruction above."


def should_run(status: str, dispatches: Sequence[Dispatch]) -> bool:
    """A turn that answered keeps the board; one that failed does only if it dispatched work."""
    return status == "completed" or bool(dispatches)


def _snapshot(board: Board) -> str:
    if not board.groups:
        return "(the board is empty)"
    lines: list[str] = []
    for group in sorted((g for g in board.groups if g.status != "completed"), key=lambda g: g.created_at):
        lines.append(f"- {group.id} [{group.status}] {group.title}")
        lines.extend(f"    - {step.text}" for step in group.steps[-OPEN_STEPS_SHOWN:])
    done = sorted((g for g in board.groups if g.status == "completed"), key=lambda g: g.created_at)
    if done:
        lines.append("Completed: " + "; ".join(f"{g.id}: {g.title}" for g in done))
    return "\n".join(lines)


def _handles(board: Board, dispatches: Sequence[Dispatch]) -> str:
    pending = {p.handle for p in board.unassigned}
    lines = [f"- {p.handle} -- {p.summary} (left over from an earlier turn)" for p in board.unassigned]
    lines += [f"- {d.handle} -- {d.summary}" for d in dispatches if d.handle not in pending]
    return "\n".join(lines) or "(none)"


def build_instruction(board: Board, dispatches: Sequence[Dispatch]) -> str:
    return prompt(TEMPLATE).format(board=_snapshot(board), dispatches=_handles(board, dispatches))


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


def _park(board: Board, dispatches: Sequence[Dispatch], turn_at: str, turn_id: str | None) -> Board:
    pending = {p.handle for p in board.unassigned}
    fresh = [Pending(d.handle, d.summary, turn_at, turn_id) for d in dispatches if d.handle not in pending]
    return Board(board.groups, board.unassigned + fresh, board.version) if fresh else board


async def _update(
    service: TaskBoardService,
    board: Board,
    *,
    session_key: str,
    call: Callable[[list[dict]], Awaitable[LLMResponse]],
    window: list[dict],
    dispatches: list[Dispatch],
    turn_at: str,
    turn_id: str | None,
    now_ms: Callable[[], int],
) -> Board | None:
    """Up to MAX_ATTEMPTS asks; the board written, or ``None`` when no update was had."""
    rows = list(window) + [{"role": "user", "content": build_instruction(board, dispatches)}]
    for _ in range(MAX_ATTEMPTS):
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
            rows += [{"role": "assistant", "content": response.content or ""}, {"role": "user", "content": _PROSE}]
            continue
        args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        if args.get("action") != "update":
            result = _NOT_UPDATE
        else:
            try:
                updated = apply_update(board, args, dispatches, turn_at=turn_at, turn_id=turn_id, now_ms=now_ms())
            except UpdateRejectedError as exc:
                result = f"Rejected: {exc}. Call task_board again with action=update."
            else:
                if board_to_dict(updated) != board_to_dict(board):
                    await service.write(session_key, updated)
                return updated
        rows += [
            _assistant_row(response, tool_call),
            {"role": "tool", "tool_call_id": tool_call.id, "name": TOOL_NAME, "content": result},
        ]
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
) -> Board | None:
    """Keep the board for the turn that just ended; the board written, or ``None`` when no update was had.

    Whatever goes wrong on the way -- the provider, the model's arguments, the
    disk -- ends with this turn's dispatches parked rather than with an exception
    in a background task nobody awaits.
    """
    async with service.lock(session_key):
        board = service.load(session_key)
        try:
            updated = await _update(
                service,
                board,
                session_key=session_key,
                call=call,
                window=window,
                dispatches=dispatches,
                turn_at=turn_at,
                turn_id=turn_id,
                now_ms=now_ms,
            )
        except Exception:
            logger.opt(exception=True).warning("Task board update for {} failed", session_key)
            updated = None
        if updated is not None:
            return updated
        parked = _park(board, dispatches, turn_at, turn_id)
        if parked is not board:
            logger.warning(
                "Task board update for {} never landed; {} dispatch(es) parked for the next turn",
                session_key,
                len(parked.unassigned) - len(board.unassigned),
            )
            try:
                await service.write(session_key, parked)
            except Exception:
                logger.opt(exception=True).warning("Task board for {} could not be written", session_key)
        return None
