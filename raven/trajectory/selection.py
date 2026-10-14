"""Turn selection: a session's main-line turns, their trace closure, and the cut transcript.

A *main-line turn* is a ``session.turn`` root recorded under a session's key that
no other trace dispatched. It is the unit a reader picks when exporting or
reporting part of a conversation; picking one brings along every trace it
dispatched (sub-agents, their own sub-agents, follow-up turns that report
back), linked through ``trace.dispatched_in_trace_id`` on each child's root.

Selection is two scans of the span logs. :func:`plan_selection` validates the
request against the session's turns and resolves the closure; only its
:class:`SelectionPlan` is accepted by :func:`collect_selection`, which then reads
every physical record of the closure in write order. Both scans prefilter raw
lines by substring before decoding, so the cost is one read of the logs plus
JSON parsing of the lines that can matter.

The session record is cut to the same turns by :func:`cut_session_record`: the
messages are split into blocks at each turn's opening user message, a block is
matched to its trace through the Turn Trace Mark the agent loop stores on that
message, and records written before the mark existed are matched by the turn's
input text inside the turn's time window -- but only where the record's order
leaves a single possible turn. A turn that cannot be matched with certainty
keeps its trajectory only.

Nothing here has side effects: no pin, no attempt definition, no bundle.
"""

from __future__ import annotations

import copy
import json
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from raven.tracing import artifact_v2
from raven.tracing import config as tracing_config
from raven.trajectory import entries as _entries
from raven.trajectory import store as tstore

TURN_SPAN = "session.turn"
DISPATCHED_IN = "trace.dispatched_in_trace_id"
TURN_TRACE_KEY = "trace_id"

WHY_EMPTY = "empty"
WHY_NOT_IN_SESSION = "not_in_session"
WHY_IN_PROGRESS = "in_progress"

_TURN_NEEDLE = TURN_SPAN.encode()
_LINK_NEEDLE = DISPATCHED_IN.encode()
_INPUT_READ_LIMIT = 1 << 20
# The opening entry is stamped when the message arrived, close to the span's
# start; older records were stamped at turn end. The slack covers clock reads
# taken a moment apart on either side of the window.
_WINDOW_SLACK = timedelta(seconds=5)


@dataclass(frozen=True)
class TurnSummary:
    trace_id: str
    span_id: str
    number: int
    start: str
    end: str | None
    input_preview: str
    in_progress: bool
    input_artifact: str | None
    main_line: bool


class SelectionError(ValueError):
    """The requested turns cannot be selected; ``invalid`` names each one and why."""

    def __init__(self, invalid: Sequence[tuple[str, str]] = (), *, why: str | None = None) -> None:
        self.invalid = tuple(invalid)
        self.why = why
        detail = why or ", ".join(f"{trace_id} ({reason})" for trace_id, reason in self.invalid)
        super().__init__(f"invalid turn selection: {detail}")


@dataclass(frozen=True)
class SelectionPlan:
    session_key: str
    selected: tuple[str, ...]
    dispatched: tuple[str, ...]
    turns: tuple[TurnSummary, ...]
    session_turns: tuple[TurnSummary, ...]

    @property
    def closure(self) -> frozenset[str]:
        return frozenset(self.selected) | frozenset(self.dispatched)


@dataclass(frozen=True)
class Selection:
    plan: SelectionPlan
    spans: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SessionCut:
    included: bool
    metadata: dict[str, Any] | None
    messages: tuple[dict[str, Any], ...]
    unmatched: tuple[str, ...]

    def jsonl(self) -> str:
        rows: list[dict[str, Any]] = ([self.metadata] if self.metadata is not None else []) + list(self.messages)
        return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


@dataclass
class _Scan:
    turns: list[dict[str, Any]]
    links: dict[str, str]


def _state(state_dir: Path | None) -> Path:
    return state_dir or tracing_config.state_dir()


