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
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from raven.acp_client import protocol
from raven.acp_client.client import AcpClient
from raven.acp_client.permissions import auto_approver
from raven.acp_client.protocol import SESSION_MCP_CAPABILITY, STEER_CAPABILITY, AcpError, AcpRemoteError
from raven.utils.atomic_io import atomic_update

_FILENAME = "subagent_acp_capabilities.json"

# Teardown budget. A killpg'd process group should be reaped at once; the bound
# exists so a wedged child can never hold a verify open indefinitely.
_CLOSE_TIMEOUT_S = 10.0

# The same four values as ``probe.ProbeStatus``, deliberately: the /subagents UI
# already renders that vocabulary, and a fifth status would mean teaching every
# surface a new word for a state it can already draw. Defined here rather than
# imported because probe.py reaches into backends, which reaches into the acp
# backend, which reaches back here.
SnapshotStatus = Literal["ready", "attention", "missing", "unknown"]

_LAUNCH_FIELDS = ("command", "cwd", "env", "ready_timeout_ms")

# Substrings that mark a remote error as "the operator has to authenticate",
# which is a different action from "this thing is not reachable". Matched on the
# message because ACP has no typed auth-required error: measured against
# `hermes acp`, an unusable provider credential surfaces as an ordinary failure
# whose text is the only signal.
_AUTH_HINTS = ("auth", "unauthor", "credential", "api key", "apikey", "login", "401")


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
    available_modes: tuple[AcpMode, ...] = ()
    """The profiles ``session/set_mode`` can switch a session between.

    Measured here rather than declared on the roster row so the menu the model
    picks from is the one the agent actually serves: a row that named its own
    would drift the first time the agent gained or dropped a mode."""
    auth_methods: tuple[str, ...] = ()
    elapsed_ms: int = 0
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
            "availableModes": [
                {"id": m.id, "name": m.name, "description": m.description} for m in self.available_modes
            ],
            "authMethods": list(self.auth_methods),
            "elapsedMs": self.elapsed_ms,
        }

    def to_row(self) -> dict[str, Any]:
        row = self.to_wire()
        row["fingerprint"] = self.fingerprint
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
            available_modes=_modes("availableModes"),
            auth_methods=_strs("authMethods"),
            elapsed_ms=int(row.get("elapsedMs") or 0),
        )


def snapshot_fingerprint(cfg: Any) -> str:
    """A digest of the fields that decide how this acp agent is launched.

    ``name``, ``description``, ``preset`` and ``enabled`` are deliberately absent
    for the reason ``probe_state.fingerprint`` gives: renaming an agent or
    switching it off and on does not change what it can do, so neither may
    discard a measurement that still holds.
    """
    payload = {name: getattr(cfg, name, None) for name in _LAUNCH_FIELDS}
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

    def forget(self, agent: str) -> None:
        def drop(current: str | None) -> tuple[str, None]:
            return self._dump([r for r in self._parse(current) if r.get("agent") != agent]), None

        try:
            atomic_update(self._path, drop)
        except OSError as exc:  # noqa: BLE001 - see record()
            logger.warning("acp capability snapshot delete failed: {}", exc)


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
    available_modes: tuple["AcpMode", ...] = ()
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


