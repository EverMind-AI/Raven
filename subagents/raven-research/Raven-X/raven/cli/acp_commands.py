"""``raven acp``: the ACP agent's process shell.

A client (the main raven's subagent backend, or an editor) spawns this command
and speaks newline-delimited JSON-RPC to its stdin and stdout. What lives here
is only the process's own business -- claiming fd 1 for the protocol, sending
the logs somewhere else, opening stdin as a stream, and making a crash visible
-- because that part has to be right before any method can work: the only
bytes on stdout must be frames.

The protocol itself is :mod:`raven.acp.server`, which this hands the channel
and the session-engine registry to.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import sys
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from loguru import logger

from raven.acp.loops import AcpLoops
from raven.acp.server import install_crash_handlers, serve
from raven.acp.stdio import MAX_FRAME_BYTES, claim_stdout
from raven.agent.tools.web import (
    selected_fetch_fallback,
    selected_fetch_key,
    selected_fetch_provider,
    selected_search_key,
    selected_search_provider,
    web_provider_keys,
)
from raven.cli._log_file import redirect_loguru_to_file

_THREAD_CHUNK = 64 * 1024

_SAFE_LEAF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
"""A session id fit to be one directory name: no separators, no leading dot."""

_SESSION_FILES_DIR = "acp_workspaces"
"""Parent of the per-session file roots, under the shared workspace."""

acp_app = typer.Typer(name="acp", help="Serve Raven-X as an ACP agent over stdio.")


@acp_app.callback(invoke_without_command=True)
def acp(
    ctx: typer.Context,
    config: str | None = typer.Option(
        None, "--config", help="Path to a config file (defaults to ~/.raven/config.json)."
    ),
) -> None:
    """Serve the Agent Client Protocol on stdin/stdout."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        asyncio.run(_serve(config))
    except Exception as exc:
        # Kept away from Typer's own handler, which renders a rich traceback
        # with show_locals on: stderr is the stream an ACP client displays, and
        # those frames hold config objects and request payloads. The full
        # traceback is in the log file, whose sink does not annotate values.
        logger.exception("acp: exiting on an unhandled failure")
        typer.echo(f"raven acp failed: {exc}", err=True)
        raise typer.Exit(code=1) from None


async def _serve(config: str | None = None) -> None:
    """Own the stdio channel, then serve the protocol until the client closes it.

    loguru goes to a file, but fd 2 is deliberately left alive at WARNING: an
    ACP client surfaces its agent's stderr (the consuming raven journals it and
    attaches its tail to an empty-turn failure), and taking that away would
    make a crash invisible from the side that can report it. What must not
    happen is a write reaching fd 1, and ``claim_stdout`` is what prevents that
    -- a stray ``print`` lands on stderr, noise in a log rather than a frame
    the client cannot decode.
    """
    log_path = redirect_loguru_to_file("acp.log", retention=3, terminal_level="WARNING")
    install_crash_handlers()
    with claim_stdout() as out:
        logger.info("acp: serving on stdio, logs at {}", log_path)
        shared = build_shared(config)
        await _start_backend(shared.backend)
        loops = AcpLoops(
            lambda conversation: build_loop(shared, conversation),
            session_manager=shared.session_manager,
            max_loops=shared.acp.max_loops,
        )
        try:
            async with _open_stdin() as reader:
                await serve(reader, out, loops=loops, user_pool=shared.acp.user_pool)
            logger.info("acp: exiting")
        finally:
            await _stop_backend(shared.backend)


async def _start_backend(backend) -> None:
    """Start the memory backend before the first frame, like every surface does.

    ``maybe_build_memory_backend`` hands back an unstarted backend, and start()
    is the only seam for the /health probe, the API-version gate, the recall
    warm-up and ``require_service`` enforcement -- skipping it serves turns
    whose first recall pays the cold start inside its request budget. A refused
    start (``require_service``) fails the process: the operator asked to stop
    rather than write into nothing, and the consuming raven reports the dead
    agent. Everything else degrades, as on the TUI and gateway surfaces.
    """
    from raven.memory_engine.backend import MemoryServiceUnavailableError

    if backend is None:
        return
    try:
        await backend.start()
    except MemoryServiceUnavailableError:
        try:
            await backend.stop()
        except Exception:
            logger.exception("acp: memory backend stop failed after a refused start")
        raise
    except Exception:
        logger.exception("acp: memory backend start failed; continuing with degraded memory")


