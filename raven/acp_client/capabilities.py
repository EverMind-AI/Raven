"""What an ACP agent says it can do, measured once and remembered.

This is the piece that makes an ``acp`` entry different in kind from a ``cli``
one. A cli entry's capabilities are seven fields a human typed, and the schema
can only catch them contradicting each other -- never being false. An acp entry
carries no such fields: raven connects, reads the ``initialize`` handshake, and
stores the answer.

The snapshot is what the roster reads, deliberately *not* a live handshake. A
roster that depended on a reachable process would let an agent vanish from the
model's options mid-session, and the model would then plan around a shrinking
list -- worse than a dispatch that fails with a clear error. This is the same
rule ``backends.enabled_third_party`` documents for ``enabled``.

Invalidation is by digest of the fields that decide how the agent is launched,
the mechanism :mod:`raven.agent.subagent.probe_state` already uses for test
verdicts: a stored snapshot whose digest no longer matches its config is treated
as absent rather than shown as current, which also covers a hand-edited
``config.json`` that no UI hook would see.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from raven.acp_client import npx_fetch, protocol, remote
from raven.acp_client.client import AcpClient
from raven.acp_client.permissions import auto_approver
from raven.acp_client.protocol import (
    MENU_UNREAD_META,
    SESSION_MCP_CAPABILITY,
    STEER_CAPABILITY,
    AcpConnectionError,
    AcpError,
    AcpRemoteError,
    reason_of,
)
from raven.home import subagent_role_env
from raven.ops import remote_path
from raven.utils.atomic_io import atomic_update
from raven.utils.commands import command_argv
from raven.utils.scratch import ScratchDir

_FILENAME = "subagent_acp_capabilities.json"

# Teardown budget. A close gives the agent its grace (at most five seconds, and
# none past the moment it exits), then ends the tree, so this bound only exists so
# a wedged child can never hold a verify open indefinitely.
_CLOSE_TIMEOUT_S = 10.0

# The same four values as ``probe.ProbeStatus``, deliberately: the /subagents UI
# already renders that vocabulary, and a fifth status would mean teaching every
# surface a new word for a state it can already draw. Defined here rather than
# imported because probe.py reaches into backends, which reaches into the acp
# backend, which reaches back here.
SnapshotStatus = Literal["ready", "attention", "missing", "unknown"]

_LAUNCH_FIELDS = ("command", "cwd", "env", "ready_timeout_ms")

#: Where an acp agent runs, when that is a registered machine rather than here.
#: Folded into launch digests only when set, so a digest recorded before these
#: fields existed still matches the row it was taken from.
REMOTE_LAUNCH_FIELDS = ("machine", "remote_cwd")

# Substrings that mark a remote error as "the operator has to authenticate",
# which is a different action from "this thing is not reachable". Matched on the
# message because ACP has no typed auth-required error: measured against
# `hermes acp`, an unusable provider credential surfaces as an ordinary failure
# whose text is the only signal.
_AUTH_HINTS = ("auth", "unauthor", "credential", "api key", "apikey", "login", "401")

_NOT_AUTH_HINTS = ("rate limit", "rate-limit", "429", "cooling down", "quota", "pip install")
"""Refusals that name a credential while saying it is not what is wrong.

