"""Run-time network grants and the running sandboxes each one must reach.

A person who allows a blocked connection "for this session" allows it for every
agent of that conversation, so the grant is applied to every running sandbox in
the conversation's scope and compiled into every sandbox created there later.
The main loop's sandbox is shared by every conversation the process serves; it
registers under the scope ``None`` and runs under every scope's grants.

Grants live in memory for the life of the process, the way the gate's session
grants do (``raven.permissions.session``); what should outlive it is a rule in
``permissions.network``. Everything here runs on the gateway's one event loop;
nothing locks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from raven.sandbox.network_policy import NetworkGrant, compile_network_policy

if TYPE_CHECKING:
    from raven.config.schema import NetworkPermissions


class GrantRegistry:
    """Grants per conversation scope, and the running sandboxes registered under each scope."""

    def __init__(self) -> None:
        self._grants: dict[str, set[NetworkGrant]] = {}
        self._executors: dict[int, tuple[Any, str | None]] = {}

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
        self._executors[id(executor)] = (executor, scope)

    def unregister(self, executor: Any) -> None:
        self._executors.pop(id(executor), None)

    def executors_for(self, scope: str) -> list[Any]:
        """The running sandboxes a grant in ``scope`` must reach: its own and the shared one."""
        return [executor for executor, own in self._executors.values() if own is None or own == scope]


GRANTS = GrantRegistry()


def network_source_for(
    network: Callable[[], NetworkPermissions], scope: str | None, *, registry: GrantRegistry | None = None
) -> Callable[[], dict[str, dict]]:
    """The network policy source of a sandbox in ``scope``: the live ``permissions.network`` plus the scope's grants."""
    grants = registry if registry is not None else GRANTS

    def source() -> dict[str, dict]:
        return compile_network_policy(network(), grants.grants_for(scope))

    return source
