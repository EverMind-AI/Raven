"""rpc package -- the JSON-RPC protocol between Runtime and any interactive client.

Single source of truth for the contract lives in ``rpc-schema/openrpc.json``.
The Pydantic v2 models in :mod:`raven.rpc.models` are hand-written counterparts
kept in sync via ``tests/test_rpc_schema_match.py``.
"""

LOCAL_CHANNEL = "tui"
"""The channel the terminal and the served page both run on.

They share one session pool on purpose: the same person switching between a
terminal and a window expects the same conversations, so both mint and read
``tui:<chat_id>`` keys and both list the same sessions.

The value is the terminal's name for historical reasons and stays that way.
Renaming it is not a rename -- it is the key prefix on every stored session, the
channel recorded on every scheduled job, and the config block clients already
write. A better name is worth an explicit migration, not a silent one.

And this constant is not that migration's single switch, however much it looks
like one: ``session.create`` still mints ``f"tui:{new_chat_id()}"``,
``session.list`` still filters ``["tui"]``, and ``turn``/``spine`` still default
to the literal. Changing the value here without those would advertise one
channel at handshake and mint keys on another -- a default session pointing at a
pool the picker does not show, which is the shape of the bug this constant was
introduced to fix. It covers the three sites that read it and no more.

The web app (``raven/web_rpc``) is a different channel and passes ``"web"``.
"""

__all__ = ["LOCAL_CHANNEL"]
