"""A real MCP server for the bridge e2e test, with the five behaviours it needs.

Not named ``test_*`` on purpose: pytest must spawn this as a subprocess, not
collect it. The tools stand in for what a bridged dispatch actually depends on --
a server-initiated request, progress notifications, a result too big for a
default line limit, an upstream that dies while a request is in flight, one
that goes unreachable while staying alive, and one that leaves evidence on disk
that a caller reached this process.
"""

import os
import signal
import sys
import threading
import time

from mcp.server.fastmcp import Context, FastMCP

# How long ``go_silent`` stays alive before killing itself. Long enough to
# outlast the client SDK's stdio teardown (a 2s wait on the process plus a 2s
# SIGTERM->SIGKILL escalation), which is the window the test measures. The caller
# kills this process by pid as soon as it is done, so the timer only matters when
# the caller never gets there -- without it a SIGTERM-deaf process would outlive
# the run, and neither the SDK's escalation nor closing stdin reaches it.
_LINGER_SECONDS = 10.0

mcp = FastMCP("bridge-e2e-upstream")

# argv[1], when given, is a path to drop this pid into. The caller of
# ``go_silent`` needs it: that tool leaves a process no signal from the client
# SDK reaches, and finding it by name would hit another xdist worker's upstream
# instead of this one.
if len(sys.argv) > 1:
    with open(sys.argv[1], "w") as _pid_file:
        _pid_file.write(str(os.getpid()))

# argv[2] and argv[3] arm ``leave_marker``: where it records the note it was
# called with, and the receipt it hands back. Both stay on the host side of the
# bridge -- a sub-agent is told the note to pass and never learns either of
# these -- so a marker file holding that note proves the call arrived here, and
# the receipt turning up downstream proves the result went back.
_MARKER_FILE = sys.argv[2] if len(sys.argv) > 2 else None
_MARKER_RECEIPT = sys.argv[3] if len(sys.argv) > 3 else "no-receipt-configured"


@mcp.tool()
async def ask_client(ctx: Context) -> str:
    """Server-initiated request: makes the client answer a sampling call."""
    from mcp.types import SamplingMessage, TextContent

    reply = await ctx.session.create_message(
        messages=[SamplingMessage(role="user", content=TextContent(type="text", text="ping"))],
        max_tokens=16,
    )
    return f"client answered: {reply.content.text}"


@mcp.tool()
async def stepped(ctx: Context, steps: int = 3) -> str:
    """Report progress once per step, then return."""
    for i in range(steps):
        await ctx.report_progress(progress=i + 1, total=steps, message=f"step {i + 1}")
    return f"done {steps} steps"


@mcp.tool()
def big(kb: int) -> str:
    """Return a payload of roughly kb kilobytes, standing in for an image blob."""
    return "x" * (kb * 1024)


@mcp.tool()
def die() -> str:
    """Exit while this request is in flight."""
    os._exit(9)


@mcp.tool()
def go_silent() -> str:
    """Become unreachable without exiting: EOF on stdout, but the process lives.

    This is the shape that makes the placement of the endpoint's downstream
    ``writer.close()`` observable. ``die`` cannot: it leaves a process that is
    already dead, so the SDK's ``await process.wait()`` returns at once and a
    close before or after transport teardown look the same from downstream.
    Refusing SIGTERM forces that teardown to burn its full timeout instead.

    The linger is a non-daemon thread so the process stays alive by
    construction rather than by whatever the crashed writer task leaves behind,
    and it ends in ``os._exit`` so nothing here can outlive the test.
    """
    threading.Thread(target=lambda: (time.sleep(_LINGER_SECONDS), os._exit(0)), daemon=False, name="linger").start()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    os.close(1)
    time.sleep(_LINGER_SECONDS)
    return "never reaches the client"


@mcp.tool()
def leave_marker(note: str) -> str:
    """Record that the caller reached this server, and return the receipt for it.

    Args:
        note: the exact note the caller was told to pass through.
    """
    if _MARKER_FILE:
        with open(_MARKER_FILE, "w") as marker:
            marker.write(note)
    return f"marker recorded, receipt {_MARKER_RECEIPT}"


mcp.run("stdio")
