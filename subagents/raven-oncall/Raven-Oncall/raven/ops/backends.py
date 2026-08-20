"""Backend resolution from campaign state -- the tool layer's extension seam.

A campaign's ``meta.json`` names the backend its jobs run on; this maps that name
to a factory, so the tools never hardcode one. Docker over SSH is built in and
stays the default (a campaign written before this seam existed has no ``backend``
key and still resolves). Two kinds of client plug in here:

  - **execution adapters** -- a Slurm/cloud backend (SkyPilot) becomes a factory
    registration, not a change to every tool;
  - **the scripted eval world** -- an evaluation points a campaign at a
    time-compressed fake backend, which is how the full loop (submit, wake,
    reconcile, triage, chart) gets measured without waiting out real jobs;
  - **bare processes** -- GPU training on a shared host is a plain command, not a
    container: the model and data sit on a network mount and the driver stack
    belongs to the host, so wrapping it in an image would only wrap a command
    that already runs.

``prepare_from_meta`` is the same idea for round-zero setup: only Docker needs an
app-dir sync and image pull, so that knowledge lives here rather than as a
backend check inside the tools.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from raven.ops.backend import JobBackend

DEFAULT_BACKEND = "docker"

BackendFactory = Callable[[dict[str, Any]], JobBackend]

_FACTORIES: dict[str, BackendFactory] = {}


def register_backend(name: str, factory: BackendFactory) -> None:
    """Make ``name`` resolvable from a campaign's meta. Re-registering replaces."""
    _FACTORIES[name] = factory


def backend_name(meta: dict[str, Any]) -> str:
    return str(meta.get("backend") or DEFAULT_BACKEND)


def backend_from_meta(meta: dict[str, Any]) -> JobBackend:
    # A campaign that names a connection gets its address from there rather than
    # repeating it. Resolved at this one seam so every backend keeps reading the
    # same meta keys it always did, and a campaign with no connection is handed
    # its meta untouched.
    from raven.ops.connections import resolve_into

    meta = resolve_into(meta)
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
    from raven.ops import make_ssh_runner, make_ssh_sync, prepare_remote
    from raven.ops.connections import resolve_into

    # Same seam as backend_from_meta: a campaign that names a connection has no
    # address of its own, and reading meta["host"] here raised KeyError on the
    # first campaign an agent created for itself (2026-08-18).
    meta = resolve_into(meta)
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
    from raven.ops.process_backend import process_from_meta

    return process_from_meta(meta)


def _openfoam_from_meta(meta: dict[str, Any]) -> JobBackend:
    from raven.ops.openfoam_backend import openfoam_from_meta

    return openfoam_from_meta(meta)


def _docker_from_meta(meta: dict[str, Any]) -> JobBackend:
    # Imported inside the factory so the module-level name stays patchable by
    # tests that swap the SSH runner.
    from raven.ops import DockerExecutor, make_ssh_runner

    run = make_ssh_runner(
        meta["host"], int(meta.get("port", 22)), os.path.expanduser(meta.get("key", "~/.ssh/id_rsa")),
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
