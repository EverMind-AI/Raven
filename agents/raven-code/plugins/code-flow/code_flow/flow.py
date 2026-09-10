"""The code flow's turn hooks.

Repository instructions and workspace concurrency notices join the system
message through before_iteration. They never rewrite the inbound query.
The hook tracks the turn in the session ledger,
binds and restores its checklist, and files the workspace report at send.
Session deletion discards product state; new session IDs isolate new tasks.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import ValidationError

from code_flow.config import FlowConfig
from code_flow.manifest import build_manifest
from code_flow.sessions import LEDGER, SessionLedger
from code_flow.system_context import inject_system_context
from code_flow.tools.read_state import Owner, ReadSessions, forget_session, owner_for
from code_flow.tools.todo import STORES, TodoStore
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


#: The coding set of a checkout's own instruction files, read from the bound
#: working directory. The launcher renders these names into the slice; the
#: slice decides, so an eval that must judge the model alone empties it.
PROJECT_FILES = ["AGENTS.md", "CLAUDE.md", "CONTEXT.md"]

#: Per-file ceiling. A repository can carry an arbitrarily large markdown at
#: these names, and a turn's prompt must not inherit that size.
PROJECT_FILE_MAX_CHARS = 24_000

PROJECT_INSTRUCTIONS_HEAD = (
    "# Repository instructions\n"
    "The working directory carries its own instruction files. They describe how this "
    "repository is built, tested and contributed to; follow them for this task unless the "
    "task itself says otherwise."
)


def project_instructions(root: Path | None, names: list[str]) -> str:
    """The bound checkout's instruction files, as one block, or empty.

    Nothing bound reads nothing: unbound, these names would resolve against the
    agent's own home, whose files are the agent's and not a project's. A name
    that is missing, is not a regular file, or cannot be read is skipped
    rather than raised -- context assembly runs on every turn, and one bad
    symlink in someone's checkout must not end the conversation. Not fenced as
    untrusted: these are the project's standing instructions to whoever works
    in it, the same footing the conduct stands on, and a data fence would tell
    the model to ignore exactly what it is being handed to follow.
    """
    if root is None or not names:
        return ""
    try:
        root = Path(root).resolve(strict=True)
    except (OSError, RuntimeError):
        return ""
    parts: list[str] = []
    seen: set[Path] = set()
    for name in names:
        path = Path(root) / name
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                logger.warning("code-flow: skipping an instruction file outside the working directory: {}", path)
                continue
            if resolved in seen or not resolved.is_file():
                continue
            with resolved.open(encoding="utf-8", errors="replace") as source:
                text = source.read(PROJECT_FILE_MAX_CHARS + 1)
            seen.add(resolved)
        except (OSError, RuntimeError) as exc:
            logger.warning("code-flow: could not read the project file {}: {}", path, exc)
            continue
        if len(text) > PROJECT_FILE_MAX_CHARS:
            text = text[:PROJECT_FILE_MAX_CHARS] + "\n\n... (truncated to fit the context window)"
        parts.append(f"## {path.name}\n\n{text.rstrip()}")
    if not parts:
        return ""
    return PROJECT_INSTRUCTIONS_HEAD + "\n\n" + "\n\n".join(parts)


class CodeFlowHook(AgentHook):
    """The code flow's turn-frame conduct."""

    def __init__(
        self,
        ledger: SessionLedger | None = None,
        project_files: list[str] | None = None,
        todos: TodoStore | None = None,
        *,
        flow_enabled: bool = True,
        reads: ReadSessions | None = None,
    ) -> None:
        self._ledger = ledger if ledger is not None else LEDGER
        self._project_files = list(project_files or [])
        # The checklist store this product's ``todo`` tool writes through, or
        # None when the tool face is not served (nothing to bind or restore).
        self._todos = todos
        self._flow_enabled = flow_enabled
        self._reads = reads

    @property
    def name(self) -> str:
        return "code_flow"

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        # Commands return before iteration or after_send, so they must not
        # create an in-flight mark for a turn that will never report back.
        if (ctx.inbound_content or "").strip().lower() in {"/new", "/help"}:
            return HookDecision()
        cwd = workdir.current()
        if self._flow_enabled:
            self._ledger.begin_turn(ctx.session_key, cwd)
        if self._todos is not None:
            try:
                self._todos.bind(ctx.session_key, cwd)
                ctx.metadata["code_flow.todo_binding"] = (ctx.session_key, str(cwd) if cwd is not None else None)
            except Exception:  # noqa: BLE001 - a record problem must not cost the turn
                logger.exception("code-flow: could not bind the checklist for {}", ctx.session_key)
        return HookDecision()

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if self._reads is not None:
            self._reads.bind(ctx.session_key)
        if self._flow_enabled:
            self._ledger.touch(ctx.session_key)
        decision = HookDecision()
        if self._todos is not None:
            # System turns skip inbound, and the loop can select another
            # session after inbound. Per-turn metadata survives rollbacks.
            cwd = workdir.current()
            binding = (ctx.session_key, str(cwd) if cwd is not None else None)
            try:
                if ctx.metadata.get("code_flow.todo_binding") != binding or not self._todos.is_bound:
                    self._todos.bind(*binding)
                    ctx.metadata["code_flow.todo_binding"] = binding
                if ctx.messages is not None:
                    snapshot = self._todos.snapshot_if_hidden(ctx.messages)
                    if snapshot is not None:
                        logger.info("code-flow: restoring the checklist into context for {}", ctx.session_key)
                        decision = HookDecision(append_note=snapshot, notes=["code-flow: checklist snapshot restored"])
            except Exception:  # noqa: BLE001 - a checklist failure must not discard repository instructions
                logger.exception("code-flow: could not restore the checklist for {}", ctx.session_key)
        if self._flow_enabled:
            cwd = workdir.current()
            key = "code_flow.repository_instructions"
            cached = ctx.metadata.get(key)
            if cached is None or cached[0] != cwd:
                cached = (cwd, project_instructions(cwd, self._project_files))
                ctx.metadata[key] = cached
            peers = self._ledger.peers_in_flight(ctx.session_key)
            injected = inject_system_context(
                ctx, cached[1], concurrency_notice(peers) if peers else "", pending_note=decision.append_note
            )
            if injected.short_circuit_result is not None:
                return injected
        return decision

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        if self._reads is not None:
            self._reads.unbind()
        if not self._flow_enabled:
            return HookDecision()
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
    """The session-events seat: a deleted session leaves the ledger, and its
    saved checklist goes with it."""

    def __init__(self, ledger: SessionLedger, todos: TodoStore | None = None, read_owner: Owner | None = None) -> None:
        self._ledger = ledger
        self._todos = todos
        self._read_owner = read_owner

    def on_session_deleted(self, session_key: str, removed: bool) -> None:
        self._ledger.forget(session_key)
        if self._read_owner is not None:
            forget_session(self._read_owner, session_key)
        if self._todos is not None:
            try:
                if self._todos.discard(session_key):
                    logger.info("code-flow: discarded the saved checklist of deleted session {}", session_key)
            except OSError as exc:
                logger.warning("code-flow: could not discard the checklist of {}: {}", session_key, exc)


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

    Checklist and file-observation lifecycles remain while tools are enabled,
    even when the flow's notices and workspace reports are off. An absent or malformed
    slice casts no surface.
    """
    cfg = _flow_config(ctx, "hook")
    if cfg is None or not (cfg.enabled or cfg.tools.enabled):
        return None
    return CodeFlowHook(
        project_files=cfg.project_files,
        todos=_todo_store(ctx, cfg),
        flow_enabled=cfg.enabled,
        reads=ReadSessions(owner_for(ctx)) if cfg.tools.enabled else None,
    )


def make_session_observer(ctx: "PluginContext") -> SessionForget | None:
    """Factory for the ``session_forget`` observer contribution; same admission."""
    cfg = _flow_config(ctx, "observer")
    if cfg is None or not (cfg.enabled or cfg.tools.enabled):
        return None
    return SessionForget(LEDGER, todos=_todo_store(ctx, cfg), read_owner=owner_for(ctx) if cfg.tools.enabled else None)


def _todo_store(ctx: "PluginContext", cfg: FlowConfig) -> TodoStore | None:
    """The checklist store the ``todo`` tool factory uses, or None when the
    tool face is off -- then there is no tool to bind a session for."""
    if not cfg.tools.enabled:
        return None
    return STORES.for_home(ctx.services.workspace)


__all__ = [
    "ACP_META_OBSERVER",
    "CONCURRENCY_NOTICE",
    "MANIFEST_META_KEY",
    "PROJECT_FILES",
    "PROJECT_FILE_MAX_CHARS",
    "PROJECT_INSTRUCTIONS_HEAD",
    "CodeFlowHook",
    "SessionForget",
    "concurrency_notice",
    "make_flow_hook",
    "make_session_forget",
    "make_session_observer",
    "project_instructions",
]