def relearn_session_modes(
    snapshot: CapabilitySnapshot | None,
    result: Any,
    *,
    store: SnapshotStore | None = None,
) -> CapabilitySnapshot | None:
    """Re-record an agent's modes from a live session response, if they moved.

    The menu is measured once at probe time and cached, but the agent
    re-advertises it on every route into a session (``_with_modes`` in the
    fork's ACP methods), so the response a dispatch already holds is newer
    evidence than the snapshot. Without this the cache only ever moves when
    someone presses Test: ``snapshot_fingerprint`` covers how the agent is
    LAUNCHED, and an agent that reworded its own modes launches identically, so
    nothing marks the stored text stale and the dispatching model goes on
    reading a menu the agent no longer serves.

    The stored fingerprint is kept rather than recomputed. Only the modes were
    re-measured here, and claiming a fresh handshake would clear a staleness
    flag the rest of the snapshot has not earned.

    ``None`` when there was nothing to learn, which includes a response carrying
    no modes at all. That is read as "this route did not report them", never as
    "the agent dropped them": erasing the menu takes the ``mode`` argument out
    of the spawn schema entirely, and a stale description is the smaller harm.
    """
    if snapshot is None:
        return None
    modes = _read_session_modes(result)
    if not modes or modes == snapshot.available_modes:
        return None
    updated = replace(snapshot, available_modes=modes)
    (store or SnapshotStore()).record(updated)
    logger.info(
        "acp agent {!r}: re-recorded {} mode(s) from a session response",
        snapshot.agent,
        len(modes),
    )
    return updated


def _looks_like_auth(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _AUTH_HINTS)


async def verify_agent(cfg: Any) -> CapabilitySnapshot:
    """Connect once, read what the agent reports, and disconnect. Never raises.

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

    def done(status: SnapshotStatus, detail: str, hs: _Handshake | None = None) -> CapabilitySnapshot:
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
            available_modes=hs.available_modes,
            auth_methods=hs.auth_methods,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    client: AcpClient | None = None
    try:
        client = await AcpClient.launch(
            name=name,
            command=getattr(cfg, "command", "") or "",
            cwd=getattr(cfg, "cwd", None),
            env=dict(getattr(cfg, "env", None) or {}),
            # The probe dispatches a real task, so it meets real permission
            # requests and has to answer them the way a dispatch would.
            on_request=auto_approver(name),
        )
        try:
            init = await client.request("initialize", protocol.initialize_params(), timeout=budget)
        except AcpRemoteError as exc:
            # The agent is there and talking, it just refused this handshake --
            # a version or shape mismatch, not an absent install. Reporting it
            # as "missing" would send the operator looking for the wrong problem.
            return done("unknown", f"connected, but rejected the ACP handshake: {exc.message} [{exc.code}]")
        except AcpError as exc:
            tail = client.stderr_tail(400)
            suffix = f"; stderr: {tail}" if tail else ""
            return done("missing", f"handshake failed: {exc}{suffix}")

        handshake = _read_initialize(init)
        if handshake.protocol_version != protocol.PROTOCOL_VERSION:
            handshake.warnings.append(
                f"agent speaks ACP v{handshake.protocol_version}, raven speaks v{protocol.PROTOCOL_VERSION}"
            )

        with tempfile.TemporaryDirectory(prefix="raven_acp_verify_") as tmp:
            try:
                session = await client.request("session/new", {"cwd": tmp, "mcpServers": []}, timeout=budget)
            except AcpRemoteError as exc:
                needs_auth = bool(handshake.auth_methods) or _looks_like_auth(exc.message)
                status: SnapshotStatus = "attention" if needs_auth else "unknown"
                hint = f" (auth methods: {', '.join(handshake.auth_methods)})" if handshake.auth_methods else ""
                return done(status, f"connected, but no session could be opened: {exc.message}{hint}", handshake)
            except AcpError as exc:
                tail = client.stderr_tail(400)
                suffix = f"; stderr: {tail}" if tail else ""
                return done("attention", f"connected, but session/new failed: {exc}{suffix}", handshake)

        handshake.available_models = _read_session_models(session)
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
            f"; models: {len(handshake.available_models)}"
            + (f"; modes: {', '.join(m.id for m in handshake.available_modes)}" if handshake.available_modes else "")
        )
        return done("ready", detail, handshake)
    except AcpError as exc:
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


__all__ = [
    "CapabilitySnapshot",
    "SnapshotStatus",
    "SnapshotStore",
    "default_snapshot_path",
    "relearn_session_modes",
    "snapshot_fingerprint",
    "steer_offered",
    "verify_agent",
]
