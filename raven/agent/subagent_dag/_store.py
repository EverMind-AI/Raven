# -*- coding: utf-8 -*-
"""Backend-backed storage for one DAG run's message files."""

import asyncio
import json
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

INDEX_FILENAME = "index.json"

# One lock per (event loop, history root). The index is read-modify-written and
# is also what makes a node id unique per conversation, so two concurrent runs
# racing on it do not merely lose a discovery row -- they can both pass the
# uniqueness check and then both claim the same id, leaving two nodes answering
# to one name.
#
# Keyed by root so unrelated sessions never serialize behind each other, and
# held in a table keyed weakly on the loop so a lock is never reused across
# loops (asyncio.Lock binds to the loop that first awaits it) and the whole
# table for a finished loop is collected with it.
_INDEX_LOCKS: "weakref.WeakKeyDictionary[Any, dict[str, asyncio.Lock]]" = weakref.WeakKeyDictionary()


@asynccontextmanager
async def index_guard(root: str) -> AsyncIterator[None]:
    """Serialize this process's reads and writes of one session's run index.

    Every caller that reads the index to decide something, and every caller
    that writes it, must hold this -- read/validate/claim is a check-then-act
    sequence, and splitting it lets two runs claim one node id. Not reentrant:
    :meth:`DagRunStore.upsert_index` expects the caller to hold it already.

    In-process only. Two processes sharing one session (two gateways) still
    race; closing that needs file locking, which this deliberately does not do.

    Args:
        root (`str`):
            The session's DAG history root.

    Yields:
        `None`:
            With the index held for this root.
    """
    per_loop = _INDEX_LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = per_loop.get(root)
    if lock is None:
        lock = per_loop[root] = asyncio.Lock()
    async with lock:
        yield


# States a node can be in that no node is ever *written* with -- they describe
# what the index says rather than what a run recorded.
#
# RUNNING: the run claimed the id at start and has not finalized. Still going,
# or died mid-flight.
# UNRECORDED: the run finished but left no per-node outcome, which is what a
# history written before outcomes were indexed looks like. Distinct from
# RUNNING because "wait for it" is the wrong advice for a run that is over.
RUNNING = "running"
UNRECORDED = "unrecorded"


def output_path_in(backend: Any, root: str, run_id: str, node_id: str) -> str:
    """Path of one node's output file, for any run under ``root``.

    Defined here rather than only on :class:`DagRunStore` because a node may
    reference an earlier run's output, and that path has to be built from the
    same layout the store writes.

    Args:
        backend (`BackendBase`):
            Backend supplying the environment's path semantics.
        root (`str`):
            The session's DAG history root.
        run_id (`str`):
            The run that produced the node.
        node_id (`str`):
            The node id.

    Returns:
        `str`:
            ``<root>/<run_id>/<node_id>.out.md``.
    """
    return backend.join_path(root, run_id, f"{node_id}.out.md")


