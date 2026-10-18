"""``raven acp``: the ACP agent's process shell.

An editor spawns this command and speaks newline-delimited JSON-RPC to its stdin
and stdout. What lives here is only the process's own business -- claiming fd 1
for the protocol, sending the logs somewhere else, opening stdin as a stream, and
making a crash visible -- because that part has to be right before any method can
work: the only bytes on stdout must be frames.

The protocol itself is :mod:`raven.acp.server`, which this hands the channel to.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from loguru import logger

from raven.acp.server import install_crash_handlers, serve
from raven.acp.stdio import claim_stdout
from raven.acp.stdio import open_stdin as _open_stdin
from raven.cli._log_file import redirect_loguru_to_file
from raven.config.loader import set_config_path
from raven.utils import asyncio_runner as bounded_asyncio
from raven.utils.heap import start_trimmer

# Written out here rather than left to the docstring because this command has no
# interactive surface to discover it from: a client spawns it, and everything a
# person needs in order to point it at the right instance, find its log, or drive
# it by hand has nowhere else to appear.
_HELP = """Serve Raven as an ACP agent over stdio.

A client -- an editor, or another agent -- spawns this process and
speaks the Agent Client Protocol to its stdin and stdout:
newline-delimited JSON-RPC, one frame per line, protocolVersion 1. It
is not an interactive command: run bare in a terminal it waits for a
frame that never arrives. To drive it by hand, feed it a file with
`raven acp < session.jsonl`.
\b
Answered:
  session/new, session/load, session/resume, session/list,
  session/close, session/delete, session/prompt, session/cancel,
  session/set_config_option.
\b
Not answered:
  session/set_mode and logout are not implemented. A non-empty
  mcpServers on session/new or session/load is refused rather than
  ignored: MCP is connected once per process, and nothing scopes a
  server to one session.

A session takes its working directory from the cwd the client sends, so
there is no flag for it: session/new, session/load and session/resume
each require one and answer -32602 without it. Models switch through
session/set_config_option with category "model", which binds the
calling session alone and takes effect on its next turn --
session/set_model is not in the stable schema. playbook is announced
to the client as an available command. A frame over 8 MiB is answered
with an error and skipped, leaving the stream in sync.

Because stdout carries the protocol, logs go to a file:
<config dir>/logs/acp.log, rotating at 10 MB, 3 kept. stderr is left
to the client and carries WARNING and above; an unhandled failure
prints one line there and exits 1, with the traceback in the log.
\b
Environment:
  RAVEN_ACP_LOG_LEVEL  Level for the log file (default INFO).
                       --verbose outranks it. DEBUG makes LiteLLM
                       write each whole request as one record,
                       measured at 146 KiB on a single line.
  RAVEN_CLI_DEBUG      Also mirror DEBUG and above to stderr. With a
                       DEBUG file level, a client whose stderr reader
                       gives up on a line that long can deadlock this
                       process.
  RAVEN_HOME           Move the whole instance: config, logs, runtime
                       state. --config moves it for one run.
  RAVEN_SUBAGENT       1/true/yes/on says another raven launched this
                       process to answer one task for it, which
                       withholds the tools that hand work to a further
                       agent (spawn, run_subagent_dag, the graph
                       controls, the playbook pair) and says so in the
                       prompt. Set by the host on every child it
                       launches; set it yourself only to reproduce
                       what a sub-agent sees.

