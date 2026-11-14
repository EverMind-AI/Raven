"""Run-time network grants and the running sandboxes each one must reach.

A person who allows a blocked connection "for this session" allows it for every
agent of that conversation, so the grant is applied to every running sandbox in
the conversation's scope and compiled into every sandbox created there later.
The main loop's sandbox is shared by every conversation the process serves; it
registers under the scope ``None`` and runs under every scope's grants.

Grants live in memory for the life of the process, the way the gate's session
grants do (``raven.permissions.session``); what should outlive it is a rule in
``permissions.network``. A sandbox is held weakly, so one whose run is over
leaves the registry with its last reference and nobody has to remember to take
it out. Everything here runs on the gateway's one event loop; nothing locks.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from raven.sandbox.network_policy import NetworkGrant, compile_network_policy

if TYPE_CHECKING:
    from raven.config.schema import NetworkPermissions


class GrantRegistry:
    """Grants per conversation scope, and the running sandboxes registered under each scope."""

    def __init__(self) -> None:
        self._grants: dict[str, set[NetworkGrant]] = {}
        self._executors: dict[int, tuple[weakref.ref[Any], str | None]] = {}

    def grant(self, scope: str, grant: NetworkGrant) -> None:
        # Ignored without a scope, as ``remember_allowed`` does: an empty scope
        # would otherwise reach the shared sandbox, which runs under every scope.
        if scope:
            self._grants.setdefault(scope, set()).add(grant)

    def grants_for(self, scope: str | None) -> frozenset[NetworkGrant]:
        """The grants a sandbox in ``scope`` runs under; ``None`` (the shared sandbox) gets them all."""
        if scope is None:
            return frozenset().union(*self._grants.values())
        return frozenset(self._grants.get(scope, ()))

    def register(self, executor: Any, scope: str | None) -> None:
        key = id(executor)
        self._executors[key] = (weakref.ref(executor, lambda ref: self._forget(key, ref)), scope)

    def unregister(self, executor: Any) -> None:
        self._executors.pop(id(executor), None)

    def _forget(self, key: int, ref: weakref.ref[Any]) -> None:
        # Only the entry this reference made: the id may already name a newer sandbox.
        if self._executors.get(key, (None,))[0] is ref:
            del self._executors[key]

    def is_shared(self, executor: Any) -> bool:
        """Whether ``executor`` is the sandbox every conversation runs in (registered under ``None``)."""
        entry = self._executors.get(id(executor))
        return entry is not None and entry[0]() is executor and entry[1] is None

    def executors_for(self, scope: str) -> list[Any]:
        """The running sandboxes a grant in ``scope`` must reach: its own and the shared one."""
        reached = []
        for ref, own in list(self._executors.values()):
            executor = ref()
            if executor is not None and (own is None or own == scope):
                reached.append(executor)
        return reached


GRANTS = GrantRegistry()


def network_source_for(
    network: Callable[[], NetworkPermissions], scope: str | None, *, registry: GrantRegistry | None = None
) -> Callable[[], dict[str, dict]]:
    """The network policy source of a sandbox in ``scope``: the live ``permissions.network`` plus the scope's grants."""
    grants = registry if registry is not None else GRANTS

    def source() -> dict[str, dict]:
        return compile_network_policy(network(), grants.grants_for(scope))

    return source
