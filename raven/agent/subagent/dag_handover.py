"""Extract and render bounded conversation handovers for reassigned DAG nodes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import escape as escape_xml
from pathlib import Path
from typing import Any, Mapping, Sequence

from raven.agent.subagent.dag_store import DagRunStore
from raven.agent.subagent.instance_log import message_rows, transcript_path
from raven.agent.subagent.prompt_errors import DagValidationError


@dataclass(frozen=True)
class HandoverTurn:
    agent: str
    user: str
    assistant: str


@dataclass(frozen=True)
class RenderedHandover:
    text: str
    carried_turns: int
    omitted_turns: int


@dataclass(frozen=True)
class LoadedHandover:
    turns: tuple[HandoverTurn, ...]
    notices: tuple[str, ...] = ()


class HandoverError(DagValidationError):
    """Required handover history is inconsistent or cannot fit the text limit."""


_ROLE_LABEL = re.compile(r"(?i)(?:\A|(?<=\r)|(?<=\n))([^\S\r\n]*)(user:|assistant:)")
_HANDOVER_TAG = re.compile(r"(?i)<(?=/?handover)")
_NO_ANSWER = "(no answer)"
_OMITTED_ANSWER = "[answer omitted]"


def escape_handover_text(text: str) -> str:
    """Prevent carried text from impersonating a role or handover tag."""
    text = _ROLE_LABEL.sub(lambda match: f"{match[1]}\\{match[2]}", text)
    return _HANDOVER_TAG.sub(lambda _match: "<\\", text)


def _text_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = [
            block["text"]
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
        if blocks:
            return "".join(blocks)
    return None


def turns_from_rows(rows: Sequence[Mapping[str, Any]], *, agent: str) -> tuple[HandoverTurn, ...]:
    """Take each user row and its last eligible assistant text from an instance log."""
    turns: list[HandoverTurn] = []
    user: str | None = None
    answer: str | None = None
    for row in rows:
        if "_type" in row:
            continue
        role = row.get("role")
        if role == "user":
            if user is not None:
                turns.append(HandoverTurn(agent, user, answer if answer is not None else _NO_ANSWER))
            user = _text_content(row.get("content")) or ""
            answer = None
        elif role == "assistant" and user is not None and not row.get("tool_calls"):
            content = _text_content(row.get("content"))
            if content is not None and not content.startswith("[failed]"):
                answer = content
    if user is not None:
        turns.append(HandoverTurn(agent, user, answer if answer is not None else _NO_ANSWER))
    return tuple(turns)


async def _read_recorded_follow_ups(store: DagRunStore, node_id: str) -> list[str]:
    """Read follow-ups positionally, so one malformed entry cannot shift a later message."""
    try:
        raw = await store.read_text(store.follow_ups_path(node_id))
    except (FileNotFoundError, KeyError):
        return []
    try:
        recorded = json.loads(raw)
    except ValueError as exc:
        raise HandoverError("handover follow-up history is malformed") from exc
    if not isinstance(recorded, list) or not all(isinstance(message, str) for message in recorded):
        raise HandoverError("handover follow-up history is malformed")
    return recorded


async def read_node_turns(
    store: DagRunStore, node_id: str, *, attempt_agents: Sequence[str]
) -> tuple[HandoverTurn, ...]:
    """Read one logical turn per attempt from the node's own archives."""
    if not attempt_agents:
        raise HandoverError("handover has no recorded attempts")
    try:
        task = await store.read_text(store.prompt_path(node_id))
    except (FileNotFoundError, KeyError) as exc:
        raise HandoverError("handover original prompt is unavailable") from exc
    follow_ups: Sequence[str] = ()
    if len(attempt_agents) > 1:
        follow_ups = await _read_recorded_follow_ups(store, node_id)
    if len(follow_ups) < len(attempt_agents) - 1:
        raise HandoverError("handover follow-up history is incomplete for recorded attempts")
    users = (task, *follow_ups[: len(attempt_agents) - 1])
    turns: list[HandoverTurn] = []
    for attempt, (agent, user) in enumerate(zip(attempt_agents, users, strict=True), start=1):
        try:
            answer = await store.read_text(store.attempt_output_path(node_id, attempt))
        except (FileNotFoundError, KeyError):
            answer = _NO_ANSWER
        turns.append(HandoverTurn(agent, user, answer))
    return tuple(turns)


def read_instance_turns(session_dir: Path, *, agent: str, handle: str) -> tuple[HandoverTurn, ...]:
    """Read visible conversation turns from the source agent's instance log."""
    return turns_from_rows(message_rows(session_dir, agent, handle), agent=agent)


def _instance_log_available(session_dir: Path, *, agent: str, handle: str) -> bool:
    path = transcript_path(session_dir, agent, handle)
    try:
        with path.open(encoding="utf-8") as log:
            records = (json.loads(line) for line in log if line.strip())
            header = next(records)
            if not isinstance(header, dict) or header.get("_type") != "metadata":
                return False
            return all(isinstance(row, dict) and row.get("role") for row in records)
    except (OSError, UnicodeError, ValueError, StopIteration):
        return False


async def _archived_attempt_prompts(store: DagRunStore, node_id: str, *, attempts: int) -> tuple[str, ...] | None:
    """Read every recorded attempt's dispatched prompt, or ``None`` when one is missing."""
    prompts: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            prompts.append(await store.read_text(store.attempt_prompt_path(node_id, attempt)))
        except (FileNotFoundError, KeyError):
            return None
    return tuple(prompts)