def _records(
    state_dir: Path, wanted: re.Pattern[bytes] | None, needles: Sequence[bytes] = ()
) -> Iterator[dict[str, Any]]:
    """Span records in write order whose raw line passes the prefilter.

    Same tolerance as :func:`raven.trajectory.store.iter_spans`: an unreadable
    file, an undecodable or unparseable line, a non-object record each cost
    themselves only.
    """
    for path in tstore.span_log_paths(state_dir):
        try:
            raw_lines = path.read_bytes().splitlines()
        except OSError:
            continue
        for raw_line in raw_lines:
            if needles and not any(needle in raw_line for needle in needles):
                continue
            if wanted is not None and wanted.search(raw_line) is None:
                continue
            try:
                line = raw_line.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _scan(session_key: str, state_dir: Path) -> _Scan:
    turns: list[dict[str, Any]] = []
    links: dict[str, str] = {}
    for record in _records(state_dir, None, (_TURN_NEEDLE, _LINK_NEEDLE)):
        if tstore._str_value(record.get("parentSpanId")) is not None:
            continue
        trace_id = tstore._str_value(record.get("traceId"))
        if trace_id is None:
            continue
        attrs = tstore._span_attrs(record)
        parent = tstore._str_value(attrs.get(DISPATCHED_IN))
        if parent is not None and parent != trace_id:
            links[trace_id] = parent
        if (
            record.get("name") == TURN_SPAN
            and attrs.get("session.key") == session_key
            and tstore._str_value(record.get("spanId")) is not None
        ):
            turns.append(record)
    return _Scan(turns=_entries.merge_snapshots(turns), links=links)


def _summaries(scan: _Scan, *, answering: bool) -> list[TurnSummary]:
    rows: list[dict[str, Any]] = []
    for record in scan.turns:
        attrs = tstore._span_attrs(record)
        rows.append(
            {
                "trace_id": record["traceId"],
                "span_id": record["spanId"],
                "start": tstore._str_value(record.get("startTime")) or "",
                "end": tstore._str_value(record.get("endTime")),
                "input_preview": tstore._str_value(attrs.get("turn.input_preview")) or "",
                "checkpoint": attrs.get("turn.in_progress") is True,
                "input_artifact": tstore._str_value(attrs.get("turn.input.artifact_path")),
                "main_line": tstore._str_value(attrs.get(DISPATCHED_IN)) is None,
            }
        )
    rows.sort(key=lambda row: (row["start"], row["span_id"]))
    main = [row for row in rows if row["main_line"]]
    last_main = main[-1]["span_id"] if main else None
    numbers = {row["span_id"]: index for index, row in enumerate(main, 1)}
    out: list[TurnSummary] = []
    for row in rows:
        checkpoint = row["checkpoint"]
        out.append(
            TurnSummary(
                trace_id=row["trace_id"],
                span_id=row["span_id"],
                number=numbers.get(row["span_id"], 0),
                start=row["start"],
                end=None if checkpoint else row["end"],
                input_preview=row["input_preview"],
                in_progress=checkpoint and answering and row["span_id"] == last_main,
                input_artifact=row["input_artifact"],
                main_line=row["main_line"],
            )
        )
    return out


def session_turns(session_key: str, *, answering: bool, state_dir: Path | None = None) -> list[TurnSummary]:
    """The session's main-line turns, numbered from 1 in start order.

    ``answering`` says whether the session has a turn in flight right now; it
    is required because a checkpoint alone cannot tell a running turn from one
    whose process died before closing it, and only the latter may be picked.
    """
    return [turn for turn in _summaries(_scan(session_key, _state(state_dir)), answering=answering) if turn.main_line]


