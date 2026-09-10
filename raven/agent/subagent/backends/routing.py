"""One product entry fronting several implementations: the route is picked on ``run``.

A row declaring ``routes`` is the only row the dispatching model sees for the
work its targets do (they ship ``hidden``). Every caller that dispatches to the
row -- ``spawn``, a DAG node and its retries, a WebUI direct chat -- resolves
one backend by name and calls ``run`` on it, so this is the one seat where the
task text and the implementation meet, and the gate lives here rather than once
per caller.

``run`` reads the task as the dispatching model wrote it (``authored_task``;
the rendered ``task`` only where a caller has no other text): a lane may have
inlined whole files into the rendered task, and their contents are neither
routing evidence nor something to send through the host model again. It picks
in this order:

1. a reused ``instance`` handle continues the conversation it was opened on --
   the implementation whose transport already bound the handle in this
   session wins, and nothing is classified;
2. a route whose ``match`` pattern is found in the authored text, with file
   references (paths, ``@ref`` tokens, template placeholders) taken out first,
   takes it without a model call -- a task that names its deliverable cannot be
   misrouted, and a source that happens to be a deck is not a deck being asked
   for;
3. otherwise the host's classifier (set by the manager, which holds the host
   model) picks between the targets and this row's own implementation, reading
   the targets' roster lines only: the fronting row's line is written for the
   dispatching model and argues the opposite case;
4. no classifier, no answer, or an answer outside the set keeps the task on the
   row's own implementation -- a route can redirect, never lose, a task.

The wrapper is the row's backend as far as the host is concerned: binders and
config pushes are forwarded to every implementation (a target's own row is on
the table but nothing addresses it, so it would otherwise never be bound), and
every other attribute reads off the row's own implementation.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.subagent.backends.base import optional_keyword
from raven.contracts.subagent_backend import SubagentBackend

#: ``(menu, task, default) -> chosen name or None``; ``menu`` is ``[(name, description), ...]``.
Router = Callable[[list[tuple[str, str]], str, str], Awaitable[str | None]]

# What a file reference looks like in a task: a template placeholder, an @ref
# token, a POSIX or Windows path, or a bare filename with an extension. Taken out
# before a route pattern is searched, so ``/data/keynote.pptx`` names a source
# and ``a .pptx`` (no stem) still names a deliverable.
_FILE_REFERENCE_RE = re.compile(
    r"\{\{.*?\}\}|@\S+|(?<![\w])(?:~|\.{1,2})?/[^\s'\"<>]+|\b[A-Za-z]:\\[^\s'\"<>]+|\b[\w-]+\.[A-Za-z0-9]{1,5}\b",
    re.DOTALL,
)


def without_file_references(text: str) -> str:
    return _FILE_REFERENCE_RE.sub(" ", text)


_BROADCAST = (
    "bind_session_dir",
    "bind_event_sink",
    "bind_caps_listener",
    "bind_unprompted_announcer",
    "set_mcp_source",
    "repoint_pooled_resident",
)


class RoutingBackend(SubagentBackend):
    def __init__(
        self,
        name: str,
        primary: SubagentBackend,
        targets: list[tuple[str, str, str, SubagentBackend]],
        *,
        instances: Any,
    ) -> None:
        """``targets`` are ``(name, match pattern, description, backend)`` in route order."""
        self.name = name
        self._primary = primary
        self._targets = [(n, re.compile(m, re.IGNORECASE) if m else None, d, b) for n, m, d, b in targets]
        self._instances = instances
        self._router: Router | None = None

    @property
    def streams(self) -> bool:  # type: ignore[override]
        return bool(getattr(self._primary, "streams", False))

    def set_router(self, router: Router | None) -> None:
        self._router = router

    def implementations(self) -> list[SubagentBackend]:
        return [self._primary, *(b for _, _, _, b in self._targets)]

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr in _BROADCAST:
            return self._broadcast(attr)
        return getattr(self._primary, attr)

    def _broadcast(self, attr: str) -> Callable[..., None]:
        def call(*args: Any, **kwargs: Any) -> None:
            for backend in self.implementations():
                member = getattr(backend, attr, None)
                if callable(member):
                    member(*args, **kwargs)

        return call

    async def pick(self, task: str, *, session_key: str | None, instance: str | None) -> tuple[str, SubagentBackend]:
        """The ``(name, backend)`` that runs this task; see the module docstring for the order."""
        if instance and (bound := await self._bound(session_key, instance)) is not None:
            return bound
        phrasing = without_file_references(task)
        for name, pattern, _, backend in self._targets:
            if pattern is not None and pattern.search(phrasing):
                logger.info("{!r}: task names {!r}'s deliverable; routing there", self.name, name)
                return name, backend
        if self._router is not None and self._targets:
            menu = [(name, description) for name, _, description, _ in self._targets]
            try:
                answer = await self._router(menu, task, self.name)
            except Exception as exc:  # noqa: BLE001 - a routing failure must not lose the task
                logger.warning("{!r}: the route classifier failed; running here: {}", self.name, exc)
                answer = None
            for name, _, _, backend in self._targets:
                if answer == name:
                    logger.info("{!r}: classified for {!r}; routing there", self.name, name)
                    return name, backend
            if answer is not None and answer != self.name:
                logger.warning(
                    "{!r}: the route classifier answered {!r}, not a candidate; running here", self.name, answer
                )
        return self.name, self._primary

    async def _bound(self, session_key: str | None, instance: str) -> tuple[str, SubagentBackend] | None:
        """The implementation whose transport already holds this handle, if any.

        Read off the instance registry rows the transports write when a session
        is bound (``agentId`` set). The manager's own status row for the handle
        is written under this row's name before the first run and carries no
        session, so it is not evidence; a bound session under this row's name is.
        """
        try:
            records = self._instances.list_instances(session_key or "default")
        except Exception as exc:  # noqa: BLE001 - the registry is best-effort storage
            logger.warning("{!r}: could not read the instance registry: {}", self.name, exc)
            return None
        bound = {r.get("agent") for r in records if r.get("handle") == instance and r.get("agentId")}
        for name, _, _, backend in self._targets:
            if name in bound:
                return name, backend
        if self.name in bound:
            return self.name, self._primary
        return None

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
        authored_task: str | None = None,
        **kwargs: Any,
    ) -> str:
        _, backend = await self.pick(authored_task or task, session_key=session_key, instance=instance)
        return await backend.run(
            task,
            task_id=task_id,
            workspace=workspace,
            executor=executor,
            session_key=session_key,
            instance=instance,
            # The same hand-off the lanes make: an implementation typed against
            # the paper before this keyword existed is not handed it.
            **optional_keyword(backend, "authored_task", authored_task),
            **kwargs,
        )


__all__ = ["Router", "RoutingBackend", "without_file_references"]