Design notes are in docs/specs, in the files named *-acp-*.
"""

# No subcommands are registered, so Click's default ``COMMAND [ARGS]...`` in the
# usage line promises a shape that does not exist. The group itself stays -- see
# the callback's guard, which keeps a future ``raven acp <something>`` from also
# starting a server.
acp_app = typer.Typer(name="acp", help=_HELP, subcommand_metavar="")


@acp_app.callback(invoke_without_command=True)
def acp(
    ctx: typer.Context,
    config: str | None = typer.Option(
        None,
        "--config",
        help="Config file to serve from (default: the one $RAVEN_HOME names).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Log at DEBUG to the file. Leaves stderr at WARNING; see RAVEN_CLI_DEBUG.",
    ),
) -> None:
    """Serve the Agent Client Protocol on stdin/stdout.

    ``--config`` is applied before anything derives a path, because ``get_logs_dir``
    hangs off the config's own directory: an instance pointed at another config
    writes its log beside that config rather than into the default home. Without
    it, a host running several of these -- each meant to be a different agent, with
    its own model, provider and identity -- has no way to say which one this
    process is, and every one of them comes up on the host's config.
    """
    if ctx.invoked_subcommand is not None:
        return
    if config:
        _use_config(config)
    try:
        served = bounded_asyncio.run(_serve(verbose=verbose))
    except Exception as exc:
        # Kept away from Typer's own handler, which renders a rich traceback with
        # ``show_locals`` on -- measured at 228 lines of stderr, with the value of
        # every local in every frame. stderr is the stream an ACP client displays,
        # and those frames hold config objects and request payloads. The full
        # traceback is in the log file, where the sink is configured not to
        # annotate it with values.
        logger.exception("acp: exiting on an unhandled failure")
        typer.echo(f"raven acp failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    # ACP owns a short-lived process whose native rendering and memory runtimes
    # can crash during interpreter finalization after async teardown completes.
    # Only after a real stdio session: a test that stubs ``_serve`` gets None
    # back and keeps its interpreter.
    if served:
        os._exit(0)


def _use_config(path: str) -> None:
    """Point this process at ``path`` before any component reads a path from it.

    Exit 2 and not 1: a config that is not there is a mistake in the spawn
    command, and a client that can tell that apart from the agent having crashed
    can say so instead of retrying.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        typer.echo(f"raven acp: no config file at {resolved}", err=True)
        raise typer.Exit(code=2)
    set_config_path(resolved)


def _file_log_level(verbose: bool = False) -> str:
    """The level the acp log file accepts. INFO unless asked for more.

    DEBUG is expensive here in a way a level usually is not. LiteLLM's
    ``print_args_passed_to_litellm`` writes the whole request -- system prompt,
    history, every tool schema -- as one DEBUG record per call, measured at
    146 KiB on a single line. Those lines also reach the client over fd 2, and a
    client whose reader gives up on one deadlocks this process at its next
    write, so INFO keeps the volume off that path by default.

    ``RAVEN_ACP_LOG_LEVEL`` is the way back to a full trace, for a session where
    the payloads are the thing being debugged, and ``--verbose`` asks the same
    thing on the command line. The flag outranks the variable: it is set per spawn
    by whoever is debugging this run, the variable by whatever inherited the
    environment. The gateway's entry point has always taken this level from
    config; only this one had it hardcoded.
    """
    if verbose:
        return "DEBUG"
    return os.environ.get("RAVEN_ACP_LOG_LEVEL", "").strip().upper() or "INFO"


async def _serve(*, verbose: bool = False) -> bool:
    """Own the stdio channel, then serve the protocol until the client closes it.

    loguru goes to a file, but fd 2 is deliberately left alone: an ACP client
    surfaces its agent's stderr, and taking that away would make a crash
    invisible from the side that can actually report it. What must not happen is
    a write reaching fd 1, and ``claim_stdout`` is what prevents that -- a stray
    ``print`` lands on stderr, where it is noise in a log rather than a frame the
    client cannot decode.
    """
    log_path = redirect_loguru_to_file(
        "acp.log", file_level=_file_log_level(verbose), retention=3, terminal_level="WARNING"
    )
    install_crash_handlers()
    # A pooled host outlives every deck it renders, so it trims like the gateway.
    start_trimmer()
    with claim_stdout() as out:
        logger.info("acp: serving on stdio, logs at {}", log_path)
        async with _open_stdin() as reader:
            await serve(reader, out)
        logger.info("acp: exiting")
    return True


__all__ = ["acp_app"]
