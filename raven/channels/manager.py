"""Channel manager for coordinating chat channels.

Construction + lifecycle only. Outbound delivery is the spine's
DeliveryHub/Outlet (a ChannelOutletAdapter per channel registered by the
gateway); inbound is each channel's Intake -> scheduler.submit.
"""

from __future__ import annotations

import asyncio
import json
import sys
from importlib.metadata import PackageNotFoundError, distribution
from typing import Any

from loguru import logger

from raven.channels.contract import Channel
from raven.config.schema import Config


def _missing_dep_hint(modname: str) -> str:
    """How to install a channel's missing SDK, tailored to the install mode.

    An editable (dev) checkout uses ``uv sync``; a wheel/tool install has no
    source tree, so it must re-run the installer instead. PEP 610
    ``direct_url.json`` distinguishes them -- a wheel install records
    ``archive_info`` (no ``dir_info`` key), so ``.get`` chaining avoids a
    KeyError when it is absent. This runs while a channel is already failing,
    so a missing/corrupt file must degrade to the installer hint, never raise.
    """
    editable = False
    try:
        raw = distribution("raven").read_text("direct_url.json")
        if raw:
            editable = bool(json.loads(raw).get("dir_info", {}).get("editable", False))
    except (PackageNotFoundError, ValueError):
        pass

    if editable:
        return f"Run: uv sync --extra channel-{modname}"
    if sys.platform == "win32":
        return "Re-run the installer to add channels: irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex"
    return "Re-run the installer to add channels: curl -fsSL https://raven.evermind.ai/install.sh | bash"


class ChannelManager:
    """Manages chat channels: construct enabled adapters, start/stop, status."""

    def __init__(self, config: Config):
        self.config = config
        self.channels: dict[str, Channel] = {}

        self._init_channels()

    def _init_channels(self) -> None:
        """Initialize enabled channels from their declarative ``ChannelSpec``.

        Each adapter's ``spec.factory`` defers the heavy SDK import, so a
        missing channel dependency surfaces here as an ImportError and disables
        just that channel.
        """
        from raven.channels.registry import discover_specs

        groq_key = self.config.providers.groq.api_key

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
                    _missing_dep_hint(modname),
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
