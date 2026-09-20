"""The code flow's turn participant.

Repository instructions and workspace concurrency notices join the system
message through the ``system_addendum`` verb. They never rewrite the inbound
query. The participant tracks the turn in the session ledger,
binds and restores its checklist, and files the workspace report at send.
Session deletion discards product state; new session IDs isolate new tasks.
One participant per turn: the checklist binding and the per-directory instruction
cache are attributes that die with the turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import ValidationError

from code_flow.config import FlowConfig
from code_flow.manifest import build_manifest
from code_flow.sessions import LEDGER, SessionLedger
from code_flow.system_context import sized_addendum
from code_flow.tools.read_state import Owner, ReadSessions, forget_session, owner_for
from code_flow.tools.todo import STORES, TodoStore
from raven.agent import workdir
from raven.agent.hook.participant import ParticipantHook
from raven.contracts.participant import AgentParticipant, Answer, StepView

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
    in it, the same footing the participant stands on, and a data fence would tell
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


class CodeParticipant(AgentParticipant):
    """The code flow's judgements and bookkeeping, one instance per turn.

    Process-level dependencies (the ledger, the checklist store, the slice's
    file names) arrive through ``__init__``; what used to ride the hook
    context's metadata dict -- the turn's checklist binding, the per-directory
    instruction cache, the pending restore note the sizing must count -- is an
    attribute here and dies with the turn.
    """

    def __init__(
        self,
        ledger: SessionLedger,
        project_files: list[str],
        todos: TodoStore | None,
        flow_enabled: bool,
        reads: ReadSessions | None,
    ) -> None:
        self._ledger = ledger
        self._project_files = project_files
        # The checklist store this product's ``todo`` tool writes through, or
        # None when the tool face is not served (nothing to bind or restore).
        self._todos = todos
        self._flow_enabled = flow_enabled
        self._reads = reads
        self._todo_binding: tuple[str, str | None] | None = None
        self._instructions: tuple[Any, str] | None = None
        self._pending_note: str | None = None

    async def intake(self, text: str, step: StepView) -> Answer | None:
        # Commands return before iteration or after_send, so they must not
        # create an in-flight mark for a turn that will never report back.
        if (text or "").strip().lower() in {"/new", "/help"}:
            return None
        cwd = workdir.current()
        if self._flow_enabled:
            self._ledger.begin_turn(step.session_key, cwd)
        if self._todos is not None:
            try:
                self._todos.bind(step.session_key, cwd)
                self._todo_binding = (step.session_key, str(cwd) if cwd is not None else None)
            except Exception:  # noqa: BLE001 - a record problem must not cost the turn
                logger.exception("code-flow: could not bind the checklist for {}", step.session_key)
        return None

    async def advise(self, step: StepView) -> str | None:
        """The checklist, restored into context when the window elided it."""
        if step.response is not None:
            # Asked before the call and after it; everything here belongs before.
            return None
        if self._reads is not None:
            self._reads.bind(step.session_key)
        if self._flow_enabled:
            self._ledger.touch(step.session_key)
        note: str | None = None
        if self._todos is not None:
            # System turns skip inbound, and the loop can select another
            # session after inbound. The turn's binding survives rollbacks
            # because the participant does.
            cwd = workdir.current()
            binding = (step.session_key, str(cwd) if cwd is not None else None)
            try:
                if self._todo_binding != binding or not self._todos.is_bound:
                    self._todos.bind(*binding)
                    self._todo_binding = binding
                snapshot = self._todos.snapshot_if_hidden(list(step.transcript))
                if snapshot is not None:
                    logger.info("code-flow: restoring the checklist into context for {}", step.session_key)
                    note = snapshot
            except Exception:  # noqa: BLE001 - a checklist failure must not discard repository instructions
                logger.exception("code-flow: could not restore the checklist for {}", step.session_key)
        # What the sizing below must count: the note lands on the prompt in
        # the same call the addendum does.
        self._pending_note = note
        return note

    async def system_addendum(self, step: StepView) -> Answer | None:
        if not self._flow_enabled:
            return None
        cwd = workdir.current()
        if self._instructions is None or self._instructions[0] != cwd:
            self._instructions = (cwd, project_instructions(cwd, self._project_files))
        peers = self._ledger.peers_in_flight(step.session_key)
        return sized_addendum(
            step.transcript,
            step.tools,
            self._instructions[1],
            concurrency_notice(peers) if peers else "",
            window=step.window,
            pending_note=self._pending_note,
        )

    async def archive(self, step: StepView, reply: str | None) -> dict[str, dict[str, Any]] | None:
        if self._reads is not None:
            self._reads.unbind()
        if not self._flow_enabled:
            return None
        record = self._ledger.record(step.session_key)
        cwd = workdir.current()
        if cwd is None and record is not None and record.cwd:
            cwd = Path(record.cwd)
        report = build_manifest(
            step.session_key,
            cwd,
            record.base_commit if record is not None else None,
            shared_with=len(record.peers) if record is not None else 0,
        )
        self._ledger.end_turn(step.session_key)
        return {ACP_META_OBSERVER: {MANIFEST_META_KEY: report}}


class CodeFlowHook(ParticipantHook):
    """The code flow's seat in the hook chain: one participant per turn.

    Kept as a named class rather than a bare ``ParticipantHook`` because the
    constructor is the product's assembly surface -- the factory below and a
    ledger's worth of tests wire sessions through it -- and because
    ``isinstance`` is how the factory's admission is asserted.
    """

    def __init__(
        self,
        ledger: SessionLedger | None = None,
        project_files: list[str] | None = None,
        todos: TodoStore | None = None,
        *,
        flow_enabled: bool = True,
        reads: ReadSessions | None = None,
    ) -> None:
        seated_ledger = ledger if ledger is not None else LEDGER
        names = list(project_files or [])
        super().__init__(
            "code_flow",
            lambda: CodeParticipant(seated_ledger, names, todos, flow_enabled, reads),
            # This participant does not review, so no verdict of its can send a turn
            # back, and the loop should stream the reply rather than hold it.
            rolls_back=False,
        )


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
    "CodeParticipant",
    "CodeFlowHook",
    "SessionForget",
    "concurrency_notice",
    "make_flow_hook",
    "make_session_forget",
    "make_session_observer",
    "project_instructions",
]
