"""Hermes-only stub RPC handlers.

These method names exist in the fork-imported hermes UI but Raven does not
back them with real functionality in v0.1. Rather than physically remove the
slash commands (which would inflate the fork-import diff and worsen future
upstream merges), we wire each name to a stub that raises
:class:`NotSupportedInV01Error` (JSON-RPC -32012). The hermes UI already has
an error-toast component that consumes this shape gracefully — the user types
the slash command and sees a transient "Not supported" toast.

The stub group covers the original 6 logical hermes-only groups (10 names)
plus the additional unaligned method names that ui-tui actually invokes but
Raven v0.1 does not back with real functionality:

* ``voice.toggle`` / ``voice.record`` — voice features (original)
* ``browser.manage`` — browser automation (original)
* ``spawn_tree.save`` / ``spawn_tree.list`` / ``spawn_tree.load`` — hermes's
  sub-agent topology UX (Raven uses Sentinel/Subagent differently)
* ``process.stop`` — hermes's filesystem-rollback-aware turn killer; we route
  the user to Ctrl+C
* ``rollback.list`` / ``rollback.diff`` / ``rollback.restore`` — fs-level
  rollback (we point users at ``git`` instead)
* ``tools.configure`` — TUI-side tool config editor; in Raven users edit
  ``~/.raven/config.json`` directly
* ``session.{compress, save, status, steer, usage}`` — hermes session-mgmt
  slash commands Raven doesn't back yet (``session.title``,
  ``session.undo``, and ``session.branch`` were promoted to real handlers
  in methods/session.py)
* ``skills.reload`` — hermes skill hot-reload UX (Raven uses SkillForge
  closed loop, no manual reload trigger)
* ``reload.env`` — hermes env-file hot-reload (Raven reads env on process
  start only)
* ``approval.respond`` / ``sudo.respond`` / ``secret.respond`` — hermes
  interactive approval flows (Raven v0.1 has no approval UI)
* ``image.attach`` — hermes image-paste attachment
* ``prompt.submit`` / ``prompt.background`` — hermes "stash this prompt for
  later" flow; Raven v0.1 only supports inline submit via the chat turn
  pipeline

Dead-code cleanup (physical removal of the hermes slash commands) is a
follow-up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.tui_rpc.errors import NotSupportedInV01Error

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher


# ---------------------------------------------------------------------------
# Stub payload table — (method_name, error_string, optional_hint)
# ---------------------------------------------------------------------------

_STUB_DEFINITIONS: tuple[tuple[str, str, str | None], ...] = (
    ("voice.toggle", "voice not supported in Raven v0.1", None),
    ("voice.record", "voice recording not supported in Raven v0.1", None),
    ("browser.manage", "browser automation not supported", None),
    (
        "process.stop",
        "process.stop not supported; use Ctrl+C",
        "Press Ctrl+C in the TUI to interrupt a running turn.",
    ),
    (
        "rollback.list",
        "filesystem-level rollback not supported in v0.1; use git",
        None,
    ),
    (
        "rollback.diff",
        "filesystem-level rollback not supported in v0.1; use git",
        None,
    ),
    (
        "rollback.restore",
        "filesystem-level rollback not supported in v0.1; use git",
        None,
    ),
    (
        "spawn_tree.save",
        "spawn_tree topology not supported (Raven uses sentinel/subagent differently)",
        None,
    ),
    (
        "spawn_tree.list",
        "spawn_tree topology not supported (Raven uses sentinel/subagent differently)",
        None,
    ),
    (
        "spawn_tree.load",
        "spawn_tree topology not supported (Raven uses sentinel/subagent differently)",
        None,
    ),
    (
        "tools.configure",
        "tools configuration via TUI not supported; edit config.json",
        None,
    ),
    # session.* slash-command stubs. ui-tui exposes these slash
    # commands; v0.1 Raven answers each with -32012 so the hermes
    # error-toast renders gracefully instead of -32601 method_not_found.
    # (``session.branch`` was promoted to a real handler in methods/session.py
    # by session fork; removed here so the dispatcher does not
    # double-register it.)
    (
        "session.compress",
        "session.compress not supported in Raven v0.1",
        "Raven uses Curator context engine to manage context size; manual compress not exposed.",
    ),
    (
        "session.save",
        "session.save not supported in Raven v0.1",
        "Sessions are append-only JSONL — no explicit save needed.",
    ),
    # NOTE: ``session.status`` was previously stubbed (-32012) but is now
    # promoted to a real handler in
    # ``raven.tui_rpc.methods.slash_routing.session_status`` that delegates
    # to ``cli.dispatch(["status"])``.
    (
        "session.steer",
        "session.steer not supported in Raven v0.1",
        "Edit the system prompt via config.json `agents.defaults.system_prompt`.",
    ),
    # NOTE: ``session.undo`` was previously stubbed (-32012) but is now promoted
    # to a real handler in ``raven.tui_rpc.methods.session.session_undo`` that
    # drops the last turn in place (parity test
    # ``test_tui_rpc_session.py::test_session_undo_*``).
    (
        "session.usage",
        "session.usage not supported in Raven v0.1",
        "Use the TUI footer token-usage widget (tui.show_token_usage=true).",
    ),
    # skills.reload — hermes skill hot-reload. Raven SkillForge runs a
    # closed-loop Detect→Draft→Active→Evolve→Retire pipeline; no manual
    # reload trigger.
    (
        "skills.reload",
        "skills.reload not supported in Raven v0.1",
        "Raven SkillForge auto-detects and evolves skills; no manual reload.",
    ),
    # reload.env — env-file hot reload. Raven reads env once on process
    # start; relaunch `raven tui` to pick up changes.
    (
        "reload.env",
        "reload.env not supported in Raven v0.1",
        "Restart `raven tui` to pick up environment changes.",
    ),
    # approval / sudo / secret response — hermes interactive approval flows.
    # Raven v0.1 has no approval UI; users edit config directly.
    (
        "approval.respond",
        "approval.respond not supported in Raven v0.1",
        "Raven v0.1 has no interactive approval flow.",
    ),
    (
        "sudo.respond",
        "sudo.respond not supported in Raven v0.1",
        "Raven v0.1 has no interactive sudo prompt.",
    ),
    (
        "secret.respond",
        "secret.respond not supported in Raven v0.1",
        "Raven v0.1 has no interactive secret prompt.",
    ),
    # NOTE: ``commands.catalog`` was previously stubbed (-32012) but is now
    # promoted to a real handler in
    # ``raven.tui_rpc.methods.commands.commands_catalog`` that reflects
    # ``raven.cli.commands.app`` to build a Typer-aware slash catalog. See
    # ``docs/openspec/changes/harness-command-catalog-dynamic/``.
    # image.attach — hermes image-paste attachment. v0.1 Raven is
    # text-only.
    (
        "image.attach",
        "image.attach not supported in Raven v0.1",
        "Raven v0.1 is text-only; vision support deferred.",
    ),
    # prompt.submit / prompt.background — hermes "stash a draft prompt"
    # flows. Raven v0.1 only supports inline submit via the chat turn
    # pipeline (turn.send when wired).
    (
        "prompt.submit",
        "prompt.submit not supported in Raven v0.1",
        "Use the chat composer to submit prompts inline.",
    ),
    (
        "prompt.background",
        "prompt.background not supported in Raven v0.1",
        "Background / queued prompts are deferred to a follow-up L2.",
    ),
)


HERMES_ONLY_STUB_METHODS: tuple[str, ...] = tuple(name for name, _msg, _hint in _STUB_DEFINITIONS)


def _make_stub(error_msg: str, hint: str | None):
    """Build an async handler that raises NotSupportedInV01Error with payload."""

    async def _handler(params: dict[str, Any]) -> dict:  # pragma: no cover — never returns
        data: dict[str, Any] = {"error": error_msg}
        if hint is not None:
            data["hint"] = hint
        raise NotSupportedInV01Error(error_msg, data=data)

    return _handler


def register_stub_methods(dispatcher: "Dispatcher") -> None:
    """Register all 6-group hermes-only stub methods on a dispatcher.

    Each handler raises :class:`NotSupportedInV01Error` (JSON-RPC -32012);
    the dispatcher serializes it to a ``{code, message, data: {error, hint?}}``
    error frame which hermes's existing error-toast consumes verbatim.
    """
    for method, error_msg, hint in _STUB_DEFINITIONS:
        dispatcher.register(method, _make_stub(error_msg, hint))


__all__ = [
    "HERMES_ONLY_STUB_METHODS",
    "register_stub_methods",
]
