"""Serve this agent over ACP (Agent Client Protocol).

The transport a client drives this deck-builder through, replacing a launcher
that forked the CLI and rebuilt the result from its stdout. The design record,
including the event mapping and every capability declared false, is
``docs/acp-server-design.md``.

Layering, outermost first:

* ``server`` -- one connection: read frames, answer each in its own task, tear the
  sessions down on EOF.
* ``stdio`` -- making stdout safe to speak a protocol on, and framing bytes.
* ``methods`` -- the inbound method surface.
* ``session`` -- one session's directories, engine and in-flight prompt.
* ``spine`` -- ``AcpOutlet``: the spine event vocabulary rendered as
  ``session/update`` frames. The twin of ``raven/tui_rpc/spine.py``'s
  ``TuiOutlet``.
* ``materials`` -- source documents in, verified deck out.
* ``capabilities`` / ``tool_kinds`` / ``protocol`` -- what is declared, how a tool
  call is drawn, and the wire.
"""

from raven.acp.protocol import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION"]
