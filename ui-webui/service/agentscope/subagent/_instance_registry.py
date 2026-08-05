# -*- coding: utf-8 -*-
"""Per-session registry mapping instance handles to CLI session ids."""

from typing import Any


class SessionInstanceRegistry:
    """Resolves a sub-agent instance handle to a stable CLI session id.

    Scoped to one chat session. Backed by a storage object exposing
    ``get_subagent_instance`` / ``upsert_subagent_instance`` (duck-typed
    so this module does not import ``app.storage``). Persistence is
    deferred: the tool calls :meth:`lookup` to decide create-vs-resume
    and only :meth:`commit` after a create run succeeds, so a failed
    create never poisons the handle.
    """

    def __init__(self, storage: Any, session_id: str) -> None:
        """Initialize the registry.

        Args:
            storage (`Any`):
                Storage backend with ``get_subagent_instance`` and
                ``upsert_subagent_instance`` coroutines.
            session_id (`str`):
                The chat session this registry is scoped to.
        """
        self._storage = storage
        self._session_id = session_id

    async def lookup(self, handle: str) -> str | None:
        """Return the CLI session id bound to ``handle``, or ``None``.

        Args:
            handle (`str`):
                The agent-chosen instance handle.

        Returns:
            `str | None`:
                The stored ``agent_id`` when the handle is known, else
                ``None`` (the caller should run the create command).
        """
        record = await self._storage.get_subagent_instance(
            self._session_id,
            handle,
        )
        return record.agent_id if record is not None else None

    async def commit(
        self,
        handle: str,
        agent_id: str,
        prototype_name: str,
    ) -> None:
        """Persist ``handle -> agent_id`` after a successful create.

        Args:
            handle (`str`):
                The agent-chosen instance handle.
            agent_id (`str`):
                The CLI session id to bind to the handle.
            prototype_name (`str`):
                The prototype ``name`` this instance is of.
        """
        await self._storage.upsert_subagent_instance(
            self._session_id,
            handle,
            agent_id,
            prototype_name,
        )