async def read_index(backend: Any, root: str) -> list[dict]:
    """Every run recorded for this session, oldest first.

    Args:
        backend (`BackendBase`):
            The session workspace backend.
        root (`str`):
            The session's DAG history root.

    Returns:
        `list[dict]`:
            The index entries, or an empty list when the index is missing or
            unreadable -- a damaged index must not fail the run that reads it.
    """
    path = backend.join_path(root, INDEX_FILENAME)
    if not await backend.file_exists(path):
        return []
    try:
        entries = json.loads((await backend.read_file(path)).decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        # Logged, not swallowed silently: this file is what keeps node ids
        # unique per conversation, so reading it as empty quietly drops that
        # guarantee for every run until it is repaired.
        logger.warning("DAG run index at {} is unreadable ({}); treating this session as having no runs", path, exc)
        return []
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


@dataclass(frozen=True)
class SessionNodes:
    """Every node id this session's runs have claimed, and what became of it.

    Two different questions are asked of this, and conflating them is what
    made a failed node look referenceable:

    - *is this id taken?* -- ``owner``. Every id a run ever claimed, whatever
      the outcome. A failed node still owns its id: its run directory exists
      and its prompt is on disk, so handing the name to something else would
      make the history ambiguous.
    - *can this id be read?* -- ``state``. Only a ``completed`` node has an
      output file to reference at all.

    Attributes:
        owner (`dict[str, str]`):
            Node id to the run that claimed it.
        state (`dict[str, str]`):
            Node id to ``"completed"``, ``"failed"``, ``"skipped"``, or one of
            :data:`RUNNING` / :data:`UNRECORDED` for a run whose per-node
            outcome the index does not carry.
    """

    owner: dict[str, str] = field(default_factory=dict)
    state: dict[str, str] = field(default_factory=dict)

    def is_readable(self, node_id: str) -> bool:
        """Whether ``node_id`` has an output file a later graph can reference."""
        return self.state.get(node_id) == "completed"


async def read_session_nodes(backend: Any, root: str) -> SessionNodes:
    """Read this session's node index: which ids are taken, and their outcome.

    Later entries win on the ids of any older ones, so a history written
    before ids were unique per session still resolves to the most recent
    producer instead of raising.

    Args:
        backend (`BackendBase`):
            The session workspace backend.
        root (`str`):
            The session's DAG history root.

    Returns:
        `SessionNodes`:
            The owner and state maps. Empty when the session has no history,
            and missing the runs recorded before node ids were indexed -- those
            are reachable only by file path (``@runs/<run_id>/<node>.out.md``).
    """
    owner: dict[str, str] = {}
    state: dict[str, str] = {}
    for entry in await read_index(backend, root):
        run_id = entry.get("run_id")
        if not isinstance(run_id, str):
            continue
        statuses = entry.get("status") if isinstance(entry.get("status"), dict) else {}
        # No outcome for a node means one of two different things, and they take
        # opposite advice: a run with a summary is over, so waiting is useless.
        absent = UNRECORDED if entry.get("summary") else RUNNING
        for node_id in entry.get("nodes") or ():
            if not isinstance(node_id, str):
                continue
            owner[node_id] = run_id
            state[node_id] = str(statuses.get(node_id, absent))
    return SessionNodes(owner=owner, state=state)


def make_run_id() -> str:
    """Build a unique, sortable run id.

    Shares ``history_stamp`` with ``make_call_id`` so the two id shapes stay
    comparable: the panel sorts spawns and graph runs into one list by id, and
    a second-accurate stamp on either side would reshuffle ties on every poll.

    Returns:
        `str`:
            ``"<UTC timestamp>-<8 hex>"``, e.g.
            ``"20260717T031500123456Z-1a2b3c4d"``.
    """
    from raven.agent.subagent_history import history_stamp

    return f"{history_stamp()}-{uuid.uuid4().hex[:8]}"


def node_live_key(run_id: str, node_id: str) -> str:
    """The live-index key a node's activity is collected under.

    Shared with the reader rather than spelled out on both sides: a spawn keys
    its activity by the record directory's name, and a node has no such
    directory, so the two namespaces are kept apart by this prefix.
    """
    return f"dag:{run_id}:{node_id}"


class DagRunStore:
    """Owns the on-disk layout for a single DAG run.

    All I/O goes through the workspace ``backend`` so it works for
    local, Docker, E2B, and remote backends alike.
    """

    def __init__(self, backend: Any, root: str, run_id: str) -> None:
        """Initialize the store.

        Args:
            backend (`BackendBase`):
                The session workspace backend.
            root (`str`):
                The session's DAG history root -- already the full
                ``.../subagents/mas_dag`` path, not a directory this
                class appends a marker to. Deliberately not the session working
                directory: a run record outlives whatever the working directory
                is pointed at (raven/agent/subagent_history.py).
            run_id (`str`):
                The unique id for this run.
        """
        self._backend = backend
        self._root = root
        self.run_id = run_id

    @property
    def root(self) -> str:
        """This session's DAG history root, holding every run dir.

        Exposed because a node may reference an earlier run in the same
        conversation (``@runs/<run_id>/...``), and those references resolve
        against this root rather than the working directory.
        """
        return self._root

    @property
    def run_dir(self) -> str:
        """The run-scoped directory ``<root>/<run_id>``."""
        return self._backend.join_path(self._root, self.run_id)

    def prompt_path(self, node_id: str) -> str:
        """Path of a node's rendered prompt file.

        Args:
            node_id (`str`):
                The node id.

        Returns:
            `str`:
                ``<run_dir>/<node_id>.prompt.md``.
        """
        return self._backend.join_path(self.run_dir, f"{node_id}.prompt.md")

    def output_path(self, node_id: str) -> str:
        """Path of a node's captured output file.

        Args:
            node_id (`str`):
                The node id.

        Returns:
            `str`:
                ``<run_dir>/<node_id>.out.md``.
        """
        return output_path_in(self._backend, self._root, self.run_id, node_id)

    def transcript_path(self, node_id: str) -> str:
        """Path of a node's own transcript file.

        Args:
            node_id (`str`):
                The node id.

        Returns:
            `str`:
                ``<run_dir>/<node_id>.transcript.jsonl``.
        """
        return self._backend.join_path(self.run_dir, f"{node_id}.transcript.jsonl")

    async def init(self, graph_json: str, node_ids: list[str] | None = None) -> None:
        """Create the run dir (implicitly) and persist ``graph.json``.

        The session index entry is claimed here, at the start, not at the end:
        a later graph may name this run's nodes by id, and the uniqueness rule
        that makes those ids addressable has to see a run that is still in
        flight -- otherwise two concurrent runs both pass the check and the
        session ends up with two nodes answering to one name. The caller must
        hold :func:`index_guard`, spanning its own read of the index through to
        this claim, for the same reason.

        Args:
            graph_json (`str`):
                The submitted graph spec, serialized as JSON.
            node_ids (`list[str] | None`):
                This run's node ids, recorded in the session index.
        """
        await self.write_text(
            self._backend.join_path(self.run_dir, "graph.json"),
            graph_json,
        )
        if node_ids is not None:
            await self.upsert_index({"run_id": self.run_id, "nodes": list(node_ids)})

    async def write_text(self, path: str, text: str) -> None:
        """Write UTF-8 text to ``path`` (parent dirs auto-created).

        Args:
            path (`str`):
                Destination path in the backend environment.
            text (`str`):
                The text to write.
        """
        await self._backend.write_file(path, text.encode("utf-8"))

    async def read_text(self, path: str) -> str:
        """Read UTF-8 text from ``path``.

        Args:
            path (`str`):
                Path in the backend environment.

        Returns:
            `str`:
                The decoded contents.
        """
        data = await self._backend.read_file(path)
        return data.decode("utf-8", errors="replace")

    async def write_manifest(self, manifest: dict) -> None:
        """Persist the run manifest as ``manifest.json``.

        Args:
            manifest (`dict`):
                Node id to status/metadata mapping.
        """
        await self.write_text(
            self._backend.join_path(self.run_dir, "manifest.json"),
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

    async def upsert_index(self, entry: dict) -> None:
        """Merge one run entry into the session-level ``index.json``.

        Upsert rather than append: this run's entry is written twice, once at
        ``init`` to claim its node ids and once at the end to record the
        summary, and the second must not leave a duplicate behind.

        The caller must hold :func:`index_guard` for this root. It is not taken
        here because the interesting section is wider than the write: whoever
        claims ids has to have read the index inside the same guard.

        Args:
            entry (`dict`):
                Fields to merge into this run's entry.
        """
        path = self._backend.join_path(self._root, INDEX_FILENAME)
        entries = await read_index(self._backend, self._root)
        for index, existing in enumerate(entries):
            if existing.get("run_id") == self.run_id:
                entries[index] = {**existing, **entry}
                break
        else:
            entries.append(entry)
        await self.write_text(
            path,
            json.dumps(entries, ensure_ascii=False, indent=2),
        )
