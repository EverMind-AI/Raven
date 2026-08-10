"""Every method in the RPC contract must have a handler.

`skill.*` and `mcp.*` are declared in `models.py` METHOD_MODELS and in
`ui-tui/rpc-schema/openrpc.json` but no register call backs them, so calling them
in a real `raven tui` returns -32601. That gap predates this test and is
allowlisted below rather than silently tolerated: the point of the test is that
no NEW method joins it. `ui-tui/src/components/skillsHub.tsx` is the visible
cost of the gap going unnoticed - it calls `skills.manage`, a name that exists
only in `ui-tui/src/gatewayClientStub.ts`.

The dispatcher here is built with a stub for every optional dependency, because
several groups are capability-gated rather than unimplemented:
`register_aligned_methods_except_system` skips `turn.*` when `emitter` is None,
and `approval.respond` / `confirm.respond` / `clarify.respond` when their broker
is None. Passing no kwargs makes those seven look unimplemented and pushes them
into the allowlist, which is the opposite of what this guard is for. The stubs
are never called - registration only stores them.
"""

from __future__ import annotations

from types import SimpleNamespace

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.methods import register_aligned_methods
from raven.tui_rpc.models import METHOD_MODELS

# Declared in the contract, with no handler in any register_*_methods module.
# Shrinking this set is progress; growing it needs a reason in the PR description.
KNOWN_UNREGISTERED = {
    "mcp.list",
    "mcp.test",
    "mcp.tools",
    "session.get",
    "session.history",
    "skill.list",
    "skill.pin",
    "skill.unpin",
}

# Names declared in the contract whose handler has not landed yet. Distinct from
# KNOWN_UNREGISTERED, which is an accepted permanent gap: an entry here is a
# temporary state that a later change is expected to close by registering the
# handler and deleting the name, never by adding it to KNOWN_UNREGISTERED
# instead. Kept empty on the base branch so a method never quietly ships without
# a handler for more than the change that adds it.
IN_PROGRESS: set[str] = set()


def _registered() -> set[str]:
    dispatcher = Dispatcher()
    register_aligned_methods(
        dispatcher,
        emitter=SimpleNamespace(),
        approval_broker=SimpleNamespace(),
        confirm_broker=SimpleNamespace(),
        question_broker=SimpleNamespace(),
        scheduler=SimpleNamespace(),
    )
    return set(dispatcher.methods())


def test_every_contract_method_is_registered() -> None:
    missing = set(METHOD_MODELS) - _registered() - KNOWN_UNREGISTERED - IN_PROGRESS
    assert missing == set(), f"declared in METHOD_MODELS but never registered: {sorted(missing)}"


def test_the_capability_gated_groups_do_register_when_their_dependency_is_present() -> None:
    # Pins the reason this module passes stubs at all: without them these seven
    # read as unimplemented, and an allowlist that names them would hide a real
    # regression on any one of them.
    registered = _registered()
    for name in (
        "approval.respond",
        "clarify.respond",
        "confirm.respond",
        "turn.cancel",
        "turn.send",
        "turn.subscribe",
        "turn.unsubscribe",
    ):
        assert name in registered, name


def test_neither_allowlist_names_a_method_that_is_registered() -> None:
    # A stale entry in either set would hide a future regression on that name.
    registered = _registered()
    assert (KNOWN_UNREGISTERED | IN_PROGRESS) & registered == set()


def test_subagents_methods_are_registered() -> None:
    registered = _registered()
    for name in (
        "subagents.list",
        "subagents.add",
        "subagents.update",
        "subagents.remove",
        "subagents.toggle",
        "subagents.probe",
        "subagents.test",
        "subagents.test_cancel",
    ):
        assert name in registered, name


def test_no_subagents_method_is_left_in_progress() -> None:
    assert IN_PROGRESS == set()