An agent's remedy text mentions its credential commands whether or not a
credential is the problem, so a rate limit or a missing package reads as "auth"
to the hints above. All three of these are hermes's own words, from its source:
"Anthropic credentials are rate-limited for {model} ... (see `hermes auth
list`)", a credential "cooling down after a rate limit / quota error (429)", and
"Entra ID auth requires the 'azure-identity' package. Install it with: pip
install azure-identity". Hermes's comment on the first says it outright -- a
benched key "is not a missing credential; telling the user to re-authenticate
would send them chasing a cooldown that lifts on its own".
"""


@dataclass(frozen=True)
class AcpMode:
    """One operating profile an agent offers, from its session response.

    The spec's ``SessionMode``. Carried whole rather than reduced to an id
    because the description is what the dispatching model reads when it picks
    one -- an enum of bare ids ("fast", "deep") invites a choice made on the
    name, which is how a mode gets picked for a difference it does not have.
    """

    id: str
    name: str = ""
    description: str = ""


@dataclass(frozen=True)
class AcpModelChoice:
    """One model an agent offers, from the ``model`` entry of its config options.

    ``value`` is the opaque id the agent takes back; ``name`` is what it asked
    to be shown, which is usually far shorter (``claude-opus-5`` against
    ``openrouter/openrouter/anthropic/claude-opus-5``). ``group`` is the
    agent's own bucketing -- a provider, typically -- kept because a flat list
    of forty ids is not a menu anyone reads.

    Carried whole for the reason ``AcpMode`` is: reducing it to the id alone
    puts a choice in front of a reader on a spelling the agent never meant them
    to see.
    """

    value: str
    name: str = ""
    group: str = ""


EFFORT_MODE_IDS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "default", "none"})
"""Mode ids that name a reasoning effort, so a mode menu made only of them can be offered as effort."""


def mode_effort_choices(modes: tuple["AcpMode", ...]) -> tuple["AcpEffortChoice", ...]:
    """``modes`` as reasoning-effort choices, when every mode is an effort level.

    ACP has no field saying whether an agent's ``modes`` are reasoning levels or
    editor/permission modes; the names are the only signal. So this takes a menu
    as effort only when *every* id is a level name: one ``acceptEdits`` or
    ``plan`` makes it a mode menu, and offering a permission mode as a reasoning
    level is worse than offering no effort menu at all.
    """
    ids = [mode.id for mode in modes]
    if not ids or any(mode_id not in EFFORT_MODE_IDS for mode_id in ids):
        return ()
    return tuple(AcpEffortChoice(value=mode.id, name=mode.name or mode.id) for mode in modes if mode.id)


@dataclass(frozen=True)
class AcpEffortChoice:
    """One reasoning effort an agent offers, from its ``thought_level`` config option.

    ``value`` is what ``session/set_config_option`` takes back (``low``,
    ``high``, ``max``, ``default`` on OpenCode 1.19.4); ``name`` is what the
    agent asked to be shown, which is usually the same word. Carried whole for
    the reason :class:`AcpModelChoice` is: a menu of bare ids is not what the
    agent meant to put in front of a reader.
    """

    value: str
    name: str = ""


@dataclass(frozen=True)
class CapabilitySnapshot:
    """One handshake's worth of measured facts about an acp agent."""

    agent: str
    fingerprint: str
    status: SnapshotStatus
    detail: str
    measured_at_ms: int
    protocol_version: int = 0
    agent_name: str = ""
    agent_version: str = ""
    can_resume: bool = False
    can_fork: bool = False
    """Recorded but not yet used. Kept because it costs nothing to read here and
    a later fork feature would otherwise have to re-handshake every agent to
    learn something this connection already reported."""
    can_load: bool = False
    can_steer: bool = False
    """Whether the agent serves raven's ``_raven/session/steer`` extension,
    read from ``agentCapabilities._meta``. Decides whether text typed at an
    instance mid-turn can be merged into that turn or has to wait for it."""
    mcp_http: bool = False
    mcp_sse: bool = False
    session_mcp: bool = False
    """Whether the agent promises to honour ``mcpServers`` per session, read from
    ``agentCapabilities._meta``.

    Not the same question as ``mcp_http`` / ``mcp_sse``, which name transports.
    This one is about the field itself: a raven build that refuses it answers
    ``session/new`` with ``-32602`` while reporting exactly the ``mcpCapabilities``
    object a build that honours it reports, so no spec field separates them. Read
    only where that refusal is possible -- see ``AcpAgentBackend._session_mcp_refused``,
    which is also why a third-party agent leaving this false changes nothing."""
    prompt_modalities: tuple[str, ...] = ()
    available_models: tuple[str, ...] = ()
    """The ids ``models.availableModels`` advertises. Older material: the agents
    this host drives answer with ``configOptions`` instead, so this is usually
    empty and ``model_choices`` below is what a menu is built from."""
    model_choices: tuple[AcpModelChoice, ...] = ()
    """The models ``session/set_config_option`` can switch a session between.

    Measured rather than declared, for the reason ``available_modes`` gives:
    a row naming its own would drift the first time the agent's list changed."""
    effort_choices: tuple[AcpEffortChoice, ...] = ()
    """The reasoning efforts the agent's ``thought_level`` option offers.

    A separate config option from ``model`` (``session/set_config_option`` with
    the option's own id, ``effort`` on OpenCode 1.19.4), which is why the model
    list no longer carries effort-suffixed ids there. Measured like the other
    two menus, and empty for an agent that advertises none."""
    effort_option: str = ""
    """The config option id the effort values are set through (``effort``).

    Empty with ``effort_choices``: an agent with no effort menu has no id to
    send to, and the session's own response is what names it."""
    available_modes: tuple[AcpMode, ...] = ()
    """The profiles ``session/set_mode`` can switch a session between.

    Measured here rather than declared on the roster row so the menu the model
    picks from is the one the agent actually serves: a row that named its own
    would drift the first time the agent gained or dropped a mode."""
    auth_methods: tuple[str, ...] = ()
    needs_auth: bool = False
    """The agent answered, and then refused to open a session without a credential.

    Measured, not inferred. ``auth_methods`` alone cannot stand in for it: an
    agent that works advertises those too, so "not ready and has auth methods"
    is a guess. Deliberately outside ``usable``, which stays "ready and not
    stale" -- an agent waiting to be signed in is neither usable nor broken, and
    the surface that tells a reader which is which needs the distinction."""
    unfetched: bool = False
    """The handshake never happened because ``npx`` could not fetch the agent.

    Recorded apart from ``status`` for the reason ``needs_auth`` is: the status
    such a failure used to get was ``missing``, which the roster reads as an
    absent install and answers with the agent's own installer -- a command that
    fixes nothing when the network is what failed. See `npx_fetch_failure`."""
    elapsed_ms: int = 0
    model_menu_measured: bool = True
    """Whether ``model_choices`` was measured, or only defaulted at load.

    A row written before the menu was recorded reads back with an empty
    ``model_choices``, exactly like an agent measured to offer none; only the
    raw row tells the two apart (``SnapshotStore.has_model_menu``). Set at load
    time from the row's keys and never written as a field of its own: an
    unmeasured menu is written as the key's absence, so a verdict re-recorded
    over such a row does not turn "never measured" into "measured, none"."""
    stale: bool = False
    """Measured against a launch config this agent no longer has.

    Set at load time, never persisted -- it is a fact about the comparison, not
    about the handshake. What it changes is who may trust which field: the
    capabilities are still the best evidence available and are used, while the
    *status* is not, because a green light for a command that has since been
    edited is a claim no measurement backs."""

    @property
    def usable(self) -> bool:
        return self.status == "ready" and not self.stale

    def mode_efforts(self) -> tuple[AcpEffortChoice, ...]:
        """This agent's mode menu read as reasoning effort, or ``()`` when it is not one.

        A real ``thought_level`` option always wins; none is guessed for raven's
        own agents, whose modes are run-intensity tiers rather than a model's
        reasoning effort.
        """
        if self.effort_choices or self.agent_name == "raven":
            return ()
        return mode_effort_choices(self.available_modes)

    def to_wire(self) -> dict[str, Any]:
        """camelCase for the RPC layers, matching how ``ProbeResult`` does it."""
        return {
            "agent": self.agent,
            "status": self.status,
            "detail": self.detail,
            "measuredAtMs": self.measured_at_ms,
            "protocolVersion": self.protocol_version,
            "agentName": self.agent_name,
            "agentVersion": self.agent_version,
            "canResume": self.can_resume,
            "canFork": self.can_fork,
            "canLoad": self.can_load,
            "canSteer": self.can_steer,
            "mcpHttp": self.mcp_http,
            "mcpSse": self.mcp_sse,
            "sessionMcp": self.session_mcp,
            "promptModalities": list(self.prompt_modalities),
            "availableModels": list(self.available_models),
            "modelChoices": [{"value": c.value, "name": c.name, "group": c.group} for c in self.model_choices],
            "effortChoices": [{"value": c.value, "name": c.name} for c in self.effort_choices],
            "effortOption": self.effort_option,
            "availableModes": [
                {"id": m.id, "name": m.name, "description": m.description} for m in self.available_modes
            ],
            "authMethods": list(self.auth_methods),
            "needsAuth": self.needs_auth,
            "unfetched": self.unfetched,
            "elapsedMs": self.elapsed_ms,
        }

    def to_row(self) -> dict[str, Any]:
        row = self.to_wire()
        row["fingerprint"] = self.fingerprint
        if not self.model_menu_measured:
            del row["modelChoices"]
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "CapabilitySnapshot | None":
        """Rebuild from a stored row, or ``None`` if the row is not usable.

        Tolerant on purpose: a snapshot is a cache, so a row written by an older
        version should degrade to "not measured yet" rather than raise on load
        and take the page with it.
        """
        agent = row.get("agent")
        fingerprint = row.get("fingerprint")
        status = row.get("status")
        if not isinstance(agent, str) or not isinstance(fingerprint, str):
            return None
        if status not in ("ready", "attention", "missing", "unknown"):
            return None

        def _strs(key: str) -> tuple[str, ...]:
            value = row.get(key)
            return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()

        def _choices(key: str) -> tuple[AcpModelChoice, ...]:
            value = row.get(key)
            if not isinstance(value, list):
                return ()
            return tuple(
                AcpModelChoice(value=c["value"], name=str(c.get("name") or ""), group=str(c.get("group") or ""))
                for c in value
                if isinstance(c, dict) and isinstance(c.get("value"), str) and c["value"]
            )

        def _efforts(key: str) -> tuple[AcpEffortChoice, ...]:
            value = row.get(key)
            if not isinstance(value, list):
                return ()
            return tuple(
                AcpEffortChoice(value=c["value"], name=str(c.get("name") or ""))
                for c in value
                if isinstance(c, dict) and isinstance(c.get("value"), str) and c["value"]
            )

        def _modes(key: str) -> tuple[AcpMode, ...]:
            value = row.get(key)
            if not isinstance(value, list):
                return ()
            return tuple(
                AcpMode(id=m["id"], name=str(m.get("name") or ""), description=str(m.get("description") or ""))
                for m in value
                if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]
            )

        return cls(
            agent=agent,
            fingerprint=fingerprint,
            status=status,
            detail=str(row.get("detail") or ""),
            measured_at_ms=int(row.get("measuredAtMs") or 0),
            protocol_version=int(row.get("protocolVersion") or 0),
            agent_name=str(row.get("agentName") or ""),
            agent_version=str(row.get("agentVersion") or ""),
            can_resume=bool(row.get("canResume")),
            can_fork=bool(row.get("canFork")),
            can_load=bool(row.get("canLoad")),
            can_steer=bool(row.get("canSteer")),
            mcp_http=bool(row.get("mcpHttp")),
            mcp_sse=bool(row.get("mcpSse")),
            session_mcp=bool(row.get("sessionMcp")),
            prompt_modalities=_strs("promptModalities"),
            available_models=_strs("availableModels"),
            model_choices=_choices("modelChoices"),
            effort_choices=_efforts("effortChoices"),
            effort_option=str(row.get("effortOption") or ""),
            available_modes=_modes("availableModes"),
            auth_methods=_strs("authMethods"),
            needs_auth=bool(row.get("needsAuth")),
            unfetched=bool(row.get("unfetched")),
            elapsed_ms=int(row.get("elapsedMs") or 0),
            model_menu_measured="modelChoices" in row,
        )


def snapshot_fingerprint(cfg: Any) -> str:
    """A digest of the fields that decide how this acp agent is launched.

    ``name``, ``description``, ``preset`` and ``enabled`` are deliberately absent
    for the reason ``probe_state.fingerprint`` gives: renaming an agent or
    switching it off and on does not change what it can do, so neither may
    discard a measurement that still holds.
    """
    payload = {name: getattr(cfg, name, None) for name in _LAUNCH_FIELDS}
    # Only when set, so every snapshot measured before the field existed holds.
    if getattr(cfg, "lend_keys", None):
        payload["lend_keys"] = list(cfg.lend_keys)
    # Same rule: a row moved to another machine is another agent install.
    for name in REMOTE_LAUNCH_FIELDS:
        if getattr(cfg, name, None):
            payload[name] = getattr(cfg, name)
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def default_snapshot_path() -> Path:
    from raven.config.loader import get_config_path

    return get_config_path().parent / _FILENAME


class SnapshotStore:
    """One JSON file of capability snapshots, keyed by agent name.

    Stored as a list rather than a keyed object for the reason
    :mod:`raven.agent.subagent.instances` gives for the same choice: an agent
    name is an arbitrary user string, and a list needs no key escaping.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_snapshot_path()

    @staticmethod
    def _parse(text: str | None) -> list[dict[str, Any]]:
        if text is None:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return []
        rows = raw.get("snapshots") if isinstance(raw, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    def _read(self) -> list[dict[str, Any]]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return []
        return self._parse(text)

    @staticmethod
    def _dump(rows: list[dict[str, Any]]) -> str:
        return json.dumps({"version": 1, "snapshots": rows}, indent=2, ensure_ascii=False)

    def record(self, snapshot: CapabilitySnapshot) -> None:
        """Store one snapshot, replacing any previous one for the same agent."""

        # Filter-then-append is a read-modify-write, done inside the file lock
        # so two agents verified concurrently do not drop each other's row.
        def merge(current: str | None) -> tuple[str, None]:
            rows = [r for r in self._parse(current) if r.get("agent") != snapshot.agent]
            rows.append(snapshot.to_row())
            return self._dump(rows), None

        try:
            atomic_update(self._path, merge)
        except OSError as exc:  # a snapshot is a cache, never a dependency
            logger.warning("acp capability snapshot write failed (not remembered): {}", exc)

    def revise(self, snapshot: CapabilitySnapshot, **changes: Any) -> tuple[CapabilitySnapshot, bool] | None:
        """Apply ``changes`` to the stored record ``snapshot`` was read from.

        For a write that re-measured some fields of a record, so only those are
        written, onto the row as it stands inside the lock: another holder of
        the same record may have revised a different field since ``snapshot``
        was read, and writing the caller's whole copy back would undo it.

        Nothing is written when the stored row carries another measurement
        stamp than ``snapshot``. A Test or the boot backfill that recorded in
        the meantime is newer evidence about everything, the same "newest
        measurement wins" rule the boot backfill applies to its own late
        verdicts. With no stored row, ``snapshot`` itself is the base.

        Returns the record now stored and whether this call wrote it, so a
        caller that stood down holds the newer record and builds its next
        revision on that stamp rather than being refused again for as long as
        it lives. Flagged stale when it was measured under another launch
        config than ``snapshot``, or when ``snapshot`` already was: for the
        caller's launch it proves no more than that. ``None`` when the store
        could not be written, or holds only a row that cannot be read back.
        """

        def merge(current: str | None) -> tuple[str | None, tuple[CapabilitySnapshot, bool] | None]:
            rows = self._parse(current)
            stored = next((r for r in rows if r.get("agent") == snapshot.agent), None)
            if stored is not None and int(stored.get("measuredAtMs") or 0) != snapshot.measured_at_ms:
                newer = CapabilitySnapshot.from_row(stored)
                if newer is None:
                    return None, None
                return None, (replace(newer, stale=snapshot.stale or newer.fingerprint != snapshot.fingerprint), False)
            base = (CapabilitySnapshot.from_row(stored) if stored is not None else None) or snapshot
            updated = replace(base, stale=snapshot.stale, **changes)
            rows = [r for r in rows if r.get("agent") != snapshot.agent]
            rows.append(updated.to_row())
            return self._dump(rows), (updated, True)

        try:
            return atomic_update(self._path, merge)
        except OSError as exc:  # a snapshot is a cache, never a dependency
            logger.warning("acp capability snapshot write failed (not remembered): {}", exc)
            return None

    def load(self, configs: Sequence[Any], *, allow_stale: bool = False) -> dict[str, CapabilitySnapshot]:
        """Snapshots for these configs, keyed by agent name.

        A snapshot whose fingerprint no longer matches its config is skipped,
        which is the whole invalidation mechanism -- nothing has to delete it.

        ``allow_stale`` returns those too, flagged (see ``CapabilitySnapshot.stale``).
        For the capability reader, and the reason it exists: dropping a snapshot
        outright made it indistinguishable from one that was never taken, and the
        fallback for never-taken is the most damaging default available --
        *stateless*. Editing an agent's ``env`` therefore cost it its resume, its
        chip and the ``instance`` parameter in the spawn schema, silently, until
        someone happened to press Test. Trusting the old capabilities is the
        weaker claim and it self-heals: if the edit really did swap in an agent
        that cannot resume, the next ``session/load`` fails and the backend drops
        the binding and starts fresh.
        """
        rows = {row.get("agent"): row for row in self._read()}
        found: dict[str, CapabilitySnapshot] = {}
        for cfg in configs:
            name = getattr(cfg, "name", "") or ""
            row = rows.get(name)
            if row is None:
                continue
            snapshot = CapabilitySnapshot.from_row(row)
            if snapshot is None:
                continue
            if snapshot.fingerprint != snapshot_fingerprint(cfg):
                if not allow_stale:
                    continue
                snapshot = replace(snapshot, stale=True)
            found[name] = snapshot
        return found

    def has_model_menu(self, agent: str) -> bool:
        """Whether the stored row for ``agent`` carries a ``modelChoices`` key at all.

        Distinct from the field reading empty on a loaded :class:`CapabilitySnapshot`,
        which also happens for an agent genuinely measured to offer no menu
        (``from_row``'s ``_choices`` defaults a missing or malformed key to
        ``()`` either way) -- only the raw row can tell "never measured this"
        apart from "measured, and it has none". ``True`` when there is no
        stored row at all: that case is already the missing-snapshot branch of
        the auto-verify backfill, and this predicate must not itself demand a
        re-verify for a name nothing has recorded yet.
        """
        row = next((r for r in self._read() if r.get("agent") == agent), None)
        return row is None or "modelChoices" in row

    def forget(self, agent: str) -> None:
        def drop(current: str | None) -> tuple[str, None]:
            return self._dump([r for r in self._parse(current) if r.get("agent") != agent]), None

        try:
            atomic_update(self._path, drop)
        except OSError as exc:  # noqa: BLE001 - see record()
            logger.warning("acp capability snapshot delete failed: {}", exc)

    def restore(self, agent: str, previous: "CapabilitySnapshot | None", *, only_if_stamp: int) -> None:
        """Put ``previous`` back for ``agent``, or drop its row, unless a newer measurement landed.

        For a caller whose measurement was taken for a change that then
        refused: the row keeps the value it had, so the store has to describe
        that value again, or the next read draws the menu of a model the row is
        not on. ``previous`` of ``None`` means nothing was recorded before this
        call, so the row is removed rather than replaced.

        ``only_if_stamp`` is the measurement being undone. A Test or a refresh
        that recorded in the meantime is newer evidence about the same agent
        and is left to stand, the same rule :meth:`revise` applies. Best
        effort, like its siblings: a store that cannot be written is a cache
        that stays as it was, never an error for the caller.
        """

        def merge(current: str | None) -> tuple[str | None, None]:
            rows = self._parse(current)
            stored = next((r for r in rows if r.get("agent") == agent), None)
            if stored is None or int(stored.get("measuredAtMs") or 0) != only_if_stamp:
                return None, None
            rows = [r for r in rows if r.get("agent") != agent]
            if previous is not None:
                rows.append(previous.to_row())
            return self._dump(rows), None

        try:
            atomic_update(self._path, merge)
        except OSError as exc:  # noqa: BLE001 - see record()
            logger.warning("acp capability snapshot restore failed: {}", exc)


@dataclass
class _Handshake:
    """Fields pulled out of one connect, before a status is decided."""

    protocol_version: int = 0
    agent_name: str = ""
    agent_version: str = ""
    can_resume: bool = False
    can_fork: bool = False
    can_load: bool = False
    can_delete: bool = False
    can_steer: bool = False
    mcp_http: bool = False
    mcp_sse: bool = False
    session_mcp: bool = False
    prompt_modalities: tuple[str, ...] = ()
    auth_methods: tuple[str, ...] = ()
    available_models: tuple[str, ...] = ()
    model_choices: tuple["AcpModelChoice", ...] = ()
    effort_choices: tuple["AcpEffortChoice", ...] = ()
    effort_option: str = ""
    available_modes: tuple["AcpMode", ...] = ()
    menu_unread: bool = False
    """The agent answered ``session/new``, saying its menus are empty for want of a read."""
    warnings: list[str] = field(default_factory=list)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read_initialize(result: Any) -> _Handshake:
    payload = _dict(result)
    caps = _dict(payload.get("agentCapabilities"))
    session_caps = _dict(caps.get("sessionCapabilities"))
    prompt_caps = _dict(caps.get("promptCapabilities"))
    mcp_caps = _dict(caps.get("mcpCapabilities"))
    info = _dict(payload.get("agentInfo"))
    auth = payload.get("authMethods")
    auth_ids = (
        tuple(str(m.get("id")) for m in auth if isinstance(m, dict) and m.get("id") is not None)
        if isinstance(auth, list)
        else ()
    )
    # "text" is not advertised -- every ACP agent takes text, and promptCapabilities
    # lists only the extras (measured: hermes reports {"image": true}). Seeding it
    # keeps the roster from implying an agent cannot be sent a prompt.
    modalities = ("text",) + tuple(sorted(k for k, v in prompt_caps.items() if v is True))
    return _Handshake(
        protocol_version=int(payload.get("protocolVersion") or 0),
        agent_name=str(info.get("name") or ""),
        agent_version=str(info.get("version") or ""),
        can_resume="resume" in session_caps,
        can_fork="fork" in session_caps,
        can_load=bool(caps.get("loadSession")),
        can_delete="delete" in session_caps,
        # The one extension raven itself serves, announced in the schema's
        # extension carrier; a client that did not read it must not call it.
        can_steer=STEER_CAPABILITY in _dict(caps.get("_meta")),
        mcp_http=bool(mcp_caps.get("http")),
        mcp_sse=bool(mcp_caps.get("sse")),
        # The same carrier as the steer flag, and read the same way: presence,
        # not truthiness, because the schema spells "supported" as ``{}``.
        session_mcp=SESSION_MCP_CAPABILITY in _dict(caps.get("_meta")),
        prompt_modalities=modalities,
        auth_methods=auth_ids,
    )


def handshake_of(result: Any) -> _Handshake:
    """The parsed capabilities one ``initialize`` answer carries."""
    return _read_initialize(result)


def steer_offered(initialize: Any) -> bool:
    """Whether the agent that answered this ``initialize`` takes a steer.

    Read from the live handshake rather than the stored snapshot, for the reason
    the dialect is: the snapshot was measured on some earlier connect and may
    predate the agent learning the extension, while this is the process that
    is about to answer the prompt.
    """
    return _read_initialize(initialize).can_steer


def _read_session_models(result: Any) -> tuple[str, ...]:
    """The model ids a ``session/new`` result advertises.

    Shape measured against ``hermes acp``:
    ``models.availableModels[] = {modelId, name, description}``. Anything that
    does not match is read as "not reported" rather than guessed at -- an invented
    model id would be shown to the operator as fact. See ``_read_session_modes``
    for the sibling ``modes`` key, whose shape this docstring used to defer.
    """
    models = _dict(_dict(result).get("models")).get("availableModels")
    if not isinstance(models, list):
        return ()
    return tuple(str(m["modelId"]) for m in models if isinstance(m, dict) and isinstance(m.get("modelId"), str))


def _read_session_model_choices(result: Any) -> tuple[AcpModelChoice, ...]:
    """The models a ``session/new`` result offers, from its config options.

    The stable surface is ``configOptions[]`` with one entry carrying
    ``category: "model"`` -- NOT ``models.availableModels``, which is older
    material that the agents this host actually drives do not send. Reading the
    old key alone is how a survey of live handshakes came back empty while every
    one of them was advertising a full menu.

    The entry's ``options`` are either choices or groups of choices, and both
    shapes are taken: the agent decides whether its list is worth bucketing.

    ``currentValue`` is deliberately not recorded, for the reason
    ``_read_session_modes`` gives about ``currentModeId`` -- it is the state of
    the throwaway session this probe opened, not a fact about the agent, and
    storing it would advertise a default the next session need not start in.

    Same tolerance as the mode reader: an entry without a string ``value`` is
    dropped rather than guessed at, because an invented id is one a reader would
    be offered and the agent would then refuse.
    """
    options = _dict(result).get("configOptions")
    if not isinstance(options, list):
        return ()
    entry = next(
        (o for o in options if isinstance(o, dict) and o.get("category") == "model"),
        None,
    )
    if entry is None:
        return ()

    def choices(items: Any, group: str) -> list[AcpModelChoice]:
        if not isinstance(items, list):
            return []
        out: list[AcpModelChoice] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            nested = item.get("options")
            if isinstance(nested, list):
                out.extend(choices(nested, str(item.get("name") or item.get("group") or "")))
                continue
            value = item.get("value")
            if isinstance(value, str) and value:
                out.append(AcpModelChoice(value=value, name=str(item.get("name") or ""), group=group))
        return out

    return tuple(choices(entry.get("options"), ""))


def read_session_effort(result: Any) -> tuple[str, str, tuple[AcpEffortChoice, ...]]:
    """The session's reasoning-effort option: ``(option_id, current, choices)``.

    The option is the ``configOptions`` entry whose ``category`` is
    ``thought_level`` (OpenCode 1.19.4: id ``effort``, values ``low``, ``high``,
    ``max``, ``default``). It is a **separate** option from ``model``, which is
    why that agent's model list is the base models only: 1.14.31 packed the
    effort into ``model`` ids (``.../low``), and 1.19.4 moved it here.

    Empty everywhere when the agent advertises no such option, which is every
    agent measured before 1.19.4 and every one that has no reasoning effort to
    choose. ``current`` is the value this throwaway session opened on -- the
    agent's own default -- and is not stored on the capability snapshot for the
    reason :func:`read_session_current_model` gives; the session that opens for
    a real turn keeps it beside its id so a cleared override can be sent back.
    """
    options = _dict(result).get("configOptions")
    if not isinstance(options, list):
        return "", "", ()
    for entry in options:
        if not isinstance(entry, dict) or entry.get("category") != "thought_level":
            continue
        found: list[AcpEffortChoice] = []

        def walk(items: Any) -> None:
            for item in items if isinstance(items, list) else ():
                if not isinstance(item, dict):
                    continue
                nested = item.get("options")
                if isinstance(nested, list):
                    walk(nested)
                    continue
                value = item.get("value")
                if isinstance(value, str) and value:
                    found.append(AcpEffortChoice(value=value, name=str(item.get("name") or "")))

        walk(entry.get("options"))
        return str(entry.get("id") or ""), str(entry.get("currentValue") or ""), tuple(found)
    return "", "", ()


def read_session_current_mode(result: Any) -> str:
    """The session mode a response opened on, or ``""`` when it named none.

    Used only for a menu that is effort: clearing a row's effort sends this
    back, because ``session/set_mode`` takes a value and has no unset.
    """
    modes = _dict(_dict(result).get("modes"))
    value = modes.get("currentModeId")
    return value if isinstance(value, str) else ""


def read_session_current_model(result: Any) -> str:
    """The model a session response says that session is on right now.

    The sibling of :func:`_read_session_model_choices`, reading ``currentValue``
    off the same ``category: "model"`` entry -- and deliberately NOT stored on a
    capability snapshot, for the reason the choices reader gives: on a probe it
    is the state of a throwaway session rather than a fact about the agent.

    It IS a fact about the session that reported it, though, which is what makes
    it worth reading on a live route in: it is the model that session had before
    this host touched it, and therefore the only value a clear can restore. Empty
    when the agent reported none, which a caller must read as "cannot restore"
    rather than as a model.
    """
    options = _dict(result).get("configOptions")
    if not isinstance(options, list):
        return ""
    entry = next((o for o in options if isinstance(o, dict) and o.get("category") == "model"), None)
    current = _dict(entry).get("currentValue")
    return current if isinstance(current, str) else ""


def _read_session_menu_unread(result: Any) -> bool:
    """Whether this ``session/new`` said its menus are empty for want of a read.

    An agent whose catalog read failed answers with no ``configOptions`` and no
    ``modes`` -- byte for byte what an agent that genuinely offers neither
    answers. Raven's own extension tells the two apart: the adapter sets
    ``_meta.raven[MENU_UNREAD_META]`` when it could not read, and this reads it
    back, so the snapshot records "not measured" rather than "measured, none".

    The distinction is not cosmetic. Nothing invalidates a recorded
    measurement -- staleness asks whether the launch config moved, and a failed
    read moved nothing -- so an empty menu recorded as measured outlives every
    restart, and the row offers no model picker for as long as the file
    survives. An unmeasured menu is re-read at the next boot, which is what
    makes a cold start's timeout self-healing.

    Absent means measured: every agent that is not this adapter, and every
    adapter old enough not to send the key, is taken at its word.
    """
    return bool(_dict(_dict(_dict(result).get("_meta")).get("raven")).get(MENU_UNREAD_META))


def _read_session_modes(result: Any) -> tuple[AcpMode, ...]:
    """The operating profiles a ``session/new`` result advertises.

    The spec's ``SessionModeState``: ``modes.availableModes[] = {id, name,
    description}``, beside ``modes.currentModeId``. The current id is
    deliberately not recorded -- it is the state of the throwaway session this
    probe opened, not a fact about the agent, and storing it would advertise a
    default the next session need not start in.

    Same tolerance as the model reader: an entry without a string ``id`` is
    dropped rather than guessed at, because an invented id is one the
    dispatching model would be offered and the agent would then refuse.
    """
    modes = _dict(_dict(result).get("modes")).get("availableModes")
    if not isinstance(modes, list):
        return ()
    return tuple(
        AcpMode(id=m["id"], name=str(m.get("name") or ""), description=str(m.get("description") or ""))
        for m in modes
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]
    )


def relearn_session_menus(
    snapshot: CapabilitySnapshot | None,
    result: Any,
    *,
    store: SnapshotStore | None = None,
) -> CapabilitySnapshot | None:
    """Re-record an agent's modes and model menu from a live session response, if they moved.

    Both menus are measured once at probe time and cached, but the agent
    re-advertises them on its way into a session -- the modes on every route
    (``_with_modes`` in the fork's ACP methods), the models at least on
    ``session/new`` -- so the response a dispatch already holds is newer
    evidence than the snapshot. Without this the cache only ever moves when
    someone presses Test or asks for the menu again (``subagents.refresh_models``):
    ``snapshot_fingerprint`` covers how the agent is LAUNCHED, and an agent that
    reworded its own modes or renamed its models launches identically, so
    nothing marks the stored menus stale -- the dispatching model goes on reading
    modes the agent no longer serves, and the row's picker goes on offering model
    ids it no longer takes.

    Read with the probe's own parsers, from the response this route was sending
    for anyway: nothing here opens a session or starts a process.

    The stored fingerprint and measurement stamp are kept rather than renewed.
    Only the menus were re-measured here, and claiming a fresh handshake would
    clear a staleness flag the rest of the snapshot has not earned. Only the
    menus this response reported are written, and none when a newer
    measurement has replaced the record ``snapshot`` was read from
    (``SnapshotStore.revise``). That newer record is returned instead, unrevised:
    this response may predate it, so its menus are not taken over it, but the
    caller's next response is then judged against the stamp actually stored.

    ``None`` when there was nothing to learn, which includes a response carrying
    neither menu. Each menu is judged on its own, and an absent or empty one is
    read as "this route did not report it", never as "the agent dropped it":
    erasing the modes takes the ``mode`` argument out of the spawn schema, and
    erasing the models takes the picker off the row, both worse than a stale
    entry. A model menu the agent flagged as unread is not taken either, for
    the reason ``_read_session_menu_unread`` gives.

    The reasoning-effort menu is deliberately not among them. It belongs to the
    model the session is on -- OpenCode 1.19.4 offers one only for a model that
    reasons -- so a session's menu is a fact about that session, and the row's
    is the probe's, measured on the row's own model. Taking a session's over it
    made the agent-wide record swing with every model switch, each swing a
    rebuild of the whole agent table.
    """
    if snapshot is None:
        return None
    changes: dict[str, Any] = {}
    modes = _read_session_modes(result)
    if modes and modes != snapshot.available_modes:
        changes["available_modes"] = modes
    choices = () if _read_session_menu_unread(result) else _read_session_model_choices(result)
    if choices and choices != snapshot.model_choices:
        changes["model_choices"] = choices
        changes["model_menu_measured"] = True
    if not changes:
        return None
    revised = (store or SnapshotStore()).revise(snapshot, **changes)
    if revised is None:
        return None
    updated, written = revised
    if not written:
        logger.info(
            "acp agent {!r}: session menus not re-recorded over a newer measurement; building on that one next",
            snapshot.agent,
        )
        return updated
    logger.info(
        "acp agent {!r}: re-recorded {} mode(s) and {} model(s) from a session response",
        snapshot.agent,
        len(updated.available_modes),
        len(updated.model_choices),
    )
    return updated


def looks_like_auth(text: str) -> bool:
    """Whether a refusal reads as one about a credential.

    Public because the connect path asks it too. It was private while this
    module was its only reader, and a second reader copying the hint list
    would be two spellings of one rule -- which is how the roster and the
    connect button came to disagree about the same failure in the first place.
    """
    lowered = text.lower()
    if any(hint in lowered for hint in _NOT_AUTH_HINTS):
        return False
    return any(hint in lowered for hint in _AUTH_HINTS)


# npm's own report of a failed fetch, as npm prints it before exiting: the code
# on a line of its own, then (for a network failure) lines headed "network".
# `npm error` is npm 10, `npm ERR!` npm 9 and older; both measured 2026-09-23
# against an unreachable and an unresolvable registry, npm 10.9.8 and 9.9.4.
_NPM_CODE = re.compile(r"^npm (?:ERR!|error) code (\S+)", re.MULTILINE)
_NPM_NETWORK = re.compile(r"^npm (?:ERR!|error) network ", re.MULTILINE)
# The codes that mean the registry could not be reached or would not serve the
# package: the network, TLS in front of it, or the registry's own answer (the
# E<status> codes, and ETARGET for a mirror that lacks the pinned version). A
# local failure -- EACCES on the cache, ENOSPC -- is deliberately not here: no
# network, registry or proxy setting fixes it, and that is what this verdict
# tells its reader to look at.
_NPM_FETCH_CODES = frozenset(
    {
        "ECONNREFUSED",
        "ECONNRESET",
        "ECONNABORTED",
        "ENOTFOUND",
        "EAI_AGAIN",
        "ETIMEDOUT",
        "EHOSTUNREACH",
        "ENETUNREACH",
        "ENETDOWN",
        "EPROTO",
        "ERR_SOCKET_TIMEOUT",
        "ETARGET",
        "EINTEGRITY",
        "CERT_HAS_EXPIRED",
        "SELF_SIGNED_CERT_IN_CHAIN",
        "DEPTH_ZERO_SELF_SIGNED_CERT",
        "UNABLE_TO_GET_ISSUER_CERT",
        "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
        "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
        "ERR_TLS_CERT_ALTNAME_INVALID",
    }
)
_NPM_STATUS_CODE = re.compile(r"E\d{3}")


def launches_with_npx(command: str) -> bool:
    """Whether ``command`` is run by ``npx``, which fetches what it names on first use."""
    try:
        argv = command_argv(command or "")
    except ValueError:
        return False
    if not argv:
        return False
    exe = os.path.basename(argv[0]).lower()
    return exe in ("npx", "npx.cmd", "npx.exe")


def npx_fetch_failure(command: str, exc: BaseException, stderr: str | None = None) -> str | None:
    """Why ``npx`` never got to run what ``command`` names, or ``None`` if that is not what happened.

    The npm error code when the fetch failed, ``"timeout"`` when the start ran
    out of time while npx still had it, ``None`` for everything else.

    Needed because both look like something else from above. A fetch that
    fails is an npx that exits before any handshake -- the fetch run ahead of
    the launch (`npx_fetch`), or the launch itself when no fetch could run --
    so it arrives as an exit with npm's stack trace under it; and npm spends
    about 70s retrying an unreachable registry before it says so (measured),
    which is past a budget that did not allow for it -- so what the reader saw
    was "it did not answer within 60s", about an agent that never started.
    Named for what it is, the reader can be told to look at the network, the
    npm registry or the proxy, and not at the agent.

    Evidence rather than inference, in the way `looks_like_auth` is not: the
    verdict needs npm's own error-code line, read from the whole stderr
    (``AcpConnectionError.stderr``, or ``stderr`` for a caller that holds the
    client) because npm prints that line first. The timeout is the one reading
    without such a line, and it is kept to an ``initialize`` that ran out on an
    ``npx`` command whose package was not fetched ahead of the handshake
    (`npx_fetch.fetchable_package`) -- an agent that has not finished starting,
    where a first download is the likeliest cause and the one the reader can act
    on. One that was fetched ahead had its download over before the handshake
    began, so running out there is the agent's own start. Every caller reads a
    launch on this computer, where that fetch runs: an agent on a registered
    machine, whose Raven node starts it with no fetch ahead, has its failure
    reported as the machine gave it before any of them is reached
    (`verify_agent`, `probe.ping_agent`).
    """
    if not launches_with_npx(command):
        return None
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, protocol.AcpTimeoutError) and current.method == "initialize":
            return None if npx_fetch.fetchable_package(command_argv(command)) else "timeout"
        if isinstance(current, protocol.AcpConnectionError) and current.stderr:
            stderr = stderr or current.stderr
        current = current.__cause__
    if not stderr:
        return None
    for match in _NPM_CODE.finditer(stderr):
        code = match.group(1)
        if code in _NPM_FETCH_CODES or _NPM_STATUS_CODE.fullmatch(code):
            return code
    if _NPM_NETWORK.search(stderr):
        return "network"
    return None


def npx_fetch_lead(verdict: str) -> str:
    """The sentence `npx_fetch_failure`'s verdict is reported with, for the log, the CLI and the TUI."""
    if verdict == "timeout":
        return "it did not finish starting in time, and npx may still have been downloading it"
    return f"npx could not download it ({verdict}); check the network, the npm registry or the proxy"


def npx_incomplete_lead(entry: npx_fetch.IncompleteEntry) -> str:
    """The sentence an npx cache entry left without its package.json is reported with, and its fix."""
    return (
        "npx's copy of it is incomplete -- a download of it was cut off part-way -- and npx cannot start it "
        f"until that copy ({entry.directory}) is removed; run `{entry.clear_command}` and connect again"
    )


class _NotThereError(Exception):
    """A machine an agent cannot be verified on, in one sentence."""


@dataclass(frozen=True)
class _OnMachine:
    """The link to a machine, with the agent's command confirmed to be there."""

    link: Any
    target: remote.Machine


async def _reach(machine_id: str, command: str, env: dict[str, str]) -> _OnMachine:
    """The machine's node, after it has found ``command`` there; :class:`_NotThereError` says why not.

    One round trip on the link (``probe/agent``) before the real handshake,
    so "not installed there" is told apart from "installed and broken"
    without starting anything.
    """
    try:
        target = await asyncio.to_thread(remote.machine, machine_id)
    except remote.RemoteMachineError as exc:
        raise _NotThereError(str(exc)) from None
    link = remote_path.nodes()
    if link is None:
        raise _NotThereError(f"machine {target.label}: this process does not reach registered machines")
    try:
        probed = await link.probe_agent(machine_id, command, env)
    except remote_path.NodeCallError as exc:
        raise _NotThereError(str(exc)) from None
    if not probed.get("found"):
        raise _NotThereError(remote.agent_missing(target))
    return _OnMachine(link, target)


async def verify_agent(cfg: Any, *, env: dict[str, str] | None = None) -> CapabilitySnapshot:
    """Connect once, read what the agent reports, and disconnect. Never raises.

    ``env`` is the environment the agent is started with, when the caller's is
    more than the row's own ``env`` -- the keys a row borrows from Raven, which
    the spawn path adds and this transport has no business resolving.

    Two round trips, both needed: ``initialize`` carries the capabilities and auth
    methods, and ``session/new`` is the only place the model list appears. Doing
    the second also proves the agent can actually open a session, which is the
    difference between "the executable exists" and "this agent is usable" -- the
    gap the cli transport's ``shutil.which`` probe can never close.

    The session is opened in a throwaway directory so verifying does not leave
    session state in the user's workspace, and is never prompted, so verifying
    costs no tokens.
    """
    name = getattr(cfg, "name", "") or ""
    started = time.monotonic()
    fingerprint = snapshot_fingerprint(cfg)
    budget = max(1.0, (getattr(cfg, "ready_timeout_ms", None) or 30000) / 1000)

    def done(
        status: SnapshotStatus,
        detail: str,
        hs: _Handshake | None = None,
        *,
        needs_auth: bool = False,
        unfetched: bool = False,
    ) -> CapabilitySnapshot:
        hs = hs or _Handshake()
        full = "; ".join([detail, *hs.warnings]) if hs.warnings else detail
        return CapabilitySnapshot(
            agent=name,
            fingerprint=fingerprint,
            status=status,
            detail=full,
            measured_at_ms=int(time.time() * 1000),
            protocol_version=hs.protocol_version,
            agent_name=hs.agent_name,
            agent_version=hs.agent_version,
            can_resume=hs.can_resume,
            can_fork=hs.can_fork,
            can_load=hs.can_load,
            can_steer=hs.can_steer,
            mcp_http=hs.mcp_http,
            mcp_sse=hs.mcp_sse,
            session_mcp=hs.session_mcp,
            prompt_modalities=hs.prompt_modalities,
            available_models=hs.available_models,
            model_choices=hs.model_choices,
            effort_choices=hs.effort_choices,
            effort_option=hs.effort_option,
            model_menu_measured=not hs.menu_unread,
            available_modes=hs.available_modes,
            auth_methods=hs.auth_methods,
            needs_auth=needs_auth,
            unfetched=unfetched,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    agent_env = dict(env if env is not None else (getattr(cfg, "env", None) or {}))
    command = getattr(cfg, "command", "") or ""
    machine_id = (getattr(cfg, "machine", None) or "").strip()
    there: _OnMachine | None = None
    if machine_id:
        # An agent on a registered machine is verified where it runs, through
        # the machine's Raven node as a dispatch starts it, after the node has
        # said the command is there. Every failure there is "attention", never
        # "missing": the roster answers "missing" with an installer, and that
        # installer would run on this computer.
        try:
            there = await _reach(machine_id, command, agent_env)
        except _NotThereError as exc:
            return done("attention", str(exc))
    client: AcpClient | None = None
    scratch: ScratchDir | None = None
    try:
        # Nothing here is prompted, so no permission request is expected. One
        # that arrives anyway still has to be answered, or the agent waits for
        # a reply that never comes and the handshake stalls behind its own
        # timeout.
        if there is not None:
            try:
                endpoint = await there.link.open_channel(
                    machine_id,
                    name=name,
                    command=command,
                    cwd=remote.root_of(getattr(cfg, "remote_cwd", None)),
                    env={**subagent_role_env(), **agent_env},
                )
            except remote_path.NodeCallError as exc:
                return done("attention", str(exc))
            client = AcpClient.over(endpoint, name=name, on_request=auto_approver(name))
        else:
            try:
                client = await AcpClient.launch(
                    name=name,
                    command=command,
                    cwd=getattr(cfg, "cwd", None),
                    env=agent_env,
                    on_request=auto_approver(name),
                )
            except AcpConnectionError as exc:
                # The npx fetch ahead of the agent fails here, before any handshake,
                # and is read the way a handshake that died under npx is read below:
                # neither is an install the roster's installer would bring.
                if (entry := npx_fetch.incomplete_entry(str(exc))) is not None:
                    return done("attention", f"{npx_incomplete_lead(entry)}; {exc}")
                if (unfetched := npx_fetch_failure(command, exc)) is not None:
                    return done("attention", f"{npx_fetch_lead(unfetched)}; {exc}", unfetched=True)
                raise
        try:
            init = await client.request("initialize", protocol.initialize_params(), timeout=budget)
        except AcpRemoteError as exc:
            # The agent is there and talking, it just refused this handshake --
            # a version or shape mismatch, not an absent install. Reporting it
            # as "missing" would send the operator looking for the wrong problem.
            return done("unknown", f"connected, but rejected the ACP handshake: {reason_of(exc)} [{exc.code}]")
        except AcpError as exc:
            tail = client.stderr_tail(400)
            suffix = f"; stderr: {tail}" if tail else ""
            if there is not None:
                # In the far side's own words (a full disk, a crash), never
                # "missing": whatever is absent is absent there.
                return done("attention", f"handshake failed: {exc}{suffix}")
            unfetched = npx_fetch_failure(getattr(cfg, "command", "") or "", exc, client.stderr_tail() or None)
            if unfetched is not None:
                # Not `missing`: nothing is absent that an install would bring, and
                # the roster answers `missing` with the agent's own installer.
                return done(
                    "attention", f"{npx_fetch_lead(unfetched)}; handshake failed: {exc}{suffix}", unfetched=True
                )
            return done("missing", f"handshake failed: {exc}{suffix}")

        handshake = _read_initialize(init)
        if handshake.protocol_version != protocol.PROTOCOL_VERSION:
            handshake.warnings.append(
                f"agent speaks ACP v{handshake.protocol_version}, raven speaks v{protocol.PROTOCOL_VERSION}"
            )

        if there is None:
            # Removed in the `finally`, after the close: an agent may start its
            # worker in the session's directory (Claude Code does), and Windows
            # will not remove one a live process works in.
            scratch = ScratchDir("raven_acp_verify_")
            tmp = str(scratch.path)
        else:
            # On the machine, the one directory every check reuses: a local
            # temp dir does not exist where the agent runs, and it refuses one.
            try:
                tmp, _since = await there.link.session_dir(
                    machine_id,
                    remote.session_root(getattr(cfg, "remote_cwd", None), remote.CHECK_HANDLE),
                )
            except remote_path.NodeCallError as exc:
                return done("attention", str(exc), handshake)
        try:
            session = await client.request("session/new", {"cwd": tmp, "mcpServers": []}, timeout=budget)
        except AcpRemoteError as exc:
            # Two readings of one refusal, and they do not take the same
            # evidence. The advertisement is enough for the coarse status --
            # it only decides whether a reader should look at this row -- and
            # it is not enough for `needs_auth`, which is persisted, rendered
            # as a disabled control and read as "go and sign in". Every agent
            # that works advertises auth methods too, so taking the
            # advertisement there would label any unrelated session failure,
            # a transient one included, as a credential story with no way out.
            advertised = bool(handshake.auth_methods)
            # The agent's whole answer, `data` included: the Connect button
            # classifies this same text (`reason_of`), and two readers given
            # different halves of one refusal are how they came to disagree.
            refused_over_a_credential = looks_like_auth(reason_of(exc))
            needs_auth = refused_over_a_credential
            status: SnapshotStatus = "attention" if advertised or refused_over_a_credential else "unknown"
            hint = f" (auth methods: {', '.join(handshake.auth_methods)})" if handshake.auth_methods else ""
            return done(
                status,
                f"connected, but no session could be opened: {reason_of(exc)}{hint}",
                handshake,
                needs_auth=needs_auth,
            )
        except AcpError as exc:
            tail = client.stderr_tail(400)
            suffix = f"; stderr: {tail}" if tail else ""
            return done("attention", f"connected, but session/new failed: {exc}{suffix}", handshake)

        # The effort menu can be model-specific: OpenCode 1.19.4 advertises a
        # thought_level option for a model that reasons, and a fresh session
        # opens on the agent's own default. Put the row's model on the
        # throwaway session first, so a row pinned to a reasoning model records
        # the menu that model has; the set response is the one carrying it.
        wanted_model = getattr(cfg, "model", None)
        session_id = _dict(session).get("sessionId")
        selected_effort: tuple[str, tuple[AcpEffortChoice, ...]] | None = None
        if isinstance(wanted_model, str) and wanted_model and isinstance(session_id, str) and session_id:
            try:
                switched = await client.request(
                    "session/set_config_option",
                    {"sessionId": session_id, "configId": "model", "value": wanted_model},
                    timeout=budget,
                )
            except AcpError as exc:
                logger.debug(
                    "acp verify for {!r}: setting {!r} for its effort menu failed: {}", name, wanted_model, exc
                )
            else:
                if isinstance(switched, dict):
                    option, _effort_now, efforts = read_session_effort(switched)
                    # A response that lists the options the model now has, with
                    # no thought_level among them, says the model takes no
                    # effort; keeping the default model's menu would offer rungs
                    # every turn on this one drops. An empty or absent list says
                    # nothing either way.
                    if efforts or switched.get("configOptions"):
                        selected_effort = (option, efforts)

        handshake.available_models = _read_session_models(session)
        handshake.model_choices = _read_session_model_choices(session)
        handshake.effort_option, _effort_now, handshake.effort_choices = read_session_effort(session)
        if selected_effort is not None:
            # The row's model, not the session's default, is what the effort
            # menu belongs to -- the set response is authoritative for it.
            handshake.effort_option, handshake.effort_choices = selected_effort
        handshake.menu_unread = _read_session_menu_unread(session)
        handshake.available_modes = _read_session_modes(session)
        caps = ", ".join(
            [
                *(["resume"] if handshake.can_resume else []),
                *(["fork"] if handshake.can_fork else []),
                *(["load"] if handshake.can_load else []),
            ]
        )
        label = f"{handshake.agent_name or name} {handshake.agent_version}".strip()
        detail = (
            f"connected to {label} over ACP v{handshake.protocol_version}"
            f"; sessions: {caps or 'one-shot only'}"
            f"; models: {len(handshake.model_choices) or len(handshake.available_models)}"
            + (f"; modes: {', '.join(m.id for m in handshake.available_modes)}" if handshake.available_modes else "")
        )
        return done("ready", detail, handshake)
    except AcpError as exc:
        if there is not None:
            return done("attention", str(exc))
        return done("missing", str(exc))
    except Exception as exc:  # noqa: BLE001 - a raising verify would blank the page
        logger.opt(exception=True).warning("acp verify for {!r} failed unexpectedly: {}", name, exc)
        return done("unknown", f"verify failed: {exc}")
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), timeout=_CLOSE_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 - teardown must never mask the verdict
                logger.debug("acp verify: closing {!r} did not finish cleanly: {}", name, exc)
        if scratch is not None:
            await scratch.remove()


__all__ = [
    "AcpEffortChoice",
    "CapabilitySnapshot",
    "EFFORT_MODE_IDS",
    "mode_effort_choices",
    "SnapshotStatus",
    "SnapshotStore",
    "default_snapshot_path",
    "launches_with_npx",
    "looks_like_auth",
    "npx_fetch_failure",
    "npx_fetch_lead",
    "npx_incomplete_lead",
    "read_session_current_mode",
    "read_session_effort",
    "relearn_session_menus",
    "snapshot_fingerprint",
    "steer_offered",
    "verify_agent",
]
