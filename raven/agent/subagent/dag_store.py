# -*- coding: utf-8 -*-
"""Backend-backed storage for one DAG run's message files."""

import asyncio
import json
import time
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

REGISTRY_FILENAME = "nodes.json"

# One lock per (event loop, history root). The registry is read-modify-written and
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
    """Serialize this process's reads and writes of one session's node registry.

    Every caller that reads the registry to decide something, and every caller
    that writes it, must hold this -- read/validate/claim is a check-then-act
    sequence, and splitting it lets two runs claim one node id. Not reentrant:
    :meth:`DagRunStore.record_nodes` and :meth:`DagRunStore.record_outcome`
    both expect the caller to hold it already.

    In-process only. Two processes sharing one session (two gateways) still
    race; closing that needs file locking, which this deliberately does not do.

    Args:
        root (`str`):
            The session's DAG history root.

    Yields:
        `None`:
            With the registry held for this root.
    """
    per_loop = _INDEX_LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = per_loop.get(root)
    if lock is None:
        lock = per_loop[root] = asyncio.Lock()
    async with lock:
        yield


# States a node can be in that no node is ever *written* with -- they describe
# what the registry says rather than what a run recorded.
#
# RUNNING: the run claimed the id at start and has not finalized. Still going,
# or died mid-flight.
# UNRECORDED: the run finished but left no per-node outcome, which is what a
# history written before outcomes were recorded looks like. Distinct from
# RUNNING because "wait for it" is the wrong advice for a run that is over.
RUNNING = "running"
UNRECORDED = "unrecorded"


def prompt_path_in(backend: Any, root: str, node_id: str) -> str:
    """Path of one node's rendered prompt file, for any node in this conversation.

    Args:
        backend (`BackendBase`):
            Backend supplying the environment's path semantics.
        root (`str`):
            The session's flat node root.
        node_id (`str`):
            The node id.

    Returns:
        `str`:
            ``<root>/<node_id>.prompt.md``.
    """
    return backend.join_path(root, f"{node_id}.prompt.md")


def output_path_in(backend: Any, root: str, node_id: str) -> str:
    """Path of one node's output file, for any node in this conversation.

    Args:
        backend (`BackendBase`):
            Backend supplying the environment's path semantics.
        root (`str`):
            The session's flat node root.
        node_id (`str`):
            The node id.

    Returns:
        `str`:
            ``<root>/<node_id>.out.md``.
    """
    return backend.join_path(root, f"{node_id}.out.md")


def memory_path_in(backend: Any, root: str, node_id: str) -> str:
    """Path of one node's distilled memory file.

    Args:
        backend (`BackendBase`):
            Backend supplying the environment's path semantics.
        root (`str`):
            The session's flat node root.
        node_id (`str`):
            The node id.

    Returns:
        `str`:
            ``<root>/<node_id>.memory.json``.
    """
    return backend.join_path(root, f"{node_id}.memory.json")


