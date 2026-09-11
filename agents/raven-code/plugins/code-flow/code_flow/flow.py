"""The turn-frame hook: the code flow's one seat on the loop.

Three axes ride it. At the inbound phase the hook books the session into the
process ledger and, when other sessions are mid-turn in the same directory,
puts a concurrency notice in front of this turn's prompt -- the model's view
of this turn only; the session record keeps the user's own words. At every
iteration it refreshes the session's in-flight mark, so a long turn stays
counted. At the send phase it files the workspace report (the harness
manifest, attributed to the session only while no other session shared the
directory) under the turn's ``acp_meta`` observer stash, which the ACP layer
answers back to the host as the prompt response's ``_meta``, and clears the
session's in-flight mark.

One contributed hook row on purpose: the registry serves hook names sorted,
so the axis order is part of this class, not of the manifest.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import ValidationError

from code_flow.config import FlowConfig
from code_flow.manifest import build_manifest
from code_flow.sessions import LEDGER, SessionLedger
from raven.agent import workdir
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision

if TYPE_CHECKING:
    from raven.plugins.context import PluginContext

#: The observer entry the ACP layer relays as the prompt response's ``_meta``.
ACP_META_OBSERVER = "acp_meta"
#: The ``_meta`` namespace this product's workspace report travels under.
MANIFEST_META_KEY = "raven.harnessManifest"

CONCURRENCY_NOTICE = (
    "# Workspace concurrency\n"
    "{count} other Raven-Code session{plural} {verb} working in this same directory right "
    "now, and nothing locks the tree. Re-read a file immediately before every write; modify "
    "only the files this task explicitly covers; never overwrite unrelated or unexplained "
    "existing changes. If a target file changes unexpectedly or appears concurrently modified "
    "by another task, stop writing and report it."
)


def concurrency_notice(peers: int) -> str:
    plural = "" if peers == 1 else "s"
    verb = "is" if peers == 1 else "are"
    return CONCURRENCY_NOTICE.format(count=peers, plural=plural, verb=verb)


class CodeFlowHook(AgentHook):
    """The code flow's turn-frame conduct."""

    def __init__(self, ledger: SessionLedger | None = None) -> None:
        self._ledger = ledger if ledger is not None else LEDGER

    @property
    def name(self) -> str:
        return "code_flow"

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        self._ledger.begin_turn(ctx.session_key, workdir.current())
        peers = self._ledger.peers_in_flight(ctx.session_key)
        if peers == 0:
            return HookDecision()
        return HookDecision(modified_content=f"{concurrency_notice(peers)}\n\n{ctx.inbound_content or ''}")

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        self._ledger.touch(ctx.session_key)
        return HookDecision()

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        record = self._ledger.record(ctx.session_key)
        cwd = workdir.current()
        if cwd is None and record is not None and record.cwd:
            cwd = Path(record.cwd)
        report = build_manifest(
            ctx.session_key,
            cwd,
            record.base_commit if record is not None else None,
            shared_with=len(record.peers) if record is not None else 0,
        )
        observers = ctx.metadata.setdefault("observers", {})
        if isinstance(observers, dict):
            stash = observers.setdefault(ACP_META_OBSERVER, {})
            if isinstance(stash, dict):
                stash[MANIFEST_META_KEY] = report
        self._ledger.end_turn(ctx.session_key)
        return HookDecision()


class SessionForget:
    """The session-events seat: a deleted session leaves the ledger."""

    def __init__(self, ledger: SessionLedger) -> None:
        self._ledger = ledger

    def on_session_deleted(self, session_key: str, removed: bool) -> None:
        self._ledger.forget(session_key)


def make_session_forget(ledger: SessionLedger) -> SessionForget:
    return SessionForget(ledger)


def _flow_config(ctx: "PluginContext", seat: str) -> FlowConfig | None:
    try:
        return FlowConfig.from_slice(dict(ctx.config or {}))
    except ValidationError as exc:
        logger.warning("code-flow: config slice is malformed; declining the {}: {}", seat, exc)
        return None


def make_flow_hook(ctx: "PluginContext") -> CodeFlowHook | None:
    """Factory for the ``code_flow`` hook contribution.

    Declines (returns None) when the slice leaves the flow off: a disabled
    product casts no surface at all (D6). A slice that does not parse
    declines too, with the reason logged.
    """
    cfg = _flow_config(ctx, "hook")
    if cfg is None or not cfg.enabled:
        return None
    return CodeFlowHook()


def make_session_observer(ctx: "PluginContext") -> SessionForget | None:
    """Factory for the ``session_forget`` observer contribution; same admission."""
    cfg = _flow_config(ctx, "observer")
    if cfg is None or not cfg.enabled:
        return None
    return SessionForget(LEDGER)


__all__ = [
    "ACP_META_OBSERVER",
    "CONCURRENCY_NOTICE",
    "MANIFEST_META_KEY",
    "CodeFlowHook",
    "SessionForget",
    "concurrency_notice",
    "make_flow_hook",
    "make_session_forget",
    "make_session_observer",
]
