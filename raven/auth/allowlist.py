"""Channel sender allowlist — canonical home for the deny-by-default check.

Used by ``raven.channels.base.ChannelBase.is_allowed`` (and the per-channel
overrides / ``Intake``). The semantics are deliberately conservative:

- Empty allowlist → DENY everything (so a misconfigured channel
  doesn't accidentally accept the whole internet).
- ``"*"`` in the list → ALLOW everything (explicit opt-in).
- Otherwise → exact-match against the stringified sender id.

A single warning is logged the first time a given channel rejects
because its allowlist is empty — repeating it every inbound spams logs.
"""

from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


# Channel names that have already logged the "empty allowlist" warning
# in this process. Module-level to share across all instances of a
# given channel adapter and across repeated invocations.
_warned_empty: set[str] = set()


def is_allowed(
    channel_name: str,
    sender_id: str | int | None,
    allow_list: Iterable[str] | None,
) -> bool:
    """Return whether ``sender_id`` is permitted on ``channel_name``.

    Args:
        channel_name: Human-readable channel identifier (used only for
            logging).
        sender_id: The platform-specific sender id. Stringified before
            comparison so int / open_id / phone-number flavors all
            compare cleanly.
        allow_list: Iterable of permitted sender ids (or ``"*"`` for
            allow-all). ``None`` or an empty iterable means deny.

    Returns:
        True if the sender is permitted, False otherwise.
    """
    if allow_list is None:
        _warn_empty_once(channel_name)
        return False

    # Materialize once so we can both check membership and detect emptiness
    # without forcing the caller to provide a sequence.
    allow_set = set(map(str, allow_list))
    if not allow_set:
        _warn_empty_once(channel_name)
        return False
    if "*" in allow_set:
        return True
    return str(sender_id) in allow_set


def _warn_empty_once(channel_name: str) -> None:
    if channel_name in _warned_empty:
        return
    _warned_empty.add(channel_name)
    logger.warning(
        "%s: allow_from is empty — all access denied",
        channel_name,
    )


def reset_warning_state() -> None:
    """Test-only helper — clear the "already warned" tracker so a fixture
    can assert the warning fires on a fresh channel."""
    _warned_empty.clear()


__all__ = ["is_allowed", "reset_warning_state"]
