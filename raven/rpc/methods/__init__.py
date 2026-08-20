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
* ``subagents.list`` / ``subagents.probe`` (2) — third-party sub-agent roster
* ``session.{create, close, resume}`` (3) — Wave 6.5 lifecycle return-shape
  stubs (real SessionManager wiring deferred)
* ``terminal.resize`` (1) — Wave 6.5 SIGWINCH no-op + cols record
* ``delegation.{status, pause}`` / ``subagent.interrupt`` (3) — spawn HUD caps,
  the pause kill switch, and the overlay's per-row kill
* ``shell.exec`` (1) — the composer's ``!command`` escape
* ``skills.manage`` (1) — ``/skills`` over the on-disk registry and the hub
* ``clipboard.paste`` / ``input.detect_drop`` (2) — pasted image and dropped path
* ``command.dispatch`` (1) — slash fallback: skill load, else CLI
* ``session.interrupt`` (1) — Ctrl+C on the pre-typed-chat path
* hermes-only stub groups (27 names after Wave 6.5): 6 original groups
  (voice / browser / spawn_tree / process.stop / rollback / tools.configure)
  + 17 Wave 6.5 unaligned method names that return -32012

``skill.*`` and ``mcp.*`` are still declared in the contract with no handler, so
they answer -32601; ``tests/test_rpc_registration.py`` allowlists them by
name and fails on any addition to that set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.rpc.methods._stubs import register_stub_methods
from raven.rpc.methods.approval import register_approval_methods
from raven.rpc.methods.browser import register_browser_methods
from raven.rpc.methods.cli_dispatch import register_cli_methods
from raven.rpc.methods.clipboard import register_clipboard_methods
from raven.rpc.methods.command_dispatch import register_command_dispatch_methods
from raven.rpc.methods.commands import register_commands_methods
from raven.rpc.methods.config import register_config_methods
from raven.rpc.methods.confirm import register_confirm_methods
from raven.rpc.methods.console import register_console_methods
from raven.rpc.methods.dag import register_dag_methods
from raven.rpc.methods.delegation import register_delegation_methods
from raven.rpc.methods.input import register_input_methods
from raven.rpc.methods.instances import register_instance_methods
from raven.rpc.methods.memory import register_memory_methods
from raven.rpc.methods.model import register_model_methods
from raven.rpc.methods.plughub import register_plughub_methods
from raven.rpc.methods.question import register_question_methods
from raven.rpc.methods.reload import register_reload_methods
from raven.rpc.methods.session import register_session_methods
from raven.rpc.methods.setup import register_setup_methods
from raven.rpc.methods.shell import register_shell_methods
from raven.rpc.methods.skillhub import register_skillhub_methods
from raven.rpc.methods.skills import register_skills_methods
from raven.rpc.methods.slash_routing import register_slash_routing_methods
from raven.rpc.methods.subagent import register_subagent_methods
from raven.rpc.methods.subagents import register_subagents_methods
from raven.rpc.methods.system import register_system_methods
from raven.rpc.methods.terminal import register_terminal_methods
from raven.rpc.methods.turn import (
    register_session_interrupt_method,
    register_turn_methods,
)

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
    direct_targets: "dict[str, dict[str, str]] | None" = None,
    build_error: "RpcError | None" = None,
    send_frame: "Any" = None,
) -> None:
    """Register every aligned RPC handler on a dispatcher.

    Used by both the v0.0.1 demo runner (``scripts/run_v001_demo.py``) and
    the production ``tui_commands.run_subprocess_with_rpc`` wrapper. Keeping
    a single registration point avoids drift between the two spawn paths.

    ``emitter`` and the build_rpc_spine bundle (``scheduler`` / ``turn_ids`` /
    ``build_error``) are forwarded to :func:`register_turn_methods` — when
    ``emitter`` is ``None`` the ``turn.*`` group is skipped (the demo runner
    path that does not own a streaming subscription channel still works without
    them); ``agent_loop_factory`` is forwarded to the session methods.
    ``confirm_broker`` is forwarded to :func:`register_confirm_methods`;
    ``approval_broker`` gates the shell approval response surface so callers
    without an interactive broker do not expose an unusable approval method.
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
        direct_targets=direct_targets,
        build_error=build_error,
        send_frame=send_frame,
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
    direct_targets: "dict[str, dict[str, str]] | None" = None,
    build_error: "RpcError | None" = None,
    send_frame: "Any" = None,
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
    register_reload_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_config_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_subagent_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_subagents_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_instance_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_dag_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_session_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_terminal_methods(dispatcher)
    register_stub_methods(dispatcher)
    # model.{options,save_key,disconnect,add_model,remove_model,endpoints,
    # add_endpoint,remove_endpoint}: real handlers
    # must come AFTER register_stub_methods (Dispatcher.register raises on
    # duplicate; the stub group no longer owns these names).
    register_model_methods(dispatcher)
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
    # The group ui-tui calls but nothing ever backed, so each came back -32601.
    # None of these names is in the stub table, so ordering against
    # ``register_stub_methods`` does not matter -- but they must stay after it
    # for the same reason the two groups above do, if one is ever stubbed.
    register_delegation_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_shell_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_skills_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_clipboard_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_input_methods(dispatcher)
    register_command_dispatch_methods(
        dispatcher,
        agent_loop_factory=agent_loop_factory,
        confirm_broker=confirm_broker,
    )
    # Not gated on ``emitter`` unlike the rest of turn.*: the legacy Ctrl+C path
    # needs no subscription channel, and gating it would leave exactly the
    # configurations that still use it on -32601.
    register_session_interrupt_method(dispatcher)
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
            direct_targets=direct_targets,
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
    # ext.list / cron.* / settings.* / channels.status / fs.* — the extension,
    # schedule, settings and workspace surface. Fresh namespaces, so they do
    # not collide with the stub group, and the loop factory is optional: a
    # transport without one still gets the read-only handlers.
    register_console_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    # memory.* — a read-only view onto the memory engine, which shipped
    # without an RPC surface. (A matching subagent.* view waits for the
    # transcript writer that would give it anything to list.)
    register_memory_methods(dispatcher)
    # skillhub.* / plughub.* / plug.* — the catalogue half of two things raven
    # already runs: skills (memory_engine.skill_forge) and plugins
    # (raven.plugin). Registered unconditionally so a network failure reads as a
    # handler error a caller can show, not as -32601.
    register_skillhub_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    register_plughub_methods(dispatcher, agent_loop_factory=agent_loop_factory)
    # browser.* — the reader's half of the page the agent's browser tools
    # drive. Registered unconditionally: browser.state is what tells a caller
    # the optional extra is missing, so it has to answer even then. The
    # streaming half (browser.watch) needs the transport's notification sink;
    # a transport without one simply has no live view.
    register_browser_methods(dispatcher, send_frame=send_frame)


__all__ = [
    "register_aligned_methods",
    "register_aligned_methods_except_system",
    "register_system_methods",
    "register_cli_methods",
    "register_commands_methods",
    "register_setup_methods",
    "register_reload_methods",
    "register_config_methods",
    "register_subagent_methods",
    "register_subagents_methods",
    "register_instance_methods",
    "register_dag_methods",
    "register_console_methods",
    "register_memory_methods",
    "register_plughub_methods",
    "register_session_methods",
    "register_skillhub_methods",
    "register_terminal_methods",
    "register_stub_methods",
    "register_model_methods",
    "register_slash_routing_methods",
    "register_turn_methods",
    "register_session_interrupt_method",
    "register_delegation_methods",
    "register_shell_methods",
    "register_skills_methods",
    "register_clipboard_methods",
    "register_input_methods",
    "register_command_dispatch_methods",
    "register_approval_methods",
    "register_browser_methods",
    "register_confirm_methods",
    "register_question_methods",
]
