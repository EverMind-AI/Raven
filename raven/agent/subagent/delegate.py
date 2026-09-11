"""The worker table one turn dispatches through, and the scope that holds it.

Here rather than beside the generator that writes it, because the two halves
answer to different layers: writing a table is an optional capability
(``raven.playbook``, which an install may not have) while reading one is part
of dispatch, and dispatch may not know an optional capability at module level.
So the readers -- ``SpawnTool`` and the turn scope -- import this, and the
generator fills it.

A generated agent playbook names the workers this turn may hand work to: a
label, the roster agent behind it, and the charter that label carries. Two
labels may name the same agent with different charters, which is how one query
gets a researcher for each competitor without registering an agent per pair.

Task-scoped rather than an attribute, for the reason ``ToolRegistry``'s own
per-turn state is: a Lane runs one turn at a time per conversation but
conversations run at once, and a table on the tool or the manager would be read
by whichever turn happened to look. ``delegate_scope(None)`` is the off switch
and costs a ``ContextVar`` set that resolves to the same absent table every
reader already handles.

Two things travel per worker, and only one of them is text. The brief reaches
the sub-agent as a preamble on the task it is given, so what that buys is a
worker that knows its job before it starts. The charter beside it
(``charter.py``) is read by the worker in its own process and is enforcement:
its tool list joins that worker's withheld set, which ``ToolRegistry.execute``
reads on every dispatch, and its checks are judged there before a call runs.

Which is to say the enforcement is real but it is not *this* module's, and not
this process's. Nothing here refuses anything: this table decides who the
dispatching model may hand work to, and the payload it carries is applied --
narrowed against what that install already allows -- by the worker that
receives it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Worker:
    """One label this turn may dispatch to."""

    label: str
    agent: str
    """The roster name behind the label. Every dispatch lane downstream --
    ``_resolve_backend``, the instance registry, the DAG tool -- is given this
    and never the label, because a label resolves to no backend."""

    brief: str = ""
    """One line on what this worker is for, shown to the dispatching model in
    the tool's own enum so the task it writes matches the charter."""

    charter: str = ""
    """The preamble prepended to the task this worker is given. Rendered from
    the sub-playbook once, at generation time, rather than re-derived per
    dispatch: two dispatches to one label are the same worker."""

    payload: Mapping[str, Any] | None = None
    """The same brief in the shape the worker's own process reads, shipped with
    the dispatch so a Raven worker can hold its turn to it rather than merely be
    told about it. ``None`` for a worker that cannot take one -- a third-party
    agent has no Raven modules to bind it to, and sending it anyway would put a
    key on the wire that nothing reads."""


@dataclass(frozen=True)
class DelegateTable:
    """The workers of one turn, by label."""

    workers: Mapping[str, Worker] = field(default_factory=dict)

    def get(self, label: str) -> Worker | None:
        return self.workers.get(label)

    def labels(self) -> list[str]:
        return sorted(self.workers)

    def __bool__(self) -> bool:
        return bool(self.workers)


_TABLE: ContextVar[DelegateTable | None] = ContextVar("playbook_delegate_table", default=None)


@contextmanager
def delegate_scope(table: DelegateTable | None) -> Iterator[None]:
    """Bind ``table`` for the current task, and only there.

    ``None`` binds nothing: every reader treats an absent table as "no playbook
    this turn" and takes the path it took before this module existed, so the
    feature being off is not a branch anybody has to remember to write.
    """
    token = _TABLE.set(table or None)
    try:
        yield
    finally:
        _TABLE.reset(token)


def current_delegate() -> DelegateTable | None:
    """This turn's worker table, or ``None`` outside any scope."""
    return _TABLE.get()


__all__ = [
    "DelegateTable",
    "Worker",
    "current_delegate",
    "delegate_scope",
    "dispatch_charter",
    "outbound_charter",
]


_DISPATCH: ContextVar[Mapping[str, Any] | None] = ContextVar("playbook_dispatch_charter", default=None)


@contextmanager
def dispatch_charter(payload: Mapping[str, Any] | None) -> Iterator[None]:
    """Hold the charter one dispatch should carry, for the duration of it.

    A ContextVar rather than a parameter for the reason ``resolve_mode`` gives
    about its own value: the transport that would have to carry it is
    ``SubagentBackend.run``, whose signature is frozen contract, and a field
    added there would have to be understood by every backend including the ones
    that have nothing to bind it to. The transport that *can* carry it asks for
    it instead -- see ``outbound_charter``.
    """
    token = _DISPATCH.set(payload or None)
    try:
        yield
    finally:
        _DISPATCH.reset(token)


def outbound_charter() -> Mapping[str, Any] | None:
    """The charter this dispatch carries, asked for by the transport sending it.

    ``None`` outside a dispatch and for a worker that brought none, which is
    what keeps the wire exactly as it was when no playbook is in play: a
    transport that gets ``None`` adds no key.
    """
    return _DISPATCH.get()
