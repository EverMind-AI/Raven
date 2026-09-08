"""Permission gating over tool dispatch.

The gate decides about every tool call at the registry's door -- builtin
rulings the platform ships, the user's tiers and exec patterns, and the mode's
reading of the ask tier (ask everything, smart-review, or full access). The
vocabulary it answers with lives in ``raven.contracts.permissions``; the
entrance binds the turn's asking capability through ``start_permission_turn``.
"""

from raven.permissions.builtin import BuiltinRulings
from raven.permissions.gate import PermissionGate
from raven.permissions.session import session_mode, set_session_mode, set_session_mode_restorer
from raven.permissions.turn import start_permission_turn

__all__ = [
    "BuiltinRulings",
    "PermissionGate",
    "session_mode",
    "set_session_mode",
    "set_session_mode_restorer",
    "start_permission_turn",
]