async def _stop_backend(backend) -> None:
    """Flush the backend's teardown aggregates on the clean-exit path.

    Under the consuming raven the server usually dies by SIGKILL and never gets
    here; this is for the stdin-close exit, where stop() surfaces the session's
    fail-open store/recall losses in the log.
    """
    if backend is None:
        return
    try:
        await backend.stop()
    except Exception:
        logger.exception("acp: memory backend stop failed; continuing shutdown")


@dataclass
class AcpShared:
    """What every session's engine on this connection is built from.

    Split out of the per-session half because each of these is expensive or
    single-writer: the provider owns one connection pool, the session manager is
    the cache ``session/load`` and the turn after it must agree on, the memory
    backend's start() pays a /health probe and a recall warm-up, TokenWise
    telemetry is one JSONL a second writer would interleave, and
    ``store_inflight`` is the process's cap on concurrent memory writes -- one
    set per engine would multiply it by the number of live sessions.

    ``pending_recovery`` is here for the opposite reason: not because it is
    expensive, but because it must outlive any one engine. Its reader is the
    session's next turn, and the engine cap can evict between the two.
    """

    config: Any
    ec_config: Any
    acp: Any
    provider: Any
    session_manager: Any
    strategies: Any
    plugin_registry: Any
    backend: Any
    plugin_tools: Any
    store_inflight: set[asyncio.Task]
    pending_recovery: dict[str, dict]


def build_shared(config_path: str | None = None) -> AcpShared:
    """Build the process-wide half of the engine stack, once.

    Mirrors ``tui_commands._build_tui_agent_loop`` minus the pieces that have
    no ACP consumer: no CronService (this deployment schedules nothing -- the
    consuming raven owns proactivity), no TUI colour plumbing. DR mode is not
    forced here: it comes from the config this process was launched with
    (raven-research's config.json pins ``drFlow`` on), one process one
    configuration -- plan §3.
    """
    from raven.cli._helpers import load_runtime_config, make_provider
    from raven.cli._plugin_stack import (
        build_plugin_registry,
        build_plugin_tools,
        maybe_build_memory_backend,
    )
    from raven.cli._token_wise_stack import install_from_config
    from raven.config.raven import load_raven_config
    from raven.session.manager import SessionManager

    config = load_runtime_config(config_path, None)
    ec_config = load_raven_config()

    provider = make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    # Same TokenWise stack as agent_commands: nothing else places rolling
    # cache_control breakpoints on the growing tail, and a research turn's
    # context passes the size where the provider's fixed prefix stops helping.
    strategies = install_from_config(
        ec_config.token_wise,
        telemetry_dir=config.workspace_path / ".token_wise",
        supports_caching=provider.supports_prompt_caching,
    )
    if strategies.get("cache_optimizer") is not None and hasattr(provider, "disable_auto_cache_control"):
        provider.disable_auto_cache_control = True

    plugin_registry = build_plugin_registry(ec_config)
    backend = maybe_build_memory_backend(
        config.workspace_path,
        ec_config,
        registry=plugin_registry,
    )
    plugin_tools = build_plugin_tools(
        config.workspace_path,
        ec_config,
        registry=plugin_registry,
    )
    return AcpShared(
        config=config,
        ec_config=ec_config,
        acp=config.acp,
        provider=provider,
        session_manager=session_manager,
        strategies=strategies,
        plugin_registry=plugin_registry,
        backend=backend,
        plugin_tools=plugin_tools,
        store_inflight=set(),
        pending_recovery={},
    )


