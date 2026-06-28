"""Shared base class for channel adapters.

``ChannelBase`` is thin construction plumbing only; the contract the rest of the
system depends on is the ``Channel`` protocol in
:mod:`raven.channels.contract`.
"""

from __future__ import annotations

from typing import Any

from raven.channels.contract import Capabilities
from raven.channels.intake import Intake


class ChannelBase:
    """Thin construction base for channel adapters — shared plumbing only.

    Carries no inbound/transcribe behaviour (that lives in ``Intake`` and the
    ``transcribe`` helper); it only removes the ``__init__`` boilerplate every
    adapter repeats. The contract the rest of the system depends on is the
    ``Channel`` protocol in :mod:`raven.channels.contract`, not this class —
    inheriting here is for code reuse; conformance is by protocol. A subclass
    supplies ``name``/``display_name`` and ``start``/``stop``/``send``, and may
    override ``is_allowed`` for bespoke matching (Telegram's ``<id>|<username>``
    form); the override is picked up automatically by the injected Intake.
    """

    name: str = ""
    display_name: str = ""
    capabilities: Capabilities = Capabilities()
    transcription_api_key: str = ""   # set by ChannelManager

    def __init__(self, config: Any):
        self.config = config
        self._running = False
        self.intake = Intake(self.name, config, allow_check=self.is_allowed)

    @property
    def is_running(self) -> bool:
        return self._running

    def is_allowed(self, sender_id: str) -> bool:
        """Deny-by-default allowlist check (empty = deny all; ``"*"`` = allow
        all). Override for bespoke matching; the override flows into Intake."""
        from raven.auth.allowlist import is_allowed as _check

        return _check(self.name, sender_id, getattr(self.config, "allow_from", None))
