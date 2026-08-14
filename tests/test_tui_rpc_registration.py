"""Every method in the RPC contract must have a handler.

`skill.*` and `mcp.*` are declared in `models.py` METHOD_MODELS and in
`ui-tui/rpc-schema/openrpc.json` but no register call backs them, so calling them
in a real `raven tui` returns -32601. That gap predates this test and is
allowlisted below rather than silently tolerated: the point of the test is that
no NEW method joins it.

Checking only that direction is what let nine ui-tui-invoked names ship with no
handler at all, none of them ever declared in the contract this test reads. The
caller-side guard at the bottom of this file closes it by scanning what the
client calls. `skills.manage` was the example this docstring used to cite as the
visible cost; it now has a handler, and the guard is what keeps the next one
from repeating it.

The dispatcher here is built with a stub for every optional dependency, because
several groups are capability-gated rather than unimplemented:
`register_aligned_methods_except_system` skips `turn.*` when `emitter` is None,
and `approval.respond` / `confirm.respond` / `clarify.respond` when their broker
is None. Passing no kwargs makes those seven look unimplemented and pushes them
into the allowlist, which is the opposite of what this guard is for. The stubs
are never called - registration only stores them.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

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


# ---------------------------------------------------------------------------
# Caller-side guard
# ---------------------------------------------------------------------------
#
# The tests above check the contract against the handlers. That direction alone
# cannot catch a name ui-tui invokes without ever declaring, which is how nine
# methods (`delegation.*`, `shell.exec`, `skills.manage`, `command.dispatch`,
# `clipboard.paste`, `input.detect_drop`, `session.interrupt`,
# `subagent.interrupt`) shipped answering -32601 -- one of them on every spawn,
# where the client's shared rpc helper printed the rejection into the chat.
#
# So this scans what the client actually calls. A method reached through the
# stub table is fine: -32012 renders as "not supported", which is an answer.

_UI_TUI_SRC = Path(__file__).resolve().parents[1] / "ui-tui" / "src"

# `.rpc('x.y')` / `.request<T>('x.y')` / `.subscribe('x.y')`, with an optional
# type argument between the name and the paren.
_CALL_RE = re.compile(r"\b(?:rpc|request|notify|subscribe)\s*(?:<[^;{}]*?>)?\s*\(\s*['\"`]([a-z_]+\.[a-z_]+)['\"`]")

# Names that appear in a call position but are not calls into this server.
_NOT_SERVER_CALLS = {
    # `unsubscribeMethod` metadata and the fake gateway used by fixtures.
    "totally.made.up",
}


def _invoked_by_ui_tui() -> dict[str, str]:
    """Every RPC name ui-tui calls in production code -> the file it calls it from."""
    found: dict[str, str] = {}
    for path in sorted(_UI_TUI_SRC.rglob("*.ts*")):
        parts = set(path.parts)
        if "__tests__" in parts or path.name.endswith(".test.ts") or path.name.endswith(".test.tsx"):
            continue
        if path.name in ("gatewayClientStub.ts",):
            # A hand-written offline fake, not a caller.
            continue
        for match in _CALL_RE.finditer(path.read_text(encoding="utf-8")):
            found.setdefault(match.group(1), str(path.relative_to(_UI_TUI_SRC)))
    return {name: where for name, where in found.items() if name not in _NOT_SERVER_CALLS}


@pytest.mark.skipif(not _UI_TUI_SRC.is_dir(), reason="ui-tui sources not present in this checkout")
def test_every_method_ui_tui_calls_has_a_handler_or_a_stub() -> None:
    registered = _registered()
    unanswered = {name: where for name, where in _invoked_by_ui_tui().items() if name not in registered}
    assert unanswered == {}, (
        "ui-tui calls these with nothing registered, so they answer -32601: "
        f"{sorted(f'{name} ({where})' for name, where in unanswered.items())}"
    )


@pytest.mark.skipif(not _UI_TUI_SRC.is_dir(), reason="ui-tui sources not present in this checkout")
def test_the_scan_actually_finds_calls() -> None:
    # Without this, a regex that silently stops matching turns the guard above
    # into a test that passes on an empty set forever.
    invoked = _invoked_by_ui_tui()
    assert len(invoked) > 30, f"call scan found only {len(invoked)} methods; the regex likely broke"
    assert "turn.send" in invoked
# The other direction. `test_every_contract_method_is_registered` proves a
# declared method has a handler; this proves a handler is declared. Without it a
# new method can ship, work, and be called for months while `openrpc.json` and
# `METHOD_MODELS` never hear about it -- which is what had happened to 52 of the
# 107, the market's ten included.
#
# The cost of the gap is not documentation drift. `ui-tui` generates its
# TypeScript client from the contract, so an undeclared method has no generated
# type: the TUI either hand-rolls the call and its shapes (silently diverging
# from the server) or cannot make it at all.
#
# The allowlist below is the argument this comment used to forbid. It held 42
# names, emptied as they were declared, and was deleted. It is back holding nine
# that arrived on main afterwards, from the MR that gave them handlers -- and
# holding nine is the point: the number is the debt, visible, and it only goes
# down.
INHERITED_UNDECLARED: set[str] = {
    "clipboard.paste",
    "command.dispatch",
    "delegation.pause",
    "delegation.status",
    "input.detect_drop",
    "session.interrupt",
    "shell.exec",
    "skills.manage",
    "subagent.interrupt",
}


def test_every_registered_method_is_in_the_contract() -> None:
    undeclared = _registered() - set(METHOD_MODELS) - INHERITED_UNDECLARED
    assert undeclared == set(), (
        f"registered but absent from METHOD_MODELS / openrpc.json: {sorted(undeclared)}. "
        "Add the params/result models and the openrpc.json entry."
    )


def test_the_inherited_list_only_shrinks() -> None:
    """A name here that has since been declared, or is no longer registered, has
    to leave -- otherwise the set stops measuring the debt and starts hiding it."""
    registered = _registered()
    gone = {m for m in INHERITED_UNDECLARED if m not in registered}
    declared = {m for m in INHERITED_UNDECLARED if m in METHOD_MODELS}
    assert gone == set(), f"in INHERITED_UNDECLARED but no longer registered: {sorted(gone)}"
    assert declared == set(), f"in INHERITED_UNDECLARED but now declared; remove them: {sorted(declared)}"
