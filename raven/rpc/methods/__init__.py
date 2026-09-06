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

from raven.rpc.methods._stubs import register_stub_methods
from raven.rpc.methods.approval import register_approval_methods
from raven.rpc.methods.cli_dispatch import register_cli_methods
from raven.rpc.methods.commands import register_commands_methods
from raven.rpc.methods.config import register_config_methods
from raven.rpc.methods.confirm import register_confirm_methods
from raven.rpc.methods.model import register_model_methods
from raven.rpc.methods.question import register_question_methods
from raven.rpc.methods.reload import register_reload_methods
from raven.rpc.methods.session import register_session_methods
from raven.rpc.methods.setup import register_setup_methods
from raven.rpc.methods.slash_routing import register_slash_routing_methods
from raven.rpc.methods.system import register_system_methods
from raven.rpc.methods.terminal import register_terminal_methods
from raven.rpc.methods.turn import register_turn_methods

if TYPE_CHECKING:
    from raven.rpc.approval_broker import ApprovalBroker
    from raven.rpc.confirm_broker import ConfirmBroker
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.errors import RpcError
    from raven.rpc.methods.session import AgentLoopFactory
    from raven.rpc.question_broker import QuestionBroker
    from raven.rpc.subscriptions import SubscriptionEmitter
    from raven.spine.scheduler import Scheduler


def register_aligned_methods(
    dispatcher: "Dispatcher",
    *,
    emitter: "SubscriptionEmitter | None" = None,
    agent_loop_factory: "AgentLoopFactory | None" = None,
    approval_broker: "ApprovalBroker | None" = None,
    confirm_broker: "ConfirmBroker | None" = None,
    question_broker: "QuestionBroker | None" = None,
    scheduler: "Scheduler | None" = None,
    turn_ids: "dict[str, str] | None" = None,
    build_error: "RpcError | None" = None,
    default_channel: str = "tui",
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
    ``confirm_broker`` is forwarded to :func:`register_confirm_methods`;
    ``approval_broker`` gates the shell approval response surface so callers
    without an interactive broker do not expose an unusable approval method.
    ``default_channel`` is stamped on every turn ``turn.send`` submits and must
    match the channel the delivery outlet was registered under.
    """
    register_system_methods(dispatcher)
    register_aligned_methods_except_system(
        dispatcher,
        emitter=emitter,
        agent_loop_factory=agent_loop_factory,
        approval_broker=approval_broker,
        confirm_broker=confirm_broker,
        question_broker=question_broker,
        scheduler=scheduler,
        turn_ids=turn_ids,
        build_error=build_error,
        default_channel=default_channel,
    )


def register_aligned_methods_except_system(
    dispatcher: "Dispatcher",
    *,
    emitter: "SubscriptionEmitter | None" = None,
    agent_loop_factory: "AgentLoopFactory | None" = None,
    approval_broker: "ApprovalBroker | None" = None,
    confirm_broker: "ConfirmBroker | None" = None,
    question_broker: "QuestionBroker | None" = None,
    scheduler: "Scheduler | None" = None,
    turn_ids: "dict[str, str] | None" = None,
    build_error: "RpcError | None" = None,
    default_channel: str = "tui",
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
    register_config_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_session_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_terminal_methods(dispatcher)
    register_stub_methods(dispatcher)
    # model.{options,save_key,disconnect,add_model,remove_model,endpoints,
    # add_endpoint,remove_endpoint}: real handlers
    # must come AFTER register_stub_methods (Dispatcher.register raises on
    # duplicate; the stub group no longer owns these names).
    register_model_methods(dispatcher, agent_loop_factory=agent_loop_factory)
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
    # Unlike generic stubs, approval.respond is a capability-bearing endpoint.
    # Register it only when this gateway owns an interactive approval broker.
    if approval_broker is not None:
        register_approval_methods(dispatcher, approval_broker=approval_broker)
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
            # Has to reach BOTH collaborators. The delivery outlet is registered
            # under one channel name and this stamps the name a turn is submitted
            # with; the hub routes by that name, so giving it to one side and not
            # the other loses every turn with no error anywhere.
            default_channel=default_channel,
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
    "register_model_methods",
    "register_slash_routing_methods",
    "register_turn_methods",
    "register_approval_methods",
    "register_confirm_methods",
    "register_question_methods",
]
