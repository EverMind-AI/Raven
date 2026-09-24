"""``import.*`` RPC handlers: the onboarding wizard's data-sync step.

Named ``import_sync`` rather than ``import`` because the latter is a Python
keyword. These four handlers are a thin RPC skin over the same cold-start
importer ``raven import`` already drives (``raven.importer.*``): the web
wizard's sync step scans, starts, polls and cancels the identical pipeline the
CLI's ``raven import`` command wraps in questionary prompts and a Rich
progress bar. Only one import runs at a time inside this process: the slot is
claimed before ``import.run`` awaits anything, so two frames dispatched together
cannot both pass the guard and submit the same source twice.

After the message pass the run lands the same two phases the CLI does
(``raven.importer.phases``): the profile mirror and the skill install. Their
progress is this process's own knowledge, reported through ``import.status``
beside the counts the state file holds.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.core.plugin_stack import (
    SHIPPED_DEFAULT_BACKEND,
    everos_platform_note,
    everos_plugin_installed,
    everos_plugin_missing_note,
    maybe_build_memory_backend,
    memory_enabled,
)
from raven.importer.orchestrator import ProgressEvent, run_import
from raven.importer.phases import run_phases, skill_source_for
from raven.importer.scanners import build_scanners, scan_all
from raven.importer.skills import SkillOrigin
from raven.importer.state import ImportState
from raven.importer.types import Platform, Scanner, ScanResult, SourceKind, Tier, filter_by_tier
from raven.rpc.errors import ConfigValidationError

if TYPE_CHECKING:
    from raven.config.raven import RavenConfig
    from raven.config.schema import Config
    from raven.contracts.llm_provider import LLMProvider
    from raven.contracts.memory import MemoryBackend
    from raven.rpc.dispatcher import Dispatcher

# One import at a time inside this gateway process. The rpc server dispatches
# frames concurrently, and ``import.run`` awaits a scan and a backend start
# before it has a task to hold, so the claim is a flag set with no await between
# the guard and it; the task takes over once it exists and clears the slot in
# its own ``finally``.
_TASK: asyncio.Task | None = None
_STARTING = False
# The post-import phase in flight, as ``import.status`` reports it. Set by the
# running task's progress callback and cleared with the slot: the state file
# knows nothing of the phases, so this is the only place their progress lives.
_PHASE: dict[str, Any] | None = None
# The source the message pass is on and how far into it: a large source is
# many batches, and the per-source counts do not move for any of them.
_CURRENT: dict[str, Any] | None = None


def _busy() -> bool:
    return _STARTING or (_TASK is not None and not _TASK.done())


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


def _load_configs() -> tuple["Config", "RavenConfig"]:
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config

    return load_config(), load_raven_config()


def _profile_provider(config: "Config") -> "LLMProvider | None":
    """Best-effort provider for the profile heading classifier.

    ``make_provider`` raises when no LLM credentials are configured; the
    import must still land in that case, under the mirror's fallback heading.
    """
    from raven.providers.factory import make_provider

    try:
        return make_provider(config)
    except Exception as exc:
        logger.info("import.run: no LLM provider for the profile mirror ({}); using the fallback heading", exc)
        return None


async def _skill_count(platform: Platform) -> int:
    """How many skills an import of ``platform`` would install; 0 when it has none to offer.

    Skills are directories, not message sources, so no scan counts them; the
    installer leaves factory copies alone, and so does this count.
    """
    source = skill_source_for(platform)
    if source is None:
        return 0
    try:
        discovered = await source.discover()
    except Exception as exc:
        logger.warning("import: {} skill preview unavailable: {}", platform.value, exc)
        return 0
    return sum(1 for skill in discovered if skill.origin is not SkillOrigin.BUNDLED_PRISTINE)


async def import_scan(params: dict) -> dict:
    """``import.scan`` -- what each platform holds, and whether import can run."""
    del params
    config, ec_config = _load_configs()
    workspace = config.workspace_path

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
    for p in Platform:
        counts[p]["skills"] = await _skill_count(p)

    ready = memory_enabled(workspace, ec_config)
    reason = "" if ready else _no_backend_reason(ec_config)
    if ready and ec_config.memory.backend == SHIPPED_DEFAULT_BACKEND:
        # A configured EverOS on a platform it cannot run on would earn the
        # wizard's sync step and fail it at the first write; say so here, and
        # the step is not offered.
        platform_note = everos_platform_note()
        if platform_note is not None:
            ready, reason = False, platform_note
    platforms = [{"platform": p.value, "scannable": p in scannable, **counts[p]} for p in Platform]
    return {"ready": ready, "reason": reason, "platforms": platforms}


async def import_run(params: dict) -> dict:
    """``import.run`` -- start a background import, or say why it did not."""
    global _TASK, _STARTING

    try:
        tier = Tier(params["tier"])
    except ValueError as exc:
        raise ConfigValidationError(f"unknown import tier: {params.get('tier')!r}") from exc
    try:
        requested = {Platform(p) for p in params["platforms"]}
    except ValueError as exc:
        raise ConfigValidationError(f"unknown platform in {params.get('platforms')!r}") from exc

    if _busy():
        return {"started": False, "total": 0, "detail": "an import is already running"}
    _STARTING = True
    try:
        state = _state()
        # Cleared here, under the claim: a stop that lands while this run is
        # still starting is then seen by the run's first poll rather than lost.
        state.cancel_path.unlink(missing_ok=True)

        config, ec_config = _load_configs()
        workspace = config.workspace_path
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
        # Skills never travel as ScanResults: a platform whose only importable
        # data is skills has no items and still has a run to make.
        skills = sum([await _skill_count(p) for p in requested])
        if not items and not skills:
            return {"started": False, "total": 0, "detail": "nothing to import"}

        # The memory backend serves the message pass alone; a skills-only run
        # has nothing to send it, and starting it would start a service for
        # nothing.
        if items:
            await backend.start()
            health = await backend.health()
            if health is not None and not health.ready:
                await backend.stop()
                hints = [f"{c.label}: {c.hint or c.status}" for c in health.checks if c.status != "ok" or c.hint]
                detail = "; ".join(hints) or "memory service is not ready"
                return {"started": False, "total": 0, "detail": detail}

        state.set_total(
            len(items),
            keys=[f"{r.platform.value}:{r.source_key}" for _, r in items],
            tier=tier.value,
            platforms=[p.value for p in requested],
        )

        def _on_phase(kind: str, current: int, total: int) -> None:
            global _PHASE
            _PHASE = {"kind": kind, "current": current, "total": total}

        def _on_batch(platform: str, source_key: str, sent: int, total: int) -> None:
            global _CURRENT
            _CURRENT = {"platform": platform, "source_key": source_key, "sent": sent, "total": total}

        def _on_progress(_event: ProgressEvent) -> None:
            # The source this fires for is settled, and the state file counts it
            # from here on. Leaving its last batch report standing would have a
            # reader add the same source twice -- the row would reach 100% with
            # sources still to send, then fall back when the next one starts.
            global _CURRENT
            _CURRENT = None

        async def _run(backend: "MemoryBackend", started: bool) -> None:
            global _TASK, _PHASE, _CURRENT
            try:
                summary = await run_import(
                    items,
                    backend,
                    state,
                    on_progress=_on_progress,
                    on_batch=_on_batch,
                    cancel_path=state.cancel_path,
                )
                _CURRENT = None
                # A stop has to stop the run, not hand it its two longest steps.
                if not summary.cancelled:
                    await run_phases(
                        items,
                        workspace,
                        state,
                        provider=_profile_provider(config),
                        model=config.agents.defaults.model,
                        platforms=requested,
                        on_phase=_on_phase,
                        cancel_path=state.cancel_path,
                    )
            except Exception:
                logger.exception("import.run: the background import failed")
            finally:
                if started:
                    try:
                        await backend.stop()
                    except Exception:
                        logger.exception("import.run: the memory backend did not stop cleanly")
                _PHASE = None
                _CURRENT = None
                _TASK = None

        _TASK = asyncio.create_task(_run(backend, bool(items)))
        return {"started": True, "total": len(items), "detail": ""}
    finally:
        _STARTING = False


async def import_status(params: dict) -> dict:
    """``import.status`` -- this process's own knowledge plus the state file's counts.

    Counted over one scope: the keys the last ``import.run`` recorded, so a
    subset run is not measured against every source an earlier run left in
    the file. A run the CLI started records no keys, and then every entry is
    in scope, the way ``raven import status`` counts.
    """
    del params
    running = _busy()
    progress = _state().get_progress()
    entries = {k: v for k, v in progress.get("entries", {}).items() if ":" in k}
    meta = progress.get("meta", {})
    keys = meta.get("keys")
    scope = list(keys) if keys is not None else list(entries)
    total = len(scope) if keys is not None else meta.get("total", len(entries))

    # A source is counted or named, never both. A source that failed is sent
    # again by the next run while its entry still says failed, and a reader
    # adding the share of a named source to a count that already holds it draws
    # the same source twice; leaving it out of the count instead keeps its own
    # progress visible for the whole of the retry.
    current = dict(_CURRENT) if running and _CURRENT is not None else None
    in_flight = f"{current['platform']}:{current['source_key']}" if current is not None else None

    submitted = 0
    failed = 0
    by_platform: dict[str, dict[str, int]] = {}
    for key in scope:
        platform = key.split(":", 1)[0]
        bucket = by_platform.setdefault(platform, {"total": 0, "submitted": 0, "failed": 0})
        bucket["total"] += 1
        status = None if key == in_flight else entries.get(key, {}).get("status")
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
        "phase": dict(_PHASE) if running and _PHASE is not None else None,
        "current": current,
        "phases": dict(meta["phases"]) if isinstance(meta.get("phases"), dict) else None,
        "tier": meta.get("tier"),
        "platforms": list(meta.get("platforms") or sorted(by_platform)),
    }


async def import_stop(params: dict) -> dict:
    """``import.stop`` -- touch the cancel file the running import polls for."""
    del params
    if not _busy():
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
