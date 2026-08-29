"""Authentication & authorization primitives.

``allowlist`` holds the canonical ``is_allowed(channel, sender_id, allow_list)``
check; ``channels/base.py`` delegates to it instead of inlining the
deny-by-default rule.
"""

from raven.auth.allowlist import is_allowed

__all__ = ["is_allowed"]
