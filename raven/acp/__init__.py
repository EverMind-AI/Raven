"""Raven as an ACP agent: the stdio channel, and the protocol served over it.

An editor spawns ``raven acp`` as a subprocess and speaks newline-delimited
JSON-RPC to its stdin and stdout. That makes fd 1 a wire rather than a console,
which is the constraint this package is organised around: :mod:`raven.acp.stdio`
makes the channel safe to speak on, and everything else speaks.

The opposite direction -- raven spawning somebody else's ACP agent -- already
exists under :mod:`raven.agent.acp` and is not this. The two share only the wire
layer in :mod:`raven.agent.acp.protocol`, which is imported rather than copied.
"""

from raven.acp.stdio import MAX_FRAME_BYTES, claim_stdout, read_frames, write_frame

__all__ = ["MAX_FRAME_BYTES", "claim_stdout", "read_frames", "write_frame"]
