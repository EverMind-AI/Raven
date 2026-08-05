# -*- coding: utf-8 -*-
"""The local workspace manager."""

import asyncio
import os
import time

from typing_extensions import deprecated

from ..._logging import logger
from ..._utils._common import _generate_id
from ...workspace import LocalWorkspace
from ._base import IsolationPolicy, WorkspaceManagerBase


class LocalWorkspaceManager(WorkspaceManagerBase):
    """Manages LocalWorkspace instances with TTL-based lazy lifecycle.

    Workspaces are keyed by ``workspace_id`` in the cache. On cache miss
    the manager reconstructs the workspace at ``basedir/<workspace_id>``
    (so the on-disk layout follows the configured
    :class:`IsolationPolicy`), unless an explicit ``workdir`` override is
    supplied — the workdir is deterministic for local workspaces so no
    storage lookup is needed.
    """

    def __init__(
        self,
        basedir: str,
        *,
        isolation: IsolationPolicy = IsolationPolicy.PER_AGENT,
        default_mcps: list | None = None,
        skill_paths: list[str] | None = None,
        ttl: float = 3600.0,
    ) -> None:
        """Initialize the local workspace manager.

        Args:
            basedir (`str`):
                Root directory under which per-agent workdir are
                created.
            isolation (`IsolationPolicy`, defaults to `PER_AGENT`):
                Isolation grain for :meth:`assign_workspace_id`. See
                :class:`IsolationPolicy` for semantics.
            default_mcps (`list | None`, optional):
                MCP clients seeded into brand-new workspaces.
            skill_paths (`list[str] | None`, optional):
                Skill directories seeded into brand-new workspaces.
            ttl (`float`, defaults to `3600.0`):
                Seconds before an idle cached workspace is evicted.
        """
        self._basedir = os.path.abspath(basedir)
        self._default_mcps = default_mcps or []
        self._skill_paths = skill_paths or []
        self._ttl = ttl
        # workspace_id → (workspace, last_access_monotonic)
        self._cache: dict[str, tuple[LocalWorkspace, float]] = {}
        self._lock = asyncio.Lock()
        super().__init__(isolation=isolation)

    def _pop_expired(self, now: float) -> list[LocalWorkspace]:
        """Pop every cache entry whose last-access exceeds ``ttl``.

        Caller is responsible for closing the returned workspaces
        *outside* the manager lock so a slow ``close()`` does not stall
        unrelated ``get_workspace`` callers.
        """
        expired_ids = [
            wid for wid, (_, ts) in self._cache.items() if now - ts > self._ttl
        ]
        return [self._cache.pop(wid)[0] for wid in expired_ids]

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
        *,
        workdir: str | None = None,
    ) -> LocalWorkspace:
        """Return an initialized workspace, reconstructing from disk on
        cache miss.

        The on-disk workdir defaults to ``basedir/<workspace_id>`` so the
        layout follows the configured isolation policy. An explicit
        ``workdir`` override (the session's ``work_dir``) wins; a cached
        workspace whose ``workdir`` no longer matches the requested one is
        evicted and rebuilt so a mid-session change takes effect.

        Mirrors the Docker / E2B managers' double-check pattern: a first
        lock acquisition handles the cache-hit fast path and collects
        expired entries; expired / stale entries are then closed in
        parallel *outside* the lock; on a miss a second acquisition runs
        ``initialize()`` while holding the lock so two concurrent cache
        misses for the same ``workspace_id`` cannot create two workspaces.

        Args:
            user_id (`str`):
                Accepted for interface parity; not used here.
            agent_id (`str`):
                The agent id (used only to derive a fallback
                ``workspace_id`` when none is supplied).
            session_id (`str`):
                Accepted for interface parity; not used here.
            workspace_id (`str | None`, optional):
                Explicit workspace binding and cache key. ``None``
                triggers the :meth:`assign_workspace_id` fallback.
            workdir (`str | None`, optional):
                Explicit working-directory override. When set it wins
                over ``basedir/<workspace_id>``.

        Returns:
            `LocalWorkspace`:
                The initialized workspace.
        """
        del user_id, session_id  # accepted for interface parity

        if workspace_id is None:
            workspace_id = self.assign_workspace_id(
                user_id="",
                agent_id=agent_id,
                session_id="",
            )

        target_workdir = os.path.abspath(
            workdir if workdir else os.path.join(self._basedir, workspace_id),
        )

        # Phase 1: cache hit (matching workdir) + collect expired / stale.
        async with self._lock:
            now = time.monotonic()
            expired = self._pop_expired(now)
            stale: LocalWorkspace | None = None
            hit: LocalWorkspace | None = None
            cached = self._cache.get(workspace_id)
            if cached is not None:
                ws, _ = cached
                if ws.workdir == target_workdir:
                    self._cache[workspace_id] = (ws, now)
                    hit = ws
                else:
                    # The override changed — evict and rebuild below.
                    stale = self._cache.pop(workspace_id)[0]

        # Phase 2: close expired + stale entries outside the lock, in
        # parallel, so a slow stdio MCP shutdown does not block callers.
        to_close = list(expired)
        if stale is not None:
            to_close.append(stale)
        if to_close:
            await asyncio.gather(
                *(self._safe_close(ws) for ws in to_close),
                return_exceptions=True,
            )

        if hit is not None:
            return hit

        # Phase 3: build under the lock to prevent two concurrent
        # get_workspace(workspace_id=X) calls from creating two workspaces
        # for the same id.
        orphan: LocalWorkspace | None = None
        async with self._lock:
            cached = self._cache.get(workspace_id)
            if cached is not None and cached[0].workdir == target_workdir:
                ws, _ = cached
                self._cache[workspace_id] = (ws, time.monotonic())
                return ws
            if cached is not None:
                # A concurrent call built a workspace with a different
                # workdir for this id; evict it here and close it below
                # (outside the lock) so the mismatched instance is not
                # leaked.
                orphan = self._cache.pop(workspace_id)[0]

            ws = LocalWorkspace(
                workspace_id=workspace_id,
                workdir=target_workdir,
                default_mcps=self._default_mcps,
                skill_paths=self._skill_paths,
            )
            await ws.initialize()
            self._cache[workspace_id] = (ws, time.monotonic())

        if orphan is not None:
            await self._safe_close(orphan)
        return ws

    @deprecated(
        "LocalWorkspaceManager.create_workspace is deprecated; "
        "use get_workspace(workspace_id=None) instead.",
        category=None,
    )
    async def create_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> LocalWorkspace:
        """Create a new workspace for the given agent and return it.

        .. deprecated::
            Use :meth:`get_workspace` with ``workspace_id=None`` — it
            falls back to :meth:`assign_workspace_id` under the
            manager's isolation policy and reuses the cache path.
        """
        del user_id, agent_id, session_id  # accepted for interface parity

        workspace_id = _generate_id()
        workdir = os.path.join(self._basedir, workspace_id)
        os.makedirs(workdir, exist_ok=True)
        ws = LocalWorkspace(
            workspace_id=workspace_id,
            workdir=workdir,
            default_mcps=self._default_mcps,
            skill_paths=self._skill_paths,
        )
        await ws.initialize()
        async with self._lock:
            self._cache[ws.workspace_id] = (ws, time.monotonic())
        return ws

    async def close(self, workspace_id: str) -> None:
        """Close and evict a single workspace from the cache."""
        async with self._lock:
            entry = self._cache.pop(workspace_id, None)
        if entry is None:
            return
        ws, _ = entry
        await self._safe_close(ws)

    async def close_all(self) -> None:
        """Close every cached workspace in parallel.

        Stdio MCP shutdown can be slow per workspace; doing it
        sequentially on app shutdown produces a noticeable stall, so
        we fan the calls out with :func:`asyncio.gather` (mirrors the
        Docker / E2B managers).
        """
        async with self._lock:
            entries = list(self._cache.values())
            self._cache.clear()
        if not entries:
            return
        await asyncio.gather(
            *(self._safe_close(ws) for ws, _ in entries),
            return_exceptions=True,
        )

    @staticmethod
    async def _safe_close(ws: LocalWorkspace) -> None:
        """Close a workspace, logging any failure instead of raising."""
        try:
            await ws.close()
        except Exception:
            logger.exception(
                "Failed to close LocalWorkspace %s",
                ws.workspace_id,
            )
