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

from raven.channels.contract import Channel, ChannelSpec
from raven.config.schema import Config
from raven.providers.transcription import transcription_api_key


def missing_dep_hint() -> str:
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
    from raven.config.admission import dispense_channel_config

    missing: list[str] = []
    for modname, spec in discover_specs().items():
        section = getattr(config.channels, modname, None)
        if not section or not getattr(section, "enabled", False):
            continue
        try:
            # Dispensed like the real build: on the raw section a factory that
            # reads declared cargo fails before its SDK import, and the swallow
            # below would mask the missing dependency this probe exists to see.
            spec.factory(dispense_channel_config(spec, section, channel=modname))
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
        # One lock per channel name. Starting and stopping both rebuild the
        # entry under that name and both await inside it, and the page fires
        # either without waiting for the last one, so unserialised they
        # interleave: the stop resumes after the start has installed a new
        # adapter and cancels its task and retires its outlet by name, leaving
        # a channel that reads as running with nothing listening.
        self._locks: dict[str, asyncio.Lock] = {}
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

        for modname, spec in discover_specs().items():
            section = getattr(self.config.channels, modname, None)
            if not section or not getattr(section, "enabled", False):
                continue
            try:
                self.channels[modname] = self._build_channel(modname, spec, section)
                logger.info("{} channel enabled", spec.display_name)
            except ImportError as e:
                logger.warning(
                    "{} channel disabled: missing dependency ({}). {}",
                    modname,
                    e,
                    missing_dep_hint(),
                )

        self._validate_allow_from()

    def _build_channel(self, name: str, spec: ChannelSpec, section: Any) -> Channel:
        """The one build path every entrance shares: dispense, then factory.

        Storage handover: every declaring channel's factory receives the
        door-dispensed view (declared keys from the admitted slice, socket
        fields from the central section, frozen); an undeclaring channel keeps
        the verbatim section. Raises what the door or the factory raises --
        ImportError for a missing SDK, PluginConfigError for cargo the
        declaration refuses -- and each entrance draws its own state from it.
        """
        from raven.config.admission import dispense_channel_config

        section = dispense_channel_config(spec, section, channel=name)
        channel = spec.factory(section)
        channel.transcription_api_key = transcription_api_key(self.config)
        return channel

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    def _lock(self, name: str) -> asyncio.Lock:
        """The lock serialising this channel's start and stop."""
        lock = self._locks.get(name)
        if lock is None:
            lock = self._locks[name] = asyncio.Lock()
        return lock

    def _forget_task(self, name: str, task: asyncio.Task[None]) -> None:
        """Drop a start task's record, unless the name already holds another.

        A cancelled task's done callback runs a tick after the cancel, by which
        time a restart has installed its own task under the same name; popping
        by name alone threw that record away, and a start with no record looks
        exactly like one that has finished.
        """
        if self._tasks.get(name) is task:
            del self._tasks[name]

    async def _start_channel(self, name: str, channel: Channel) -> None:
        """Run one channel's start, leaving nothing behind that claims it runs.

        Adapters set ``_running`` before the first call that can fail (a
        rejected token raises from inside ``start()``), so logging the
        exception and walking away left the object in the table reading as
        running: the row drew green, and every later attempt met
        ``start_one``'s "already". The teardown is best effort -- an adapter
        that never finished coming up often refuses to be stopped -- so the
        flag is cleared whatever ``stop()`` did with it.
        """
        try:
            await channel.start()
        except Exception as e:
            logger.error("Failed to start channel {}: {}", name, e)
            try:
                await channel.stop()
            except Exception as stop_error:
                logger.warning("Error stopping {} after a failed start: {}", name, stop_error)
            mark_stopped = getattr(channel, "mark_stopped", None)
            if mark_stopped is not None:
                mark_stopped()

    async def start_all(self) -> None:
        """Start all channels (they run forever). Outbound delivery is the
        spine outlets', not this manager's."""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            task = asyncio.create_task(self._start_channel(name, channel))
            # Recorded like a hot start's, so `start_one` can tell an adapter
            # that is coming up from one whose start has finished and left it
            # not running. Without this the launch path was invisible here and
            # the two states looked identical.
            self._tasks[name] = task
            task.add_done_callback(lambda t, n=name: self._forget_task(n, t))
            tasks.append(task)

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

        The page enables an entrance by writing config in its own process while
        the adapter lives here, so the gateway builds and starts it on the spot
        rather than at the next restart.

        Answers a word rather than raising, because every outcome is a state the
        caller draws: ``started``, ``already``, ``unknown`` (no such channel),
        ``disabled`` (config does not enable it), ``deny_all`` (empty allowFrom,
        which start-up treats as fatal and a live gateway must not),
        ``missing_dep``, or ``bad_config`` (the door refused the section's
        cargo; the log names the owner and key).

        The section is re-read from disk, not taken from ``self.config``: that is
        the snapshot this gateway launched with, and in it the channel is still
        off.
        """
        async with self._lock(name):
            return await self._start_one(name)

    async def _start_one(self, name: str) -> str:
        """``start_one`` without the lock, so ``restart_one`` holds one lock
        across both halves."""
        # An adapter in the table is not the same as an adapter that works: a
        # scan login nobody completed leaves the object here with `is_running`
        # false (weixin gives up after the code expires three times), and
        # answering "already" to that would make "connect" on a stopped entrance
        # a no-op for the life of the process. A start still in flight is a
        # different thing and keeps its "already": the launch path is a task per
        # channel that only returns when the adapter stops, so a page write
        # arriving mid-launch must not tear down what is coming up.
        existing = self.channels.get(name)
        if existing is not None:
            task = self._tasks.get(name)
            if (task is not None and not task.done()) or getattr(existing, "is_running", False):
                return "already"
            await self._stop_one(name)
        from raven.channels.registry import discover_specs
        from raven.config.admission import PluginConfigError
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
            channel = self._build_channel(name, spec, section)
        except ImportError as e:
            logger.warning("{} channel not started: missing dependency ({}). {}", name, e, missing_dep_hint())
            return "missing_dep"
        except PluginConfigError as e:
            logger.warning("{} channel not started: {}", name, e)
            return "bad_config"
        self.channels[name] = channel
        if self.on_started is not None:
            try:
                self.on_started(channel)
            except Exception as e:
                logger.error("Failed to register outlet for channel {}: {}", name, e)
        logger.info("Starting {} channel (enabled while running)...", name)
        task = asyncio.create_task(self._start_channel(name, channel))
        self._tasks[name] = task
        task.add_done_callback(lambda t, n=name: self._forget_task(n, t))
        return "started"

    async def stop_one(self, name: str) -> str:
        """Stop one channel and drop its adapter. ``stopped`` or ``absent``.

        The outlet is retired with it, through ``on_stopped``. Leaving it
        registered looked safe -- a stopped channel receives nothing, so there
        is no turn left to reply to -- but the hub's worker is resident and
        holds the adapter it started with, so the next start of this channel
        would have received on the new adapter and replied through this one.
        """
        async with self._lock(name):
            return await self._stop_one(name)

    async def _stop_one(self, name: str) -> str:
        """``stop_one`` without the lock (see :meth:`_start_one`)."""
        channel = self.channels.pop(name, None)
        if channel is None:
            return "absent"
        # Taken with the adapter it belongs to, not after the await: the task
        # cancelled here has to be this adapter's, never whatever the name
        # holds by the time the stop finishes.
        task = self._tasks.get(name)
        try:
            await channel.stop()
            logger.info("Stopped {} channel", name)
        except Exception as e:
            logger.error("Error stopping {}: {}", name, e)
        if task is not None:
            self._forget_task(name, task)
            if not task.done():
                task.cancel()
        if self.on_stopped is not None:
            try:
                await self.on_stopped(name)
            except Exception as e:
                logger.error("Failed to retire outlet for channel {}: {}", name, e)
        return "stopped"

    async def restart_one(self, name: str) -> str:
        """Stop one channel and start it again, under a single lock.

        For a config change a live adapter cannot pick up: it holds the slice
        it was built with and re-reads nothing, so a corrected credential only
        reaches it through a rebuild. Answers ``start_one``'s word -- the stop
        half has nothing the caller draws, an absent adapter being a plain
        start.
        """
        async with self._lock(name):
            await self._stop_one(name)
            return await self._start_one(name)

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
