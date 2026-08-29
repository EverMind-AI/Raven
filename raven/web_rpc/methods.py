"""Which JSON-RPC methods the web channel serves.

The gateway stands up a *second* dispatcher for the browser, separate from the
terminal's, and what it registers on that one is the browser's entire vocabulary.
That list lived inline in ``raven.cli.gateway_commands`` inside a several-hundred
line command, where the only way to learn it was to read the command and the only
way to get it wrong was silent: a method nobody registered answers
``method not found``, which a client reports as an empty list or a missing
feature rather than as a wiring mistake. Here it is one function, so it can be
called by a test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.subscriptions import SubscriptionEmitter
    from raven.spine import Scheduler


def register_web_methods(
    dispatcher: "Dispatcher",
    *,
    emitter: "SubscriptionEmitter",
    scheduler: "Scheduler",
    turn_ids: dict[str, str],
    direct_targets: dict[str, dict[str, str]],
    agent: Any,
    channel_manager: Any,
) -> None:
    """Register every method the web channel serves on ``dispatcher``.

    ``direct_targets`` must be the same object handed to ``build_web``: the turn
    methods write a direct chat's addressee into it and the outlet reads it back
    to tag that lane's events.
    """
    from raven.rpc.methods.instances import register_instance_methods
    from raven.rpc.methods.system import register_system_methods
    from raven.rpc.methods.turn import register_turn_methods
    from raven.web_rpc.methods_config import register_config_methods

    # The same channel its turns run on, or the handshake hands a web client the
    # terminal's channel and default session key.
    register_system_methods(dispatcher, channel="web")
    register_turn_methods(
        dispatcher,
        emitter=emitter,
        scheduler=scheduler,
        turn_ids=turn_ids,
        direct_targets=direct_targets,
        default_channel="web",
    )
    # The instance roster and one instance's past turns. A direct chat needs both
    # over the connection the browser already holds: without them it can address
    # an instance it cannot list, and reopening one shows an empty conversation
    # whose record is on disk.
    #
    # The factory is not optional here even though the parameter is: the history
    # handler resolves a session's record directory through the manager, and
    # without a loop to reach it answers `{"turns": []}` -- a reopened chat that
    # looks empty rather than one that failed. Measured against a real record on
    # disk before this was passed.
    register_instance_methods(dispatcher, agent_loop_factory=lambda: agent)
    # The control plane's live-channel methods (the probe's whole vocabulary).
    register_config_methods(dispatcher, channel_manager=channel_manager)


__all__ = ["register_web_methods"]