def _covers_archived_attempts(turns: Sequence[HandoverTurn], prompts: Sequence[str]) -> bool:
    """Whether the log proves it holds every archived attempt, the latest one last."""
    if not prompts:
        return False
    cursor = 0
    latest_index = -1
    for prompt in prompts:
        found = next((index for index in range(cursor, len(turns)) if turns[index].user == prompt), None)
        if found is None:
            return False
        latest_index = found
        cursor = found + 1
    latest_prompt = prompts[-1]
    # The log carries no attempt identity, so a unique latest prompt on the final
    # user row is the only proof that this attempt's turn was appended.
    return latest_index == len(turns) - 1 and sum(turn.user == latest_prompt for turn in turns) == 1


async def load_handover_turns(
    store: DagRunStore,
    node_id: str,
    *,
    session_dir: Path | None,
    source_agent: str,
    source_instance: str | None,
    attempt_agents: Sequence[str],
) -> LoadedHandover:
    """Select the source conversation, disclosing incomplete instance history."""
    if not attempt_agents:
        raise HandoverError("handover has no recorded attempts")
    if source_instance is None:
        return LoadedHandover(await read_node_turns(store, node_id, attempt_agents=attempt_agents))
    if session_dir is not None and _instance_log_available(session_dir, agent=source_agent, handle=source_instance):
        turns = read_instance_turns(session_dir, agent=source_agent, handle=source_instance)
        if turns:
            prompts = await _archived_attempt_prompts(store, node_id, attempts=len(attempt_agents))
            if prompts is not None and _covers_archived_attempts(turns, prompts):
                return LoadedHandover(turns)
    return LoadedHandover(
        await read_node_turns(store, node_id, attempt_agents=attempt_agents),
        (
            "Source instance history unavailable; handover contains only this node's archived attempts and is incomplete.",
        ),
    )


def _inner_block(turns: Sequence[HandoverTurn], retained: set[int], answers: Mapping[int, str]) -> str:
    lines: list[str] = []
    omitted_count = 0
    for index, turn in enumerate(turns):
        if index not in retained:
            omitted_count += 1
            continue
        if omitted_count:
            lines.append(f"[{omitted_count} earlier turns omitted]")
            omitted_count = 0
        lines.append(f"User: {escape_handover_text(turn.user)}")
        lines.append(f"Assistant: {answers[index]}")
    if omitted_count:
        lines.append(f"[{omitted_count} earlier turns omitted]")
    return "\n".join(lines) + "\n"


def _shorten_middle(answer: str, available: int) -> str | None:
    if len(answer) <= available:
        return answer
    if len(answer) < 3:
        return None
    low, high = 2, min(len(answer) - 1, available)
    best: str | None = None
    while low <= high:
        kept = (low + high) // 2
        omitted = len(answer) - kept
        marker = f"[... {omitted} characters omitted ...]"
        if kept + len(marker) <= available:
            front = (kept + 1) // 2
            best = answer[:front] + marker + answer[-(kept - front) :]
            low = kept + 1
        else:
            high = kept - 1
    return best


def render_handover(
    turns: Sequence[HandoverTurn], *, task: str, to_agent: str, next_message: str, max_chars: int = 128000
) -> RenderedHandover:
    """Keep the exact task and newest turn while fitting the emitted inner block."""
    source = list(turns)
    task_index = next((index for index in range(len(source) - 1, -1, -1) if source[index].user == task), None)
    if task_index is None:
        source.insert(0, HandoverTurn(source[0].agent if source else "", task, _NO_ANSWER))
        task_index = 0
    newest_index = len(source) - 1
    retained = {task_index, newest_index}
    answers = {index: escape_handover_text(turn.assistant) for index, turn in enumerate(source)}

    def block() -> str:
        return _inner_block(source, retained, answers)

    if len(block()) > max_chars and task_index != newest_index and len(answers[task_index]) > len(_OMITTED_ANSWER):
        answers[task_index] = _OMITTED_ANSWER
    if len(block()) > max_chars:
        newest_answer = answers[newest_index]
        answers[newest_index] = ""
        minimum = len(block())
        available = max_chars - minimum
        shortened = _shorten_middle(newest_answer, available)
        if shortened is None:
            raise HandoverError(
                f"handover required text includes a {len(task)}-character task and needs at least "
                f"{minimum} characters inside the block; limit is {max_chars}"
            )
        answers[newest_index] = shortened
    if len(block()) > max_chars:
        raise HandoverError(
            f"handover required text includes a {len(task)}-character task and needs "
            f"{len(block())} characters inside the block; limit is {max_chars}"
        )

    for index in range(len(source) - 1, -1, -1):
        if index in retained:
            continue
        retained.add(index)
        if len(block()) > max_chars:
            retained.remove(index)
            break

    carried = [source[index] for index in sorted(retained)]
    agents = ", ".join(dict.fromkeys(turn.agent for turn in carried))
    inner = block()
    text = (
        f'<handover from="{escape_xml(agents, quote=True)}" to="{escape_xml(to_agent, quote=True)}" '
        f'turns="{len(carried)}">\n{inner}</handover>\n\n'
        f"[Continuing this conversation. The turns above ran under {agents}; "
        f"you are now {to_agent} picking up where it left off. "
        f"Your next user message follows.]\n\n{escape_handover_text(next_message)}"
    )
    return RenderedHandover(text, len(carried), len(source) - len(carried))