def build_loop(shared: AcpShared, conversation: str | None = None):
    """Build one session's engine on top of ``shared``.

    ``conversation`` roots the session's file tools in a subtree of their own,
    so two concurrent turns writing ``report.md`` do not overwrite each other.
    Everything that follows the write root follows it: the file, exec and media
    tools, and the per-turn Checkpoint, whose shadow repo would otherwise be one
    repository every session's ``git add -A`` races. What does NOT move is
    ``workspace``, the shared root the system prompt is assembled from
    (MEMORY.md, the profile files, TOOLS.md) and the skill catalogue is read
    from -- giving a session its own workspace would empty both, which is a
    distribution change (AGENTS.md 0.2) and not the isolation being asked for.
    """
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.recovery import limits_from_defaults

    config = shared.config
    ec_config = shared.ec_config
    agent_loop = AgentLoop(
        provider=shared.provider,
        strategies=shared.strategies,
        workspace=config.workspace_path,
        file_workspace=_session_file_root(config.workspace_path, conversation),
        store_inflight=shared.store_inflight,
        pending_recovery=shared.pending_recovery,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        empty_recovery=limits_from_defaults(config.agents.defaults),
        context_window_tokens=config.agents.defaults.context_window_tokens,
        context_window_authoritative=config.agents.defaults.context_window_authoritative,
        max_concurrent_subagents=config.agents.defaults.max_concurrent_subagents,
        max_subagent_spawns_per_hour=config.agents.defaults.max_subagent_spawns_per_hour,
        brave_api_key=selected_search_key(config.tools.web)[1] or None,
        web_search_provider=selected_search_provider(config.tools.web.search),
        jina_api_key=selected_fetch_key(config.tools.web)[1] or None,
        web_fetch_provider=selected_fetch_provider(config.tools.web.fetch),
        web_fetch_fallback=selected_fetch_fallback(config.tools.web.fetch),
        web_provider_keys=web_provider_keys(config.tools.web),
        web_proxy=config.tools.web.proxy or None,
        web_corpus_endpoint=config.tools.web.corpus_endpoint or None,
        web_benchmark_containment=config.tools.web.benchmark_containment,
        media_config=config.effective_media_config(),
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=shared.session_manager,
        mcp_servers=config.tools.mcp_servers,
        disabled_tools=config.tools.disabled_tools,
        tool_search_config=config.tools.tool_search,
        sandbox_config=config.tools.sandbox,
        channels_config=config.channels,
        skill_forge_config=ec_config.skill_forge,
        skill_forge_router_config=ec_config.skill_forge.router,
        context_config=ec_config.context,
        runtime_config=ec_config.runtime,
        # The whole point of this surface: raven-research's config pins drFlow
        # on, and a construction site that drops this kwarg silently serves
        # stock Raven (tests/test_cli_agent_loop_wiring.py holds the contract).
        dr_flow=ec_config.dr_flow,
        memory_config=ec_config.memory,
        backend=shared.backend,
        plugin_tools=shared.plugin_tools,
        # An ACP session is a multi-turn interactive conversation: the DR
        # clarification gate's question comes back answered on the same
        # session.
        interactive=True,
    )
    agent_loop.configure_personalization(
        config.agents.defaults.enable_personalization,
    )
    return agent_loop


@contextlib.asynccontextmanager
async def _open_stdin() -> AsyncIterator[asyncio.StreamReader]:
    """A reader over fd 0, released on the way out.

    The limit bounds the reader's own buffer (backpressure); framing and the
    frame cap are :func:`raven.acp.stdio.read_frames`'s own, precisely so an
    oversized frame can be answered instead of raising out of the transport.

    Two paths, because ``connect_read_pipe`` refuses a regular file outright
    (``raven acp < script.jsonl``, which is how anyone first tries this by
    hand, would die before reading a byte). The fallback reads the descriptor
    on a daemon thread and feeds the same reader -- it costs a thread and gives
    up the transport's own backpressure, acceptable for a file of bounded
    size. A spawning client gets a pipe and never takes that branch.

    On the pipe path the transport is closed rather than left to the garbage
    collector: a dropped read transport is collected with the loop still
    holding its descriptor, and the unregister that follows fails with no
    caller to report to.
    """
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (ValueError, OSError) as exc:
        logger.info("acp: stdin is not a pipe ({}); reading it on a thread", exc)
        _spawn_stdin_feeder(reader)
        yield reader
        return
    try:
        yield reader
    finally:
        transport.close()


