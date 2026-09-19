"""``import.*`` RPC handlers: the onboarding wizard's data-sync step.

Named ``import_sync`` rather than ``import`` because the latter is a Python
keyword. These four handlers are a thin RPC skin over the same cold-start
importer ``raven import`` already drives (``raven.importer.*``): the web
wizard's sync step scans, starts, polls and cancels the identical pipeline the
CLI's ``raven import`` command wraps in questionary prompts and a Rich
progress bar. Only one import runs at a time inside this process -- a single
module-level task slot enforces that, mirroring the single-flight registry
``raven.rpc.methods.subagents`` keeps for ``subagents.test``.

Unlike the CLI's ``run`` command, ``import.run`` never lands the Hermes
``user.md`` mirror or installs Hermes skills: those are CLI-only extras
(``raven.cli.import_commands._land_hermes_user_md`` /
``_install_hermes_skills``), and the wizard's sync step covers memory files
and conversations only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from raven.core.plugin_stack import (
    SHIPPED_DEFAULT_BACKEND,
    everos_plugin_installed,
    everos_plugin_missing_note,
    maybe_build_memory_backend,
    memory_enabled,
)
from raven.importer.orchestrator import run_import
from raven.importer.scanners import build_scanners, scan_all
from raven.importer.state import ImportState
from raven.importer.types import Platform, Scanner, ScanResult, SourceKind, Tier, filter_by_tier
from raven.rpc.errors import ConfigValidationError

if TYPE_CHECKING:
    from raven.config.raven import RavenConfig
    from raven.contracts.memory import MemoryBackend
    from raven.rpc.dispatcher import Dispatcher

# One import at a time inside this gateway process, tracked the way
# ``subagents._RUNNING`` tracks its own single-flight calls: a live task means
# a run is in progress, cleared in the task's own ``finally``.
_TASK: asyncio.Task | None = None


def _state() -> ImportState:
    """The state file this run tracks -- a seam a test replaces with a tmp path."""
    return ImportState()


def _no_backend_reason(ec_config: "RavenConfig") -> str:
    """Why nothing backs an import, distinguishing the two ways that happens.

    Mirrors the branch ``raven.cli.import_commands._build_and_run`` takes on a
    ``None`` backend: the shipped default was never swapped out and its
    distribution simply is not installed, or nothing was ever selected.
    """
    if ec_config.memory.backend == SHIPPED_DEFAULT_BACKEND and not everos_plugin_installed():
        return everos_plugin_missing_note()
    return "no memory backend is configured; finish the memory step of onboarding first"


def _load_workspace_and_config() -> tuple[Path, "RavenConfig"]:
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config

    workspace = load_config().workspace_path
    return workspace, load_raven_config()


async def import_scan(params: dict) -> dict:
    """``import.scan`` -- what each platform holds, and whether import can run."""
    del params
    workspace, ec_config = _load_workspace_and_config()

    def _on_scan_error(platform: Platform, error: BaseException) -> None:
        logger.warning("import.scan: {} scan failed: {}", platform.value, error)

    results = await scan_all(on_error=_on_scan_error)
    scannable = {s.platform for s in build_scanners()}

    counts: dict[Platform, dict[str, int]] = {
        p: {"memory_files": 0, "conversations": 0, "estimated_size": 0} for p in Platform
    }
    for r in results:
        bucket = counts[r.platform]
        if r.kind == SourceKind.MEMORY_FILE:
            bucket["memory_files"] += 1
        elif r.kind == SourceKind.CONVERSATION:
            bucket["conversations"] += 1
        bucket["estimated_size"] += r.estimated_size

    ready = memory_enabled(workspace, ec_config)
    platforms = [{"platform": p.value, "scannable": p in scannable, **counts[p]} for p in Platform]
    return {"ready": ready, "reason": "" if ready else _no_backend_reason(ec_config), "platforms": platforms}


async def import_run(params: dict) -> dict:
    """``import.run`` -- start a background import, or say why it did not."""
    global _TASK

    try:
        tier = Tier(params["tier"])
    except ValueError as exc:
        raise ConfigValidationError(f"unknown import tier: {params.get('tier')!r}") from exc
    try:
        requested = {Platform(p) for p in params["platforms"]}
    except ValueError as exc:
        raise ConfigValidationError(f"unknown platform in {params.get('platforms')!r}") from exc

    if _TASK is not None and not _TASK.done():
        return {"started": False, "total": 0, "detail": "an import is already running"}

    workspace, ec_config = _load_workspace_and_config()
    from raven.core.plugin_stack import build_plugin_registry

    registry = build_plugin_registry(ec_config)
    backend = maybe_build_memory_backend(workspace, ec_config, registry=registry)
    if backend is None:
        return {"started": False, "total": 0, "detail": _no_backend_reason(ec_config)}

    all_results = await scan_all()
    filtered = [r for r in all_results if r.platform in requested]
    tiered = filter_by_tier(filtered, tier)
    scanner_map = {s.platform: s for s in build_scanners()}
    items: list[tuple[Scanner, ScanResult]] = [
        (scanner_map[r.platform], r) for r in tiered if r.platform in scanner_map
    ]
    if not items:
        return {"started": False, "total": 0, "detail": "nothing to import"}

    await backend.start()
    health = await backend.health()
    if health is not None and not health.ready:
        await backend.stop()
        hints = [f"{c.label}: {c.hint or c.status}" for c in health.checks if c.status != "ok" or c.hint]
        detail = "; ".join(hints) or "memory service is not ready"
        return {"started": False, "total": 0, "detail": detail}

    state = _state()
    if state.cancel_path.exists():
        state.cancel_path.unlink(missing_ok=True)
    state.set_total(len(items))

    async def _run(backend: "MemoryBackend") -> None:
        global _TASK
        try:
            await run_import(items, backend, state, cancel_path=state.cancel_path)
        finally:
            await backend.stop()
            _TASK = None

    _TASK = asyncio.create_task(_run(backend))
    return {"started": True, "total": len(items), "detail": ""}


async def import_status(params: dict) -> dict:
    """``import.status`` -- this process's own knowledge plus the state file's counts."""
    del params
    running = _TASK is not None and not _TASK.done()
    progress = _state().get_progress()
    entries = {k: v for k, v in progress.get("entries", {}).items() if ":" in k}
    meta = progress.get("meta", {})
    total = meta.get("total", len(entries))

    submitted = 0
    failed = 0
    by_platform: dict[str, dict[str, int]] = {}
    for key, entry in entries.items():
        platform = key.split(":", 1)[0]
        bucket = by_platform.setdefault(platform, {"total": 0, "submitted": 0, "failed": 0})
        bucket["total"] += 1
        status = entry.get("status")
        if status == "submitted":
            submitted += 1
            bucket["submitted"] += 1
        elif status == "failed":
            failed += 1
            bucket["failed"] += 1

    return {
        "running": running,
        "total": total,
        "submitted": submitted,
        "failed": failed,
        "by_platform": by_platform,
    }


async def import_stop(params: dict) -> dict:
    """``import.stop`` -- touch the cancel file the running import polls for."""
    del params
    running = _TASK is not None and not _TASK.done()
    if not running:
        return {"stopped": False}
    _state().cancel_path.touch()
    return {"stopped": True}


def register_import_methods(dispatcher: "Dispatcher") -> None:
    """Register the ``import.*`` methods on a dispatcher instance."""
    dispatcher.register("import.scan", import_scan)
    dispatcher.register("import.run", import_run)
    dispatcher.register("import.status", import_status)
    dispatcher.register("import.stop", import_stop)


__all__ = [
    "import_scan",
    "import_run",
    "import_status",
    "import_stop",
    "register_import_methods",
]
