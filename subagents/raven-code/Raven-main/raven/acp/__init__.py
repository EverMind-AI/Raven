"""Raven as an ACP agent: the stdio channel, and the protocol served over it.

An editor spawns ``raven acp`` as a subprocess and speaks newline-delimited
JSON-RPC to its stdin and stdout. That makes fd 1 a wire rather than a console,
which is the constraint this package is organised around: :mod:`raven.acp.stdio`
makes the channel safe to speak on, and everything else speaks.

The layers, outermost first:

* :mod:`raven.acp.stdio` -- descriptors and framing. Knows nothing about ACP.
* :mod:`raven.acp.protocol` -- the wire helpers the agent direction needs, on top
  of the framing already proven in :mod:`raven.agent.acp.protocol`.
* :mod:`raven.acp.capabilities` -- what this agent declares, and what the client
  declared back.
* :mod:`raven.acp.updates` -- the outbound translator, which is also where a
  session's turn state lives, because the turn's stop reason arrives on the event
  stream rather than as a return value.
* :mod:`raven.acp.tool_kinds` -- classifying a tool call for a client that draws it.
* :mod:`raven.acp.outbound` -- requests this agent makes of the client, and the
  answers coming back. Nothing in raven had this: the dispatcher is inbound-only
  and ``send_frame`` is fire-and-forget.
* :mod:`raven.acp.permissions` -- the shell-approval round trip over
  ``session/request_permission``. A second transport for raven's existing
  approval decision, not a second decision.
* :mod:`raven.acp.redact` -- credentials out of anything on its way to the
  client, because an ACP payload is rendered in an editor and often kept there.
* :mod:`raven.acp.methods` -- the inbound methods, each mapped onto an RPC call
  that already exists.
* :mod:`raven.acp.server` -- one connection: its engine, its frame loop, its
  teardown.

The opposite direction -- raven spawning somebody else's ACP agent -- already
exists under :mod:`raven.agent.acp` and is not this. The two share only the wire
layer in :mod:`raven.agent.acp.protocol`, which is imported rather than copied.
"""

from raven.acp.stdio import MAX_FRAME_BYTES, claim_stdout, read_frames, write_frame

__all__ = ["MAX_FRAME_BYTES", "claim_stdout", "read_frames", "write_frame"]