def _closure(selected: Iterable[str], links: dict[str, str]) -> set[str]:
    children: dict[str, set[str]] = {}
    for child, parent in links.items():
        children.setdefault(parent, set()).add(child)
    seen = set(selected)
    queue = deque(seen)
    while queue:
        for child in children.get(queue.popleft(), ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


def plan_selection(
    session_key: str, trace_ids: Sequence[str], *, answering: bool, state_dir: Path | None = None
) -> SelectionPlan:
    """Validate ``trace_ids`` as main-line turns of ``session_key`` and resolve their closure.

    Raises :class:`SelectionError` listing every id that is not a main-line
    turn of the session or that is still in progress; nothing is collected.
    """
    wanted = list(dict.fromkeys(trace_ids))
    if not wanted:
        raise SelectionError(why=WHY_EMPTY)
    scan = _scan(session_key, _state(state_dir))
    every = _summaries(scan, answering=answering)
    main = {turn.trace_id: turn for turn in every if turn.main_line}
    invalid: list[tuple[str, str]] = []
    for trace_id in wanted:
        turn = main.get(trace_id)
        if turn is None:
            invalid.append((trace_id, WHY_NOT_IN_SESSION))
        elif turn.in_progress:
            invalid.append((trace_id, WHY_IN_PROGRESS))
    if invalid:
        raise SelectionError(invalid)
    turns = sorted((main[trace_id] for trace_id in wanted), key=lambda turn: turn.number)
    selected = tuple(turn.trace_id for turn in turns)
    closure = _closure(selected, scan.links)
    return SelectionPlan(
        session_key=session_key,
        selected=selected,
        dispatched=tuple(sorted(closure - set(selected))),
        turns=tuple(turns),
        session_turns=tuple(every),
    )


def _id_pattern(ids: Iterable[str]) -> re.Pattern[bytes]:
    return re.compile(b"|".join(re.escape(trace_id.encode()) for trace_id in sorted(ids)))


def collect_selection(plan: SelectionPlan, *, state_dir: Path | None = None) -> Selection:
    """Every physical record of the plan's closure, checkpoints included, in write order.

    Raises ``LookupError`` when a selected turn has no record left: its spans
    were purged after the plan was made, and a selection missing a turn the
    reader picked must not be handed on as if it were whole.
    """
    if not isinstance(plan, SelectionPlan):
        raise TypeError("collect_selection takes the SelectionPlan that plan_selection returned")
    closure = plan.closure
    spans: list[dict[str, Any]] = []
    found: set[str] = set()
    for record in _records(_state(state_dir), _id_pattern(closure)):
        trace_id = tstore._str_value(record.get("traceId"))
        if trace_id in closure:
            spans.append(record)
            found.add(trace_id)
    missing = [trace_id for trace_id in plan.selected if trace_id not in found]
    if missing:
        raise LookupError(f"no spans left for selected turn(s): {', '.join(missing)}")
    return Selection(plan=plan, spans=tuple(spans))


def traces_present(ids: Iterable[str], *, state_dir: Path | None = None) -> set[str]:
    """The ids among ``ids`` that still have at least one span record."""
    wanted = {trace_id for trace_id in ids if isinstance(trace_id, str) and trace_id}
    found: set[str] = set()
    if not wanted:
        return found
    for record in _records(_state(state_dir), _id_pattern(wanted)):
        trace_id = tstore._str_value(record.get("traceId"))
        if trace_id in wanted:
            found.add(trace_id)
            if found == wanted:
                break
    return found


def store_artifact(value: Any, state_dir: Path) -> Path | None:
    """The resolved file a ``*.artifact_path`` value names, when it may be read.

    Only regular files inside ``<state>/logs/audit-artifacts`` qualify: span
    records are unvalidated history, and a pointer outside the store (or one
    escaping it through ``..`` or a link) must not pull an arbitrary file in.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        root = (state_dir / "logs" / "audit-artifacts").resolve()
        target = Path(value).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
    except (OSError, ValueError):
        return None
    return target


def _read_session(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    metadata: dict[str, Any] | None = None
    messages: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("_type") == "metadata":
            metadata = row
        else:
            messages.append(row)
    return metadata, messages


def _cut_metadata(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """The last metadata row with the cursors that index the full message list reset."""
    if row is None:
        return None
    out = copy.deepcopy(row)
    out["last_consolidated"] = 0
    inner = out.get("metadata")
    if isinstance(inner, dict) and "annotated_through" in inner:
        inner["annotated_through"] = 0
    return out


def _opens_turn(message: dict[str, Any]) -> bool:
    return message.get("role") == "user" and message.get("mid_turn") is not True


def _blocks(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    blocks: list[list[dict[str, Any]]] = []
    for message in messages:
        if _opens_turn(message):
            blocks.append([message])
        elif blocks:
            blocks[-1].append(message)
    return blocks


def _text_of(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part["text"]
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts) if parts else None
    return None


def _turn_input(turn: TurnSummary, state_dir: Path) -> str | None:
    target = store_artifact(turn.input_artifact, state_dir)
    if target is None:
        return None
    try:
        with target.open("rb") as handle:
            data = handle.read(_INPUT_READ_LIMIT + 1)
        if len(data) > _INPUT_READ_LIMIT:
            return None
        payload = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    payload = artifact_v2.resolve_payload(payload, state_dir / "logs" / "audit-artifacts")
    return _text_of(payload.get("content")) if isinstance(payload, dict) else None


def _same_input(turn: TurnSummary, text: str | None, full: str | None) -> bool:
    if text is None:
        return False
    if full is not None:
        return full == text
    preview = turn.input_preview
    if not preview:
        return False
    head = preview[:-3]
    if preview.endswith("...") and len(head) == tracing_config.preview_len():
        return text.startswith(head)
    return preview == text


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def _in_window(stamp: datetime | None, turn: TurnSummary, following: TurnSummary | None) -> bool:
    start = _instant(turn.start)
    if stamp is None or start is None:
        return False
    if stamp < start - _WINDOW_SLACK:
        return False
    upper = _instant(turn.end) if turn.end else (_instant(following.start) if following is not None else None)
    return upper is None or stamp <= upper + _WINDOW_SLACK


def _forced(blocks: list[int], options: dict[int, set[int]], turns: list[int]) -> dict[int, int]:
    """Block -> turn for the pairs every order-preserving assignment agrees on.

    ``blocks`` and ``turns`` are both in record order; each block has to land
    on a distinct turn later than the previous block's. ``prefix[i][j]`` says
    the first ``i`` blocks fit into the first ``j`` turns, ``suffix[i][j]``
    that the blocks from ``i`` on fit into the turns from ``j`` on; a pair is
    possible when both halves around it fit. When no assignment fits at all
    the premise is wrong (a block whose turn left no span), and nothing is
    claimed.
    """
    n, m = len(blocks), len(turns)
    fits = [[turns[j] in options[blocks[i]] for j in range(m)] for i in range(n)]
    prefix = [[False] * (m + 1) for _ in range(n + 1)]
    prefix[0] = [True] * (m + 1)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            prefix[i][j] = prefix[i][j - 1] or (prefix[i - 1][j - 1] and fits[i - 1][j - 1])
    suffix = [[False] * (m + 1) for _ in range(n + 1)]
    suffix[n] = [True] * (m + 1)
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            suffix[i][j] = suffix[i][j + 1] or (fits[i][j] and suffix[i + 1][j + 1])
    if not prefix[n][m]:
        return {}
    forced: dict[int, int] = {}
    for i in range(n):
        possible = [j for j in range(m) if fits[i][j] and prefix[i][j] and suffix[i + 1][j + 1]]
        if len(possible) == 1:
            forced[blocks[i]] = turns[possible[0]]
    return forced


def _match_unmarked(
    blocks: list[list[dict[str, Any]]], owner: dict[int, str], timeline: list[TurnSummary], state_dir: Path
) -> dict[int, str]:
    """Block index -> trace id for the unmarked blocks whose turn is certain.

    Messages are appended in turn order, so the record's unmarked blocks map
    onto distinct unclaimed turns in timeline order, and each marked block pins
    where the stretch around it sits. A block may only be a turn whose input
    matches its text and whose window holds its stamp; the window of a turn
    left as a checkpoint closes at the next turn of the whole timeline, claimed
    or not. Among the assignments that satisfy all of that, a block is given a
    turn only when every one of them agrees: an undone turn whose words were
    typed again stays ambiguous, and an ambiguous block keeps its trajectory
    only rather than lending its messages to the wrong turn.
    """
    position = {turn.trace_id: index for index, turn in enumerate(timeline)}
    claimed = set(owner.values())
    anchors = sorted((index, position[trace_id]) for index, trace_id in owner.items() if trace_id in position)
    turns = [index for index, turn in enumerate(timeline) if turn.trace_id not in claimed]
    inputs: dict[str, str | None] = {}
    options: dict[int, set[int]] = {}
    stretch: dict[int, tuple[int, int]] = {}
    for index in range(len(blocks)):
        if index in owner:
            continue
        low = max((at for block, at in anchors if block < index), default=-1)
        high = min((at for block, at in anchors if block > index), default=len(timeline))
        stretch[index] = (low, high)
        opener = blocks[index][0]
        text = _text_of(opener.get("content"))
        stamp = _instant(opener.get("timestamp"))
        fitting: set[int] = set()
        for at in turns:
            if not low < at < high:
                continue
            turn = timeline[at]
            following = timeline[at + 1] if at + 1 < len(timeline) else None
            if not _in_window(stamp, turn, following):
                continue
            if turn.trace_id not in inputs:
                inputs[turn.trace_id] = _turn_input(turn, state_dir)
            if _same_input(turn, text, inputs[turn.trace_id]):
                fitting.add(at)
        if fitting:
            options[index] = fitting
    # Marked blocks cut the record into stretches that cannot share a turn, so
    # each is solved on its own and one inconsistent stretch costs only itself.
    matched: dict[int, str] = {}
    for low, high in sorted({stretch[index] for index in options}):
        members = sorted(index for index in options if stretch[index] == (low, high))
        within = [at for at in turns if low < at < high]
        for index, at in _forced(members, options, within).items():
            matched[index] = timeline[at].trace_id
    return matched


def cut_session_record(session_path: Path, plan: SelectionPlan, *, state_dir: Path | None = None) -> SessionCut:
    """The session record cut to the plan's closure, plus the selected turns it lacks."""
    read = _read_session(Path(session_path))
    if read is None:
        return SessionCut(included=False, metadata=None, messages=(), unmatched=plan.selected)
    metadata, messages = read
    blocks = _blocks(messages)
    owner: dict[int, str] = {}
    for index, block in enumerate(blocks):
        mark = block[0].get(TURN_TRACE_KEY)
        if isinstance(mark, str) and mark:
            owner[index] = mark
    timeline = sorted(plan.session_turns, key=lambda turn: (turn.start, turn.span_id))
    owner.update(_match_unmarked(blocks, owner, timeline, _state(state_dir)))
    closure = plan.closure
    kept: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if owner.get(index) in closure:
            kept.extend(copy.deepcopy(block))
    present = set(owner.values())
    return SessionCut(
        included=True,
        metadata=_cut_metadata(metadata),
        messages=tuple(kept),
        unmatched=tuple(trace_id for trace_id in plan.selected if trace_id not in present),
    )


__all__ = [
    "Selection",
    "SelectionError",
    "SelectionPlan",
    "SessionCut",
    "TURN_TRACE_KEY",
    "TurnSummary",
    "WHY_EMPTY",
    "WHY_IN_PROGRESS",
    "WHY_NOT_IN_SESSION",
    "collect_selection",
    "cut_session_record",
    "plan_selection",
    "session_turns",
    "store_artifact",
    "traces_present",
]
