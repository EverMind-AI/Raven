"""RPC method handler subpackage.

Each module corresponds to a domain (system / setup / reload / config /
cli_dispatch / session / terminal / stubs / …) and exposes a
``register_<domain>_methods()`` helper that the server loop calls at startup.

The umbrella :func:`register_aligned_methods` registers every handler that
the ui-tui frontend currently calls:

* ``system.*`` (3) — handshake / ping / version
* ``cli.dispatch`` (1) — in-process EC CLI runner
* ``setup.status`` (1) — provider detect
* ``reload.mcp`` (1) — hermes 5s poll no-op
* ``config.get`` / ``config.set`` (2) — hot-changeable config
* ``session.{create, close, resume}`` (3) — Wave 6.5 lifecycle return-shape
  stubs (real SessionManager wiring deferred)
* ``terminal.resize`` (1) — Wave 6.5 SIGWINCH no-op + cols record
* hermes-only stub groups (27 names after Wave 6.5): 6 original groups
  (voice / browser / spawn_tree / process.stop / rollback / tools.configure)
  + 17 Wave 6.5 unaligned method names that return -32012

Domains that are NOT yet aligned (turn.* / skill.* / model.* / mcp.*) are
deliberately omitted from the umbrella — they wait for the frontend/backend
alignment audit before being wired in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from raven.tui_rpc.methods._stubs import register_stub_methods
from raven.tui_rpc.methods.cli_dispatch import register_cli_methods
from raven.tui_rpc.methods.commands import register_commands_methods
from raven.tui_rpc.methods.config import register_config_methods
from raven.tui_rpc.methods.confirm import register_confirm_methods
from raven.tui_rpc.methods.question import register_question_methods
from raven.tui_rpc.methods.reload import register_reload_methods
from raven.tui_rpc.methods.session import register_session_methods
from raven.tui_rpc.methods.setup import register_setup_methods
from raven.tui_rpc.methods.slash_routing import register_slash_routing_methods
from raven.tui_rpc.methods.system import register_system_methods
from raven.tui_rpc.methods.terminal import register_terminal_methods
from raven.tui_rpc.methods.turn import register_turn_methods

if TYPE_CHECKING:
    from raven.spine.scheduler import Scheduler
    from raven.tui_rpc.confirm_broker import ConfirmBroker
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.errors import RpcError
    from raven.tui_rpc.methods.session import AgentLoopFactory
    from raven.tui_rpc.question_broker import QuestionBroker
    from raven.tui_rpc.subscriptions import SubscriptionEmitter


def register_aligned_methods(
    dispatcher: "Dispatcher",
    *,
    emitter: "SubscriptionEmitter | None" = None,
    agent_loop_factory: "AgentLoopFactory | None" = None,
    confirm_broker: "ConfirmBroker | None" = None,
    question_broker: "QuestionBroker | None" = None,
    scheduler: "Scheduler | None" = None,
    turn_ids: "dict[str, str] | None" = None,
    build_error: "RpcError | None" = None,
) -> None:
    """Register every aligned RPC handler on a dispatcher.

    Used by both the v0.0.1 demo runner (``scripts/run_v001_demo.py``) and
    the production ``tui_commands.run_subprocess_with_rpc`` wrapper. Keeping
    a single registration point avoids drift between the two spawn paths.

    ``emitter`` and the build_tui bundle (``scheduler`` / ``turn_ids`` /
    ``build_error``) are forwarded to :func:`register_turn_methods` — when
    ``emitter`` is ``None`` the ``turn.*`` group is skipped (the demo runner
    path that does not own a streaming subscription channel still works without
    them); ``agent_loop_factory`` is forwarded to the session methods.
    ``confirm_broker`` is forwarded to :func:`register_confirm_methods`.
    """
    register_system_methods(dispatcher)
    register_aligned_methods_except_system(
        dispatcher,
        emitter=emitter,
        agent_loop_factory=agent_loop_factory,
        confirm_broker=confirm_broker,
        question_broker=question_broker,
        scheduler=scheduler,
        turn_ids=turn_ids,
        build_error=build_error,
    )


def register_aligned_methods_except_system(
    dispatcher: "Dispatcher",
    *,
    emitter: "SubscriptionEmitter | None" = None,
    agent_loop_factory: "AgentLoopFactory | None" = None,
    confirm_broker: "ConfirmBroker | None" = None,
    question_broker: "QuestionBroker | None" = None,
    scheduler: "Scheduler | None" = None,
    turn_ids: "dict[str, str] | None" = None,
    build_error: "RpcError | None" = None,
) -> None:
    """Register every aligned RPC handler EXCEPT system.* on a dispatcher.

    The production path (``tui_commands.run_subprocess_with_rpc``) wraps
    ``system.hello`` to latch a handshake event, so it must register
    ``system.{hello,ping,version}`` by hand before delegating the rest of
    the registration here. This helper exists so the production path stays
    in lock-step with the umbrella — any future ``register_*_methods``
    helper added to this module is picked up automatically by production,
    eliminating the registration-drift bug where new handlers worked in
    the demo runner but not in ``raven tui`` subprocess.
    """
    register_cli_methods(dispatcher, confirm_broker=confirm_broker)
    register_setup_methods(dispatcher)
    register_reload_methods(dispatcher)
    register_config_methods(dispatcher)
    register_session_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_terminal_methods(dispatcher)
    register_stub_methods(dispatcher)
    # harness-command-catalog-dynamic: real ``commands.catalog`` handler;
    # MUST come after ``register_stub_methods`` because the stub list dropped
    # its ``commands.catalog`` entry, and ``Dispatcher.register`` raises on
    # duplicate registration rather than last-wins — keeping this here means
    # the stub group has already finished before we register the real one.
    register_commands_methods(dispatcher)
    # slash.exec / session.status / complete.{slash,path} — must
    # come AFTER register_stub_methods so session.status's real handler
    # supersedes the (now-removed) hermes-only stub entry.
    register_slash_routing_methods(dispatcher, confirm_broker=confirm_broker)
    # turn.{send,subscribe,unsubscribe,cancel}. The handlers
    # need a SubscriptionEmitter to push streaming events; when the caller
    # has not built one (demo runner / production path pre-wire) we skip
    # registration so the dispatcher returns -32601 instead of crashing
    # mid-call. Both the umbrella and the production path forward the same
    # ``emitter`` kwarg, so parity holds whether or not it is passed.
    if emitter is not None:
        register_turn_methods(
            dispatcher,
            emitter=emitter,
            scheduler=scheduler,
            turn_ids=turn_ids,
            build_error=build_error,
        )
    # confirm.respond — needs a ConfirmBroker to resolve the pending
    # confirm future. Gated like turn.*: when no broker is supplied (demo
    # runner / drift test) the method is skipped so umbrella-vs-production
    # parity holds whether or not the broker is passed.
    if confirm_broker is not None:
        register_confirm_methods(dispatcher, confirm_broker=confirm_broker)
    # clarify.respond — needs a QuestionBroker to resolve the pending ask_user
    # future. Gated like confirm.*: skipped when no broker is supplied, so
    # umbrella-vs-production parity holds whether or not it is passed.
    if question_broker is not None:
        register_question_methods(dispatcher, question_broker=question_broker)


__all__ = [
    "register_aligned_methods",
    "register_aligned_methods_except_system",
    "register_system_methods",
    "register_cli_methods",
    "register_commands_methods",
    "register_setup_methods",
    "register_reload_methods",
    "register_config_methods",
    "register_session_methods",
    "register_terminal_methods",
    "register_stub_methods",
    "register_slash_routing_methods",
    "register_turn_methods",
    "register_confirm_methods",
    "register_question_methods",
]