async def read_registry(backend: Any, root: str) -> dict:
    """This session's node registry, or an empty one when it cannot be read.

    Args:
        backend (`BackendBase`):
            The session workspace backend.
        root (`str`):
            The session's sub-agent history root.

    Returns:
        `dict`:
            ``{"nodes": {...}, "runs": [...]}``, with both keys always present.
    """
    empty: dict = {"nodes": {}, "runs": []}
    path = backend.join_path(root, REGISTRY_FILENAME)
    if not await backend.file_exists(path):
        return empty
    try:
        loaded = json.loads((await backend.read_file(path)).decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        # Logged, not swallowed silently: this file is what keeps node ids
        # unique per conversation, so reading it as empty quietly drops that
        # guarantee for every run until it is repaired.
        logger.warning("Node registry at {} is unreadable ({}); treating this session as having no nodes", path, exc)
        return empty
    if not isinstance(loaded, dict):
        return empty
    nodes = loaded.get("nodes")
    runs = loaded.get("runs")
    return {
        "nodes": nodes if isinstance(nodes, dict) else {},
        "runs": [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else [],
    }


async def write_registry(backend: Any, root: str, registry: dict) -> None:
    """Write the whole registry back.

    The caller must hold :func:`index_guard` for ``root``.
    """
    await backend.write_file(
        backend.join_path(root, REGISTRY_FILENAME),
        json.dumps(registry, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def fold_node_id(node_id: str) -> str:
    """The key ``node_id`` occupies in the filesystem's space, not the registry's.

    A node's artifacts are files named after its id, so uniqueness is the
    filesystem's to enforce and its key space is not the registry's: APFS (the
    macOS default) and NTFS fold case, and there ``Plan.out.md`` and
    ``plan.out.md`` are one file. Every check that asks "is this id taken"
    compares folded ids, and they all come here rather than each spelling
    ``.casefold()`` for itself -- three copies of a rule is three places to
    forget when it grows a second clause.
    """
    return node_id.casefold()


def duplicate_node_id(node_id: str, owner: str, *, readable: bool, taken_as: str | None = None) -> str:
    """Why ``node_id`` cannot be taken, and what to do instead.

    Shared by both delegation surfaces: one namespace answering in two voices
    would tell a model two different things about one rule. The advice branches
    on whether the node that owns the id left anything to reference -- renaming
    is the only move when it did not, but when it did, the caller most likely
    wanted to read that node rather than make a new one.

    ``taken_as`` is the id actually on the books when it differs from the one
    asked for, which happens only for a case collision. Naming it matters: a
    model told "'plan' is already used" while it can see no such node has any
    move left but to guess.

    ``owner`` is a run id for a graph's node and the claim's ``kind`` for a
    spawn, which has no run. Spelled out rather than dropped into "run '...'"
    for both: "already used by run 'spawn'" names a run that does not exist and
    sends the reader looking for it.
    """
    held = taken_as or node_id
    holder = {
        "spawn": "an earlier spawn in this conversation",
        UNRECORDED: "an earlier task this conversation no longer records a run for",
    }.get(owner, f"run '{owner}'")
    left_nothing = "that run left it" if owner not in ("spawn", UNRECORDED) else "it was left"
    advice = (
        f"Rename it, or drop this node and reference '{held}' directly (no depends_on needed)"
        if readable
        else f"Rename it -- {left_nothing} with no output, so there is nothing to reference either"
    )
    collision = (
        ""
        if taken_as is None or taken_as == node_id
        else f" (as '{taken_as}' -- a node's files are named after its id, and ids differing only in case are one file on macOS and Windows)"
    )
    return f"node id '{node_id}' is already used by {holder}{collision}; ids are unique per conversation. {advice}"


async def claim_node(backend: Any, root: str, node_id: str, *, kind: str, started_at_ms: int) -> None:
    """Take ``node_id`` for a task that has no run to claim it through.

    A spawn dispatches one task and has no run id, so it cannot go through
    :meth:`DagRunStore.record_nodes`. It writes no ``run_id``, which is what
    makes :attr:`SessionNodes.owner` fall through to the ``kind``.

    The caller must hold :func:`index_guard` for ``root``, spanning its own
    read of the registry through to this claim: read-then-claim split across
    two guards lets two callers both pass the uniqueness check.
    """
    registry = await read_registry(backend, root)
    registry["nodes"][node_id] = {"kind": kind, "status": RUNNING, "started_at_ms": started_at_ms}
    await write_registry(backend, root, registry)


async def ensure_node_claimed(backend: Any, root: str, node_id: str, *, kind: str, started_at_ms: int) -> None:
    """Claim ``node_id`` unless something already holds it.

    The delegation tool claims before it dispatches, so that a duplicate comes
    back as a refusal the model can fix in the same turn. A caller that reaches
    the manager directly -- the RPC lane, a test, anything not going through the
    tool -- has taken no such step, and a task with no registry entry is one no
    listing can draw and no later task can reference. This is that backstop, not
    a second copy of the uniqueness rule: it never refuses, and it leaves an
    existing claim exactly as the tool wrote it.

    The caller must hold :func:`index_guard` for ``root``.
    """
    registry = await read_registry(backend, root)
    folded = fold_node_id(node_id)
    if any(fold_node_id(taken) == folded for taken in registry["nodes"]):
        return
    registry["nodes"][node_id] = {"kind": kind, "status": RUNNING, "started_at_ms": started_at_ms}
    await write_registry(backend, root, registry)


async def release_node_claim(backend: Any, root: str, node_id: str) -> None:
    """Give ``node_id`` back, for a claim whose task never started.

    Only for the window between claiming an id and the dispatch being accepted:
    a refusal there leaves a task that never ran, wrote nothing and has no
    record, while its id would stay taken for the rest of the conversation. A
    task that *did* run keeps its id whatever became of it -- that is the
    uniqueness rule, and this must never be used to reopen one.

    The caller must hold :func:`index_guard` for ``root``.
    """
    registry = await read_registry(backend, root)
    if registry["nodes"].pop(node_id, None) is None:
        return
    await write_registry(backend, root, registry)


async def record_node_outcome(
    backend: Any, root: str, node_id: str, *, status: str, has_output: bool, ended_at_ms: int
) -> None:
    """Record what became of one node claimed by :func:`claim_node`.

    Merged into whatever the claim wrote rather than replacing it: dropping
    ``kind`` would leave the entry with neither it nor a ``run_id``, and the
    duplicate-id refusal names the owner it would then have lost.

    The caller must hold :func:`index_guard` for ``root``.
    """
    registry = await read_registry(backend, root)
    entry = registry["nodes"].get(node_id) or {}
    registry["nodes"][node_id] = {
        **entry,
        "status": status,
        "has_output": has_output,
        "ended_at_ms": ended_at_ms,
    }
    await write_registry(backend, root, registry)


@dataclass(frozen=True)
class SessionNodes:
    """Every node id this session's runs have claimed, and what became of it.

    Two different questions are asked of this, and conflating them is what
    made a failed node look referenceable:

    - *is this id taken?* -- ``owner``. Every id a run ever claimed, whatever
      the outcome. A failed node still owns its id: its run directory exists
      and its prompt is on disk, so handing the name to something else would
      make the history ambiguous.
    - *can this id be read?* -- ``state`` and ``has_output`` together. A
      ``completed`` node reports the run's verdict, which does not promise a
      file: a task can finish having written nothing. Recording that separately
      keeps the verdict honest instead of demoting it to ``failed``.

    Attributes:
        owner (`dict[str, str]`):
            Node id to the run that claimed it, or to the entry's ``kind`` for
            a node with no run.
        state (`dict[str, str]`):
            Node id to ``"completed"``, ``"failed"``, ``"skipped"``,
            ``"cancelled"``, or one of :data:`RUNNING` / :data:`UNRECORDED` for
            a run whose per-node outcome the registry does not carry.
        has_output (`dict[str, bool]`):
            Node id to whether an output file was written. Absent for an entry
            written before this field existed, which reads as ``False``.
    """

    owner: dict[str, str] = field(default_factory=dict)
    state: dict[str, str] = field(default_factory=dict)
    has_output: dict[str, bool] = field(default_factory=dict)

    def is_readable(self, node_id: str) -> bool:
        """Whether ``node_id`` has an output file a later task can reference."""
        return self.state.get(node_id) == "completed" and self.has_output.get(node_id, False)

    def claimed_by(self, node_id: str) -> tuple[str, str] | None:
        """The id on the books that ``node_id`` would collide with, and its owner.

        Exact first, then case-folded. Folded because a node's artifacts are
        files named after its id: on a case-folding backend (APFS by default,
        and NTFS) ``Plan.out.md`` and ``plan.out.md`` are one file, so the two
        nodes share an output and the second write destroys the first's. The
        flat namespace is what exposed this -- the run-scoped layout gave each
        of them its own directory.

        Refused rather than normalised: ``Plan`` and ``plan`` are two names the
        model chose deliberately, and silently making them one node loses one of
        the two tasks instead of telling anyone.

        Returns:
            `tuple[str, str] | None`:
                The taken id and its owner, or ``None`` when the id is free.
                The taken id is what to name in a refusal: it may differ in case
                from the one asked for.
        """
        if (owner := self.owner.get(node_id)) is not None:
            return node_id, owner
        folded = fold_node_id(node_id)
        for taken, taken_owner in self.owner.items():
            if fold_node_id(taken) == folded:
                return taken, taken_owner
        return None


async def read_session_nodes(backend: Any, root: str) -> SessionNodes:
    """Read this session's node registry: which ids are taken, and their outcome.

    Args:
        backend (`BackendBase`):
            The session workspace backend.
        root (`str`):
            The session's sub-agent history root.

    Returns:
        `SessionNodes`:
            The owner and state maps. Empty for a session with no registry --
            which includes every conversation that ran before the registry moved
            here, whose artifacts stay reachable by absolute path only.
    """
    owner: dict[str, str] = {}
    state: dict[str, str] = {}
    has_output: dict[str, bool] = {}
    for node_id, entry in (await read_registry(backend, root))["nodes"].items():
        if not isinstance(node_id, str) or not isinstance(entry, dict):
            continue
        owner[node_id] = str(entry.get("run_id") or entry.get("kind") or UNRECORDED)
        state[node_id] = str(entry.get("status") or UNRECORDED)
        has_output[node_id] = bool(entry.get("has_output"))
    return SessionNodes(owner=owner, state=state, has_output=has_output)


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
    from raven.agent.subagent.history import history_stamp

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

    All I/O goes through the DAG core's file ``backend`` so it works for
    local, Docker, E2B, and remote backends alike.
    """

    def __init__(self, backend: Any, root: str, run_id: str, nodes_root: str, registry_root: str) -> None:
        """Initialize the store.

        Args:
            backend (`BackendBase`):
                The DAG core's file backend.
            root (`str`):
                The session's DAG history root -- already the full
                ``.../subagents/mas_dag`` path. Holds this run's ``graph.json``
                and ``manifest.json``, which describe the run rather than any
                one node.
            run_id (`str`):
                The unique id for this run.
            nodes_root (`str`):
                The session's flat node root. Node artifacts go here rather than
                under ``run_dir`` so a node id locates its own files without a
                registry lookup telling a reader which run owned it.
            registry_root (`str`):
                The session's sub-agent history root, ``<session_dir>/subagents``
                -- where the node registry (``nodes.json``) lives. ``index_guard``
                locks this value, so every reader and writer of the registry
                serializes against the same root the registry actually lives
                under.
        """
        self._backend = backend
        self._root = root
        self.run_id = run_id
        self._nodes_root = nodes_root
        self._registry_root = registry_root

    @property
    def root(self) -> str:
        """This session's DAG history root, holding every run dir.

        Distinct from :attr:`registry_root`: this is where each run's own
        ``graph.json`` and ``manifest.json`` live, one directory per run,
        while the node registry that makes an id unique across the whole
        session lives at ``registry_root`` instead.
        """
        return self._root

    @property
    def nodes_root(self) -> str:
        """The session's flat node root, holding every node's artifacts."""
        return self._nodes_root

    @property
    def registry_root(self) -> str:
        """The session's sub-agent history root -- where the node registry lives."""
        return self._registry_root

    @property
    def run_dir(self) -> str:
        """The run-scoped directory ``<root>/<run_id>``."""
        return self._backend.join_path(self._root, self.run_id)

    def prompt_path(self, node_id: str) -> str:
        """Path of a node's rendered prompt file. See :func:`prompt_path_in`."""
        return prompt_path_in(self._backend, self._nodes_root, node_id)

    def attempt_prompt_path(self, node_id: str, attempt: int) -> str:
        """Path of one attempt's dispatched prompt, kept beside the latest.

        ``prompt_path`` stays fixed at the node's original task text forever
        (attempt 1's render); a continued node's follow-up-substituted prompt is
        archived here instead of overwriting it, the same way ``output_path`` and
        ``attempt_output_path`` split "latest" from "this attempt".

        Args:
            node_id (`str`):
                The node id.
            attempt (`int`):
                1-based attempt number.

        Returns:
            `str`:
                ``<nodes_root>/<node_id>.attempt-<n>.prompt.md``.
        """
        return self._backend.join_path(self._nodes_root, f"{node_id}.attempt-{attempt}.prompt.md")

    def output_path(self, node_id: str) -> str:
        """Path of a node's output file. See :func:`output_path_in`."""
        return output_path_in(self._backend, self._nodes_root, node_id)

    def attempt_output_path(self, node_id: str, attempt: int) -> str:
        """Path of one attempt's captured output, kept beside the latest.

        Args:
            node_id (`str`):
                The node id.
            attempt (`int`):
                1-based attempt number.

        Returns:
            `str`:
                ``<nodes_root>/<node_id>.attempt-<n>.out.md``.
        """
        return self._backend.join_path(self._nodes_root, f"{node_id}.attempt-{attempt}.out.md")

    def transcript_path(self, node_id: str) -> str:
        """Path of a node's own transcript file: ``<nodes_root>/<node_id>.transcript.jsonl``."""
        return self._backend.join_path(self._nodes_root, f"{node_id}.transcript.jsonl")

    def attempt_transcript_path(self, node_id: str, attempt: int) -> str:
        """Path of one attempt's transcript, kept beside the latest.

        ``transcript_path`` is overwritten by each attempt because that is what
        the judge reads -- it judges the attempt in front of it. The evidence a
        *previous* attempt's verdict rested on would otherwise be gone, while
        that attempt's prompt and output both remain auditable, so it is archived
        the same way they are.

        Args:
            node_id (`str`):
                The node id.
            attempt (`int`):
                1-based attempt number.

        Returns:
            `str`:
                ``<nodes_root>/<node_id>.attempt-<n>.transcript.jsonl``.
        """
        return self._backend.join_path(self._nodes_root, f"{node_id}.attempt-{attempt}.transcript.jsonl")

    def memory_path(self, node_id: str) -> str:
        """Path of a node's distilled memory file. See :func:`memory_path_in`."""
        return memory_path_in(self._backend, self._nodes_root, node_id)

    async def init(self, graph_json: str, node_ids: list[str] | None = None) -> None:
        """Create the run dir (implicitly) and persist ``graph.json``.

        The session's node registry entry is claimed here, at the start, not
        at the end: a later graph may name this run's nodes by id, and the
        uniqueness rule that makes those ids addressable has to see a run that
        is still in flight -- otherwise two concurrent runs both pass the
        check and the session ends up with two nodes answering to one name.
        The caller must hold :func:`index_guard`, spanning its own read of the
        registry through to this claim, for the same reason.

        Args:
            graph_json (`str`):
                The submitted graph spec, serialized as JSON.
            node_ids (`list[str] | None`):
                This run's node ids, recorded in the session's node registry.
        """
        await self.write_text(
            self._backend.join_path(self.run_dir, "graph.json"),
            graph_json,
        )
        if node_ids is not None:
            await self.record_nodes(node_ids)

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

    async def record_nodes(self, node_ids: list[str]) -> None:
        """Claim this run's node ids in the registry, and list the run itself.

        The caller must hold :func:`index_guard` for ``registry_root``, spanning
        its own read through to this claim: read-then-claim split across two
        guards lets two runs both pass the uniqueness check.
        """
        registry = await read_registry(self._backend, self._registry_root)
        claimed_at = int(time.time() * 1000)
        for node_id in node_ids:
            registry["nodes"][node_id] = {
                "kind": "dag",
                "run_id": self.run_id,
                "status": RUNNING,
                "started_at_ms": claimed_at,
            }
        # The run entry lands here too, not only at `record_outcome`:
        # `session_run_ids` reads it and is intersected with the loop's live
        # runs to scope what the control tools may list and cancel, so a run
        # recorded only at finalize is invisible for exactly as long as it is
        # running -- the whole window those tools exist for.
        if not any(run.get("run_id") == self.run_id for run in registry["runs"]):
            registry["runs"].append({"run_id": self.run_id, "nodes": sorted(node_ids)})
        await self._write_registry(registry)

    async def wrote_output(self, node_id: str) -> bool:
        """Whether this node left an output file behind.

        Asked of the filesystem rather than derived from the outcome string:
        the two disagree for a task that completed and wrote nothing, and that
        disagreement is the whole reason the registry carries both.
        """
        return await self._backend.file_exists(self.output_path(node_id))

    async def record_outcome(self, status: dict[str, str], summary: str) -> None:
        """Record each node's outcome, whether it wrote output, and the summary.

        The caller must hold :func:`index_guard` for ``registry_root``.
        """
        registry = await read_registry(self._backend, self._registry_root)
        ended_at = int(time.time() * 1000)
        for node_id, outcome in status.items():
            entry = registry["nodes"].get(node_id) or {"kind": "dag", "run_id": self.run_id}
            registry["nodes"][node_id] = {
                **entry,
                "status": outcome,
                "has_output": await self.wrote_output(node_id),
                "ended_at_ms": ended_at,
            }
        registry["runs"] = [r for r in registry["runs"] if r.get("run_id") != self.run_id]
        registry["runs"].append({"run_id": self.run_id, "summary": summary, "nodes": sorted(status)})
        await self._write_registry(registry)

    async def _write_registry(self, registry: dict) -> None:
        await write_registry(self._backend, self._registry_root, registry)

    async def record_replan(self, entry: dict) -> None:
        """Note on this run's ``graph.json`` that it was replanned, and into what.

        A reserved top-level key beside the spec's own, not a wrapper around it.
        Four of the five readers of this file take the keys they want out of a raw
        dict (``dag_reader``, ``instance_records``, and the two rpc methods), so
        the addition is inert to them. The fifth is not: ``dag_tool``'s
        ``prepare_replan`` parses the file through ``SubAgentDagSpec``, which is
        ``extra="forbid"`` and rejects this key outright -- which is why it strips
        it before parsing, and why the key must stay out of that model.
        """
        path = self._backend.join_path(self.run_dir, "graph.json")
        graph = json.loads(await self.read_text(path))
        graph["replan"] = entry
        await self.write_text(path, json.dumps(graph, ensure_ascii=False))
