"""Channel manager for coordinating chat channels.

Construction + lifecycle only. Outbound delivery is the spine's
DeliveryHub/Outlet (a ChannelOutletAdapter per channel registered by the
gateway); inbound is each channel's Intake -> scheduler.submit.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, distribution
from typing import Any

from loguru import logger

from raven.channels.contract import Channel
from raven.config.schema import Config


def _missing_dep_hint() -> str:
    """How to install missing channel SDKs, tailored to the install mode.

    An editable (dev) checkout uses ``uv sync``; a wheel/tool install has no
    source tree, so it must re-run the installer instead. PEP 610
    ``direct_url.json`` distinguishes them -- a wheel install records
    ``archive_info`` (no ``dir_info`` key), so ``.get`` chaining avoids a
    KeyError when it is absent. This runs while a channel is already failing,
    so a missing/corrupt file must degrade to the installer hint, never raise.

    The dev hint names the umbrella ``channels`` extra and passes
    ``--inexact`` on purpose: ``uv sync`` is an exact sync, so syncing one
    channel's extra uninstalls every other channel's SDK on the way in.
    """
    editable = False
    try:
        raw = distribution("raven").read_text("direct_url.json")
        if raw:
            editable = bool(json.loads(raw).get("dir_info", {}).get("editable", False))
    except (PackageNotFoundError, ValueError):
        pass

    if editable:
        return "Run: uv sync --inexact --extra channels"
    if sys.platform == "win32":
        return "Re-run the installer to add channels: irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex"
    return "Re-run the installer to add channels: curl -fsSL https://raven.evermind.ai/install.sh | bash"


def missing_dependency_channels(config: Config) -> list[str]:
    """Enabled channels whose SDK is not installed, in registry order.

    Runs the same probe as :meth:`ChannelManager._init_channels` -- build via
    the spec factory, catch ImportError -- so a channel reported here is
    exactly one the gateway would disable at start. Every other construction
    failure is somebody else's diagnosis, not a missing dependency.

    Read-only callers (``channels status``, ``doctor``) use this so an enabled
    channel that can never start is visible before the gateway is run.
    """
    from raven.channels.registry import discover_specs

    missing: list[str] = []
    for modname, spec in discover_specs().items():
        section = getattr(config.channels, modname, None)
        if not section or not getattr(section, "enabled", False):
            continue
        try:
            spec.factory(section)
        except ImportError:
            missing.append(modname)
        except Exception:
            continue
    return missing


class ChannelManager:
    """Manages chat channels: construct enabled adapters, start/stop, status."""

    def __init__(self, config: Config):
        self.config = config
        self.channels: dict[str, Channel] = {}
        # Hot-started channels' run tasks, held so nothing collects them: the
        # loop keeps only a weak reference to a task nobody awaits.
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Set by the gateway wiring to register the new channel's outlet on the
        # DeliveryHub. A callback rather than the hub itself, so this module
        # stays unaware of the spine -- and so a channel started after launch
        # can still deliver a reply, which is the whole difference between a
        # channel that receives and one that only listens.
        self.on_started: Callable[[Channel], None] | None = None
        # The other half of it. A channel that has delivered once leaves a
        # resident outlet worker holding the adapter it started with, so
        # stopping one without retiring its outlet meant the next start
        # received on the new adapter and replied through the stopped one.
        self.on_stopped: Callable[[str], Awaitable[None]] | None = None

        self._init_channels()

    def _init_channels(self) -> None:
        """Initialize enabled channels from their declarative ``ChannelSpec``.

        Each adapter's ``spec.factory`` defers the heavy SDK import, so a
        missing channel dependency surfaces here as an ImportError and disables
        just that channel.
        """
        from raven.channels.registry import discover_specs

        groq = self.config.providers.get("groq")
        groq_key = getattr(groq, "api_key", "") or ""

        for modname, spec in discover_specs().items():
            section = getattr(self.config.channels, modname, None)
            if not section or not getattr(section, "enabled", False):
                continue
            try:
                channel = spec.factory(section)
                channel.transcription_api_key = groq_key
                self.channels[modname] = channel
                logger.info("{} channel enabled", spec.display_name)
            except ImportError as e:
                logger.warning(
                    "{} channel disabled: missing dependency ({}). {}",
                    modname,
                    e,
                    _missing_dep_hint(),
                )

        self._validate_allow_from()

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    async def _start_channel(self, name: str, channel: Channel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.error("Failed to start channel {}: {}", name, e)

    async def start_all(self) -> None:
        """Start all channels (they run forever). Outbound delivery is the
        spine outlets', not this manager's."""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """Stop all channels."""
        logger.info("Stopping all channels...")
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except Exception as e:
                logger.error("Error stopping {}: {}", name, e)

    async def start_one(self, name: str) -> str:
        """Build and start one channel that config now enables, without a restart.

        The page enables an entrance by writing config in its own process, and
        the adapter lives here -- so before this existed, turning a channel on
        did nothing until the gateway was restarted: no QR was ever fetched, and
        a scan-login entrance could not be signed into from the UI at all.

        Answers a word rather than raising, because every outcome is a state the
        caller draws: ``started``, ``already``, ``unknown`` (no such channel),
        ``disabled`` (config does not enable it), ``deny_all`` (empty allowFrom,
        which start-up treats as fatal and a live gateway must not), or
        ``missing_dep``.

        The section is re-read from disk, not taken from ``self.config``: that is
        the snapshot this gateway launched with, and in it the channel is still
        off.
        """
        if name in self.channels:
            return "already"
        from raven.channels.registry import discover_specs
        from raven.config.loader import load_config

        spec = discover_specs().get(name)
        if spec is None:
            return "unknown"
        section = getattr(load_config().channels, name, None)
        if section is None or not getattr(section, "enabled", False):
            return "disabled"
        if getattr(section, "allow_from", None) == []:
            return "deny_all"
        try:
            channel = spec.factory(section)
        except ImportError as e:
            logger.warning("{} channel not started: missing dependency ({}). {}", name, e, _missing_dep_hint())
            return "missing_dep"
        groq = self.config.providers.get("groq")
        channel.transcription_api_key = getattr(groq, "api_key", "") or ""
        self.channels[name] = channel
        if self.on_started is not None:
            try:
                self.on_started(channel)
            except Exception as e:
                logger.error("Failed to register outlet for channel {}: {}", name, e)
        logger.info("Starting {} channel (enabled while running)...", name)
        task = asyncio.create_task(self._start_channel(name, channel))
        self._tasks[name] = task
        task.add_done_callback(lambda _t, n=name: self._tasks.pop(n, None))
        return "started"

    async def stop_one(self, name: str) -> str:
        """Stop one channel and drop its adapter. ``stopped`` or ``absent``.

        The outlet is retired with it, through ``on_stopped``. Leaving it
        registered looked safe -- a stopped channel receives nothing, so there
        is no turn left to reply to -- but the hub's worker is resident and
        holds the adapter it started with, so the next start of this channel
        would have received on the new adapter and replied through this one.
        """
        channel = self.channels.pop(name, None)
        if channel is None:
            return "absent"
        try:
            await channel.stop()
            logger.info("Stopped {} channel", name)
        except Exception as e:
            logger.error("Error stopping {}: {}", name, e)
        task = self._tasks.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
        if self.on_stopped is not None:
            try:
                await self.on_stopped(name)
            except Exception as e:
                logger.error("Failed to retire outlet for channel {}: {}", name, e)
        return "stopped"

    def get_channel(self, name: str) -> Channel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {name: {"enabled": True, "running": channel.is_running} for name, channel in self.channels.items()}

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
