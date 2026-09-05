"""Backend resolution from campaign state -- the tool layer's extension seam.

A campaign's ``meta.json`` names the backend its jobs run on; this maps that name
to a factory, so the tools never hardcode one. Docker over SSH is built in and
stays the default (a campaign written before this seam existed has no ``backend``
key and still resolves). Two kinds of client plug in here:

  - **execution adapters** -- a Slurm/cloud backend (SkyPilot) becomes a factory
    registration, not a change to every tool;
  - **bare processes** -- GPU training on a shared host is a plain command, not a
    container: the model and data sit on a network mount and the driver stack
    belongs to the host, so wrapping it in an image would only wrap a command
    that already runs.

Connection-name resolution (a campaign that names an owner-registered machine
instead of carrying an address) stays a seam: the trunk connections module is
deliberately not a contracts paper (verdict feature 13 / C2), so the plugin
carries its own read-only reader over the same store
(``oncall_flow.connections``, the seam-ruling shape) and that reader is the
default resolver here. A host may still install a different one with
:func:`set_connection_resolver` -- the part-2a seam, kept -- and installing
``None`` restores the default rather than disabling resolution.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from oncall_flow.backend import JobBackend

DEFAULT_BACKEND = "docker"

BackendFactory = Callable[[dict[str, Any]], JobBackend]

_FACTORIES: dict[str, BackendFactory] = {}

_CONNECTION_RESOLVER: "Callable[[dict[str, Any]], dict[str, Any]] | None" = None


def register_backend(name: str, factory: BackendFactory) -> None:
    """Make ``name`` resolvable from a campaign's meta. Re-registering replaces."""
    _FACTORIES[name] = factory


def set_connection_resolver(resolve: "Callable[[dict[str, Any]], dict[str, Any]] | None") -> None:
    """Install how a campaign that names a connection gets its address.

    The fork resolved through its connections registry at this one seam so
    every backend keeps reading the same meta keys it always did; the plugin
    keeps the seam. With nothing installed the plugin's own reader answers
    (``oncall_flow.connections.resolve_into``), which is the seam ruling's
    landing: the registry file is the host's, the reading of it is ours.
    """
    global _CONNECTION_RESOLVER
    _CONNECTION_RESOLVER = resolve


def _resolve(meta: dict[str, Any]) -> dict[str, Any]:
    if _CONNECTION_RESOLVER is not None:
        return _CONNECTION_RESOLVER(meta)
    from oncall_flow.connections import resolve_into

    return resolve_into(meta)


def backend_name(meta: dict[str, Any]) -> str:
    return str(meta.get("backend") or DEFAULT_BACKEND)


def backend_from_meta(meta: dict[str, Any]) -> JobBackend:
    # A campaign that names a connection gets its address from there rather than
    # repeating it. Resolved at this one seam so every backend keeps reading the
    # same meta keys it always did, and a campaign with no connection is handed
    # its meta untouched.
    meta = _resolve(meta)
    name = backend_name(meta)
    try:
        factory = _FACTORIES[name]
    except KeyError:
        known = ", ".join(sorted(_FACTORIES)) or "none"
        raise ValueError(f"unknown ops backend {name!r} (registered: {known})") from None
    return factory(meta)


def prepare_from_meta(meta: dict[str, Any], *, app_dir: str) -> None:
    """Round-zero setup for backends that need it; a no-op for those that don't."""
    if backend_name(meta) != DEFAULT_BACKEND:
        return
    from oncall_flow.docker_backend import make_ssh_runner
    from oncall_flow.runner import make_ssh_sync, prepare_remote

    # Same seam as backend_from_meta: a campaign that names a connection has no
    # address of its own, and reading meta["host"] here raised KeyError on the
    # first campaign an agent created for itself (2026-08-18).
    meta = _resolve(meta)
    key_path = os.path.expanduser(meta.get("key", "~/.ssh/id_rsa"))
    host, port = meta["host"], int(meta.get("port", 22))
    remote_dir = meta.get("remote_dir", "/root/raven-ops")
    prepare_remote(
        make_ssh_runner(host, port, key_path),
        make_ssh_sync(host, port, key_path),
        image=meta.get("image", "python:3.12-slim"),
        app_local=os.path.abspath(app_dir) + "/",
        app_remote=f"{remote_dir}/app",
    )


def _process_from_meta(meta: dict[str, Any]) -> JobBackend:
    from oncall_flow.process_backend import process_from_meta

    return process_from_meta(meta)


def _openfoam_from_meta(meta: dict[str, Any]) -> JobBackend:
    from oncall_flow.openfoam_backend import openfoam_from_meta

    return openfoam_from_meta(meta)


def _docker_from_meta(meta: dict[str, Any]) -> JobBackend:
    # Imported inside the factory so the module-level name stays patchable by
    # tests that swap the SSH runner.
    from oncall_flow.docker_backend import DockerExecutor, make_ssh_runner

    run = make_ssh_runner(
        meta["host"],
        int(meta.get("port", 22)),
        os.path.expanduser(meta.get("key", "~/.ssh/id_rsa")),
        user=str(meta.get("user") or "root"),
    )
    return DockerExecutor(
        run,
        image=meta.get("image", "python:3.12-slim"),
        remote_dir=meta.get("remote_dir", "/root/raven-ops"),
    )


register_backend(DEFAULT_BACKEND, _docker_from_meta)
register_backend("process", _process_from_meta)
register_backend("openfoam", _openfoam_from_meta)