def _spawn_stdin_feeder(reader: asyncio.StreamReader) -> threading.Thread:
    """Pump fd 0 into ``reader`` from a daemon thread.

    A daemon thread and not ``run_in_executor``: the blocking read cannot be
    cancelled, and asyncio waits for the default executor when it closes the
    loop -- a process that will not exit. A daemon thread has no such hold.

    The reader is fed through ``call_soon_threadsafe`` because ``StreamReader``
    is not thread-safe.
    """
    loop = asyncio.get_running_loop()
    stream = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
    # ``read1`` and not ``read``: on a buffered stream ``read(n)`` blocks until
    # it has all n bytes or EOF, so a merely slow stream would deliver nothing
    # until 64 KiB accumulated -- one frame at a time is exactly this traffic.
    read = getattr(stream, "read1", None) or stream.read

    def _pump() -> None:
        try:
            while True:
                chunk = read(_THREAD_CHUNK)
                if not isinstance(chunk, bytes):
                    chunk = str(chunk).encode("utf-8") if chunk else b""
                if not chunk:
                    loop.call_soon_threadsafe(reader.feed_eof)
                    return
                loop.call_soon_threadsafe(reader.feed_data, chunk)
        except Exception as exc:
            # A read error is EOF as far as the protocol is concerned; reported
            # on the way past because a truncated session and a finished one
            # look identical from the loop.
            logger.warning("acp: reading stdin failed: {}", exc)
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(reader.feed_eof)

    thread = threading.Thread(target=_pump, name="acp-stdin", daemon=True)
    thread.start()
    return thread


def _session_file_root(workspace: Path, conversation: str | None) -> Path | None:
    """Where one session's file tools may write.

    ``None`` for a caller that named no session, which leaves the engine rooted
    in the shared workspace exactly as before. The channel prefix is stripped
    from the id because it is the same for every session here and a ``:`` in a
    path name is a needless portability trap.

    The id is not trusted as a path component: ``session/load`` and
    ``session/resume`` take it from the wire, and ``..`` in it would put the
    session's file root outside the workspace. Anything but a plain name is
    replaced by a digest of the id, which is still one stable directory per
    session.

    Deliberately NOT under ``workspace/sessions``, which is ``SessionManager``'s
    (``sessions/<channel>/<chat_id>.jsonl``): that store is listed with
    ``glob("*/*.jsonl")``, so a report the model happened to write as ``.jsonl``
    would come back as a session on a channel named after this directory.

    Nothing reclaims these. Deliberate: the directory holds what the model
    wrote, so deleting it on eviction would destroy the turn's output, and
    retention is the operator's call. The written bytes are the same bytes the
    one shared workspace already accumulated with no reclaim path -- this
    partitions that growth rather than adding to it. What is genuinely new is
    one Checkpoint shadow repo per session under ``.raven/`` instead of one for
    the connection: the same snapshot objects, split, plus that repo's own
    metadata per session. Both grow with the number of sessions served; the
    deferral above covers both, and ``runtime.checkpoint.policy = "never"``
    turns the second off.
    """
    if not conversation:
        return None
    leaf = conversation.split(":", 1)[-1]
    if not _SAFE_LEAF.match(leaf):
        leaf = hashlib.sha256(conversation.encode("utf-8")).hexdigest()[:32]
    root = workspace / _SESSION_FILES_DIR / leaf
    root.mkdir(parents=True, exist_ok=True)
    return root


__all__ = ["acp_app"]
