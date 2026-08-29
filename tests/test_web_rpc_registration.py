"""Every method in the web RPC contract must have a handler, and vice versa.

The web-dialect counterpart of ``tests/test_rpc_registration.py``. The
dispatcher is built exactly the way ``raven gateway`` builds its web one:
``Dispatcher()`` + ``register_web_methods(...)`` (raven/cli/gateway_commands.py),
so what this test sees registered is what a browser client can actually reach.
The stubs stand in for the live objects; registration only stores handlers, it
never calls them.

Before this file existed the ``raven.*`` namespace was entirely undeclared --
``raven/web_rpc/methods.py`` itself admits a misregistration used to surface as
an empty list in the UI instead of a wiring error. The contract lives in
``raven/web_rpc/models.py`` (WEB_METHOD_MODELS) mirrored by
``rpc-schema/openrpc-web.json``; this test ties both to the dispatcher.
"""

from __future__ import annotations

from types import SimpleNamespace

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.models import METHOD_MODELS
from raven.web_rpc.methods import register_web_methods
from raven.web_rpc.models import WEB_METHOD_MODELS


def _registered() -> set[str]:
    dispatcher = Dispatcher()
    register_web_methods(
        dispatcher,
        emitter=SimpleNamespace(),
        scheduler=SimpleNamespace(),
        turn_ids={},
        direct_targets={},
        agent=SimpleNamespace(),
        # A real gateway always passes its live CronService; None would make
        # raven.cron.* silently unregistered and this test blind to them.
        channel_manager=SimpleNamespace(),
    )
    return set(dispatcher.methods())


def test_every_declared_web_method_is_registered() -> None:
    missing = set(WEB_METHOD_MODELS) - _registered()
    assert missing == set(), f"declared in WEB_METHOD_MODELS but never registered: {sorted(missing)}"


def test_every_registered_raven_method_is_declared() -> None:
    """The other direction: a raven.* handler that ships without a contract
    entry is exactly the undeclared-dialect gap this file exists to close."""
    undeclared = {m for m in _registered() if m.startswith("raven.")} - set(WEB_METHOD_MODELS)
    assert undeclared == set(), (
        f"registered on the web dispatcher but absent from WEB_METHOD_MODELS / openrpc-web.json: "
        f"{sorted(undeclared)}. Add the params/result models and regenerate the schema entry."
    )


def test_every_shared_method_is_declared_in_the_terminal_contract() -> None:
    """The web dispatcher also serves terminal-dialect methods (system.*,
    turn.*, subagents.instance*). Those belong to openrpc.json's contract, so
    each must be declared there -- a name in neither contract is a method no
    schema-match test watches."""
    shared = {m for m in _registered() if not m.startswith("raven.")}
    undeclared = shared - set(METHOD_MODELS)
    assert undeclared == set(), (
        f"registered on the web dispatcher but declared in neither contract: {sorted(undeclared)}"
    )


def test_the_control_plane_registers_exactly_the_probe_vocabulary() -> None:
    """The C7 shrink is load-bearing: the raven.* dialect is live_probe's three
    channel methods, and a fourth raven.* registration reappearing here means a
    new client contract nobody declared."""
    registered = {n for n in _registered() if n.startswith("raven.")}
    assert registered == {"raven.channels.qr", "raven.channels.start", "raven.channels.live"}
