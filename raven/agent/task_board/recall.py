"""Reading the Task Board back to the model: a listing, one group's thread, or a search.

Search is keyword retrieval (:mod:`raven.utils.bm25`, which tokenizes Chinese by
ideograph) over each group's title, steps and linked Task summaries. A completed
group still answers, at a lower weight, so a matter that is still open wins a tie
with one that is done.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from raven.agent.task_board.board import Board, TaskGroup, TaskLink
from raven.utils.bm25 import BM25Okapi, tokenize
from raven.utils.tokens import estimate_message_tokens

COMPLETED_RECALL_WEIGHT = 0.5
RUNNERS_UP = 2


def _age(now_ms: int, then_ms: int) -> str:
    minutes = max(0, now_ms - then_ms) // 60_000
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 48 * 60:
        return f"{minutes // 60}h ago"
    return f"{minutes // (24 * 60)}d ago"


def _open_first(groups: Sequence[TaskGroup]) -> list[TaskGroup]:
    newest = sorted(groups, key=lambda g: g.created_at, reverse=True)
    return [g for g in newest if g.status != "completed"] + [g for g in newest if g.status == "completed"]


def list_groups(board: Board, *, now_ms: int) -> str:
    if not board.groups:
        return "The task board is empty."
    done = sum(1 for g in board.groups if g.status == "completed")
    lines = [f"Task board: {len(board.groups)} groups ({len(board.groups) - done} open, {done} completed)."]
    for group in _open_first(board.groups):
        last = group.steps[-1].text if group.steps else ""
        lines.append(f"- {group.id} [{group.status}] {group.title} -- last: {last} ({_age(now_ms, group.updated_at)})")
    return "\n".join(lines)


def thread_text(
    board: Board,
    group_id: str,
    *,
    status_of: Callable[[TaskLink], str],
    lineage: Mapping[str, str],
) -> str:
    """One group's whole thread; ``KeyError`` for a group the board does not hold."""
    group = next((g for g in board.groups if g.id == group_id), None)
    if group is None:
        raise KeyError(group_id)
    continued: dict[str, list[str]] = {}
    for child, parent in lineage.items():
        continued.setdefault(parent, []).append(child)
    lines = [f"{group.id} [{group.status}] {group.title}"]
    for step in group.steps:
        lines.append(f"- {step.turn_at}: {step.text}")
        for link in step.tasks:
            line = f"    {link.kind}:{link.id} ({link.summary}): {status_of(link)}"
            if link.kind == "dag" and (children := sorted(continued.get(link.id, []))):
                line += "; continued as " + ", ".join(f"dag:{c}" for c in children)
            lines.append(line)
    return "\n".join(lines)


def rank(board: Board, query: str) -> list[TaskGroup]:
    """The groups that match ``query``, best first; a completed group's score is weighted down."""
    if not board.groups:
        return []
    documents = [
        tokenize(
            " ".join([g.title, g.title] + [s.text for s in g.steps] + [t.summary for s in g.steps for t in s.tasks])
        )
        for g in board.groups
    ]
    scores = BM25Okapi(documents).get_scores(tokenize(query))
    ranked = sorted(
        (
            (score * (COMPLETED_RECALL_WEIGHT if g.status == "completed" else 1.0), index)
            for index, (g, score) in enumerate(zip(board.groups, scores))
            if score > 0
        ),
        key=lambda pair: (-pair[0], pair[1]),
    )
    return [board.groups[index] for _, index in ranked]


def search(
    board: Board,
    query: str,
    *,
    status_of: Callable[[TaskLink], str],
    lineage: Mapping[str, str],
) -> str:
    if not board.groups:
        return "The task board is empty."
    ranked = rank(board, query)
    if not ranked:
        return f"Nothing on the board matches {query!r}; call recall with no arguments to list every group."
    out = thread_text(board, ranked[0].id, status_of=status_of, lineage=lineage)
    others = ranked[1 : 1 + RUNNERS_UP]
    if others:
        out += "\nOther matches: " + "; ".join(f"{g.id} {g.title}" for g in others)
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, Mapping))
    return ""


def turn_texts(messages: Sequence[Mapping[str, Any]], turn_ats: Sequence[str], *, max_tokens: int = 4000) -> str:
    """Each named turn's opening message and final reply, inside a token budget it reports when it binds."""
    position = {m.get("timestamp"): i for i, m in enumerate(messages)}
    blocks: list[str] = []
    used = 0
    for count, turn_at in enumerate(turn_ats):
        at = position.get(turn_at)
        if at is None:
            block = f"{turn_at}: gone from the log"
        else:
            reply = ""
            for message in messages[at + 1 :]:
                if message.get("role") == "user":
                    break
                if message.get("role") == "assistant" and _text(message.get("content")).strip():
                    reply = _text(message.get("content"))
            block = f"{turn_at}:\n  asked: {_text(messages[at].get('content'))}\n  replied: {reply or '(no reply)'}"
        cost = estimate_message_tokens({"role": "user", "content": block})
        if used + cost > max_tokens:
            if not blocks:
                blocks.append(block[: max_tokens * 3])
                count += 1
            blocks.append(f"[token budget of {max_tokens} reached; {len(turn_ats) - count} turn(s) left out]")
            break
        used += cost
        blocks.append(block)
    return "\n\n".join(blocks)
