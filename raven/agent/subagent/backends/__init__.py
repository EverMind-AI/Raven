"""Subagent execution backends.

A spawned sub-agent's *execution* is pluggable: by default it runs an in-process
Raven agent loop (:class:`RavenLoopBackend`); third-party backends (CLI agents
like claude code / codex via :class:`CliAgentBackend`, or OpenAI-compatible HTTP
agents like mirothinker via :class:`OpenAIApiBackend`) plug in behind the same
:class:`SubagentBackend` protocol. The manager's spawn concurrency / rate-limit /
result re-injection stay backend-agnostic.
"""

from collections.abc import Sequence
from typing import Any, NamedTuple

from loguru import logger

from raven.agent.subagent.backends.acp_agent import AcpAgentBackend
from raven.agent.subagent.backends.base import (
    ABORTED_ACTION_RESULT,
    IN_SUBAGENT_RUN,
    SubagentActionAbortedError,
    SubagentBackend,
)
from raven.agent.subagent.backends.cli_agent import CliAgentBackend
from raven.agent.subagent.backends.openai_api import OpenAIApiBackend
from raven.agent.subagent.backends.raven_loop import RavenLoopBackend, build_subagent_prompt
from raven.agent.subagent.presets import session_mcp_for


class AgentMeta(NamedTuple):
    """One third-party sub-agent as the prompt surfaces advertise it.

    Named rather than a bare tuple because every field here is a *choice the
    model has to make correctly* — which agent, whether an ``instance`` handle
    can carry context, whether a path may be handed over — and positional
    unpacking silently drops the ones a renderer forgets.
    """

    name: str
    description: str
    stateful: bool
    reads_local_files: bool
    live_progress: bool = False
    """Whether this agent reports its work while it runs.

    Advertised because the absence is otherwise indistinguishable from a quiet
    run: a cli agent's work is read out of its transcript after it exits, so an
    empty timeline is a property of the transport rather than a hung task.
    Defaulted so a duck-typed construction cannot claim a capability the
    transport lacks.

    Distinct from ``SubagentBackend.streams``, which a cli agent can have: that
    one is about the reply reaching a human as it forms, and the model is never
    told about it. This one is about the *work* being reportable at all, which
    is what a caller planning around a long-running agent needs to know."""

    owns: str = ""
    """What kind of work this agent owns, or ``""`` when it claims none.

    Unlike the three capabilities above this one is not rendered into the tool
    descriptions: it drives the identity prompt's Delegation section instead, so
    the rule about who does what is stated once, where the model reads it before
    choosing a tool rather than after. That section is conditional on the turn
    holding a tool to delegate *with* -- see ``render._delegation_block`` -- so a
    declaration here is what makes the rule available, not what makes it appear.
    """

    modes: tuple[Any, ...] = ()
    """The operating profiles this agent offers, or ``()`` when it offers none.

    ``raven.agent.acp_client.capabilities.AcpMode`` records, measured at registration
    from the agent's own session response -- only the acp transport has them.
    Unlike the three capabilities above these are not a yes/no about the
    transport but a menu the dispatching model picks from, so they reach the
    spawn schema as an enum rather than the listing as a tag.

    Last, and after ``owns``, because this is a NamedTuple several callers build
    positionally: a field inserted ahead of ``owns`` moves the routing string
    into this slot with nothing raising.
    """


def agent_meta(cfg: Any, *, snapshot: Any = None) -> AgentMeta:
    """The advertised capabilities of one agent config, any kind.

    "Stateful" means reusing an instance handle continues that agent's
    conversation instead of starting a fresh one, and it is always read from the
    mechanism that would have to deliver it -- never from a wish:

    - ``builtin``: always true. The mechanism is raven's own replay of the stored
      message list (``instance_state.py``), which it owns end to end for an
      in-process loop, so there is no endpoint-specific exception to make. Its
      other two capabilities are likewise structural: the loop runs here, so it
      reads this filesystem, and it reports usage and tool calls as it works
      (``raven_loop.py``), which is what ``live_progress`` advertises.
    - ``cli``: from ``resume_command``. The schema already forces the optional
      ``stateful`` field to agree with it.
    - ``acp``: from the agent's own ``sessionCapabilities.resume``, recorded in a
      capability snapshot at registration. An acp config has no
      ``resume_command`` at all, so falling through to the cli rule would report
      every acp agent stateless -- which would strip the ``instance`` parameter
      out of the spawn schema entirely and make the DAG pre-check reject any
      graph that shares a handle.
    - ``openai``: from the config's ``stateful`` field, which defaults to true.
      The delivering mechanism here is raven's own replay of the stored message
      list (``raven/agent/subagent/instance_state.py``), which works against
      every endpoint -- so the mechanism cannot be what distinguishes them, and
      defaulting off would report a capability absent that is in fact present.
      What differs is whether a given endpoint is *meaningful* under replay, and
      that is a fact about the endpoint no probe can reach: mirothinker, for
      one, ignores a system prompt entirely, so replaying a transcript at it
      continues nothing. A false is therefore an endpoint-specific exception a
      preset states, or the operator of a custom endpoint writes by hand; the
      declaration is the only available carrier of a fact raven cannot discover.

    ``snapshot`` is the already-loaded snapshot for this config, for callers
    handling a batch. Omitted, it is read from the store -- so a caller that
    knows nothing about ACP (the spawn manager, the DAG tool) needs no change.

    Defined once here because three callers derive it -- the spawn manager, the
    DAG tool's roster, and the DAG capability pre-check -- and a split definition
    would let them disagree.
    """
    kind = getattr(cfg, "kind", None)
    if kind == "builtin":
        return AgentMeta(
            getattr(cfg, "name", "") or "",
            getattr(cfg, "description", "") or "",
            True,
            True,
            True,
            getattr(cfg, "owns", None) or "",
        )
    modes: tuple[Any, ...] = ()
    if kind == "acp":
        if snapshot is None:
            snapshot = acp_snapshot_for(cfg)
        stateful = bool(snapshot is not None and snapshot.can_resume)
        # Measured, never declared: the menu offered has to be the one the agent
        # serves, or the model is handed a mode the agent then refuses.
        modes = tuple(getattr(snapshot, "available_modes", ()) or ())
    elif kind == "openai":
        stateful = bool(getattr(cfg, "stateful", True))
    else:
        stateful = bool(getattr(cfg, "resume_command", None))
    return AgentMeta(
        getattr(cfg, "name", "") or "",
        getattr(cfg, "description", "") or "",
        stateful,
        bool(getattr(cfg, "reads_local_files", True)),
        # Only the acp transport carries a live event stream. This is a fact about
        # the transport, not about the agent, so it is read from `kind` rather
        # than from anything the agent or the operator says.
        kind == "acp",
        getattr(cfg, "owns", None) or "",
        modes,
    )


# Agents already reported as running on an edited launch config, so the notice
# below is printed once per change rather than once per dispatch.
_STALE_SNAPSHOT_SEEN: set[tuple[str, str]] = set()


def session_mcp_delivered(*, session_mcp: bool, snapshot: Any) -> bool:
    """Whether a granted server would actually reach this agent's session.

    The one predicate behind two questions that must not answer differently: what
    a dispatch delivers, and what the playbook generator is told an agent can
    take. Advertising an attachment the dispatch then withholds is worse than
    advertising none -- the generated work order assumes a capability the run
    deliberately does not have, and the degrade is silent by design.

    ``session_mcp`` is the peer's own property, resolved by
    :func:`~raven.agent.subagent.presets.session_mcp_for`; ``snapshot`` carries
    the transitional half. Both are spelled out in
    ``AcpAgentBackend._session_mcp_refused``, which is the reader for why each
    exists and why only one of them is temporary.
    """
    if not session_mcp:
        return False
    if snapshot is None or snapshot.session_mcp:
        return True
    return "raven" not in (getattr(snapshot, "agent_name", "") or "").lower()


def session_mcp_effective(cfg: Any, *, snapshot: Any = None) -> bool:
    """:func:`session_mcp_delivered` for a config, reading its snapshot if needed.

    ``snapshot`` is passed by a caller that already loaded one, so a roster build
    does not read the store twice per row.
    """
    if snapshot is None:
        snapshot = acp_snapshot_for(cfg)
    return session_mcp_delivered(session_mcp=session_mcp_for(cfg), snapshot=snapshot)


def acp_snapshot_for(cfg: Any) -> Any:
    """The stored capability snapshot for one acp config, or ``None``.

    Reads the store on every call rather than caching: the setters that need it
    run at startup and on a config hot-apply, so the cost is a small JSON read a
    handful of times, and a cache here would be the thing that keeps serving a
    stale "stateless" after a verify has already fixed it.

    A snapshot measured against an older launch config is taken rather than
    ignored -- see ``SnapshotStore.load`` for why -- and named in the log, since
    the alternative is a capability quietly disappearing with nothing anywhere
    connecting it to the edit that caused it.
    """
    from raven.agent.acp_client.capabilities import SnapshotStore

    name = getattr(cfg, "name", "") or ""
    try:
        snapshot = SnapshotStore().load([cfg], allow_stale=True).get(name)
    except Exception as exc:  # noqa: BLE001 - a missing snapshot is a degraded roster, not a crash
        logger.warning("acp capability snapshot read failed for {!r}: {}", name, exc)
        return None

    if snapshot is not None and snapshot.stale:
        seen = (name, snapshot.fingerprint)
        if seen not in _STALE_SNAPSHOT_SEEN:
            _STALE_SNAPSHOT_SEEN.add(seen)
            logger.warning(
                "acp agent {!r}: launch config changed since its capabilities were measured; "
                "still treating it as resume={} -- run a test to re-measure",
                name,
                snapshot.can_resume,
            )
    return snapshot


def format_agent_listing(meta: Sequence[AgentMeta]) -> str:
    """Render the roster for a tool description.

    Shared by ``spawn`` and ``run_subagent_dag`` so the agent reads the same
    roster wherever it picks an agent. A blank description degrades to the bare
    name plus its tags; nameless entries are dropped.

    Every capability renders as an explicit tag, positive or negative, rather
    than only flagging the negative case: the model has to *confirm* an agent is
    stateful before reusing an ``instance`` handle, and "no tag" is indistinguishable
    from "the roster does not say".
    """
    parts: list[str] = []
    for entry in meta:
        if not entry.name:
            continue
        tags = ", ".join(
            (
                "stateful" if entry.stateful else "stateless",
                "local-files" if entry.reads_local_files else "no-local-files",
                "live-progress" if entry.live_progress else "no-progress",
            )
        )
        head = f"{entry.name} [{tags}]"
        parts.append(f"{head} ({entry.description})" if entry.description else head)
    return "; ".join(parts)


def enabled_agents(configs: Sequence[Any]) -> list[Any]:
    """The subset of agent configs the model may dispatch to.

    Lives here beside ``agent_meta`` and ``format_agent_listing``
    because this module owns how a config is presented to the model, and it is
    applied inside the two consumers rather than at their call sites: five paths
    hand a config list to those setters (three CLI entry points, the AgentLoop's
    construction, and its hot-apply), so filtering at the boundary would be five
    places to keep in step and the sixth would be written without it.

    ``enabled`` is the user's intent and is deliberately not derived from a probe.
    A roster that depended on a PATH lookup or a network call would let an agent
    vanish from the model's options mid-session, and the model would then plan
    around a roster that shrank underneath it -- worse than a spawn that fails
    with a clear error. Missing attribute counts as enabled, so a duck-typed
    caller cannot silently lose agents.
    """
    return [cfg for cfg in configs or [] if getattr(cfg, "enabled", True)]


# Pre-rename spellings. Kept because both are pure functions with call sites
# across the test suite, and a rename that also changes behaviour is a rename
# whose failures are hard to attribute.
third_party_agent_meta = agent_meta
enabled_third_party = enabled_agents


def build_third_party_backend(cfg: Any, *, registry: Any = None, timeout: int | None = None) -> SubagentBackend:
    """Build a third-party backend from a config object (duck-typed on ``kind``).

    Accepts ThirdPartyCliSubagentConfig / ThirdPartyOpenAISubagentConfig.

    ``registry`` and ``timeout`` override the config for one call and exist for
    the availability test in :mod:`raven.agent.subagent.probe`, which has to
    bound a run whose config declares no timeout and has to keep a stateful
    create's handle binding out of the user's real instance file. Building the
    backend here rather than in the probe keeps one field list: a duplicated one
    would drift the moment a field is added, and the test would then silently
    exercise a different command than a real spawn. ``registry`` is ignored for
    kind ``openai``, which has no session store.
    """
    kind = getattr(cfg, "kind", None)
    if kind == "cli":
        return CliAgentBackend(
            name=cfg.name,
            command=cfg.command,
            resume_command=cfg.resume_command,
            id_source=cfg.id_source,
            session_id_pattern=cfg.session_id_pattern,
            output_pattern=cfg.output_pattern,
            transcript_format=cfg.transcript_format,
            cwd=cfg.cwd,
            env=dict(cfg.env),
            timeout=cfg.timeout if timeout is None else timeout,
            max_output_chars=cfg.max_output_chars,
            registry=registry,
            mcps=cfg.mcps,
            allow_mcp_secrets=cfg.allow_mcp_secrets,
        )
    if kind == "acp":
        from raven.agent.acp_client.capabilities import CapabilitySnapshot

        snapshot = acp_snapshot_for(cfg)
        return AcpAgentBackend(
            name=cfg.name,
            command=cfg.command,
            cwd=cfg.cwd,
            env=dict(cfg.env),
            ready_timeout_ms=cfg.ready_timeout_ms,
            timeout=cfg.timeout if timeout is None else timeout,
            max_output_chars=cfg.max_output_chars,
            snapshot=snapshot if isinstance(snapshot, CapabilitySnapshot) else None,
            registry=registry,
            mcps=cfg.mcps,
            allow_mcp_secrets=cfg.allow_mcp_secrets,
            session_mcp=session_mcp_for(cfg),
        )
    if kind == "openai":
        return OpenAIApiBackend(
            name=cfg.name,
            base_url=cfg.base_url,
            model=cfg.model,
            api_key=cfg.api_key,
            system_prompt=cfg.system_prompt,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.timeout if timeout is None else timeout,
            max_output_chars=cfg.max_output_chars,
        )
    raise ValueError(f"unknown third-party subagent kind: {kind!r}")


__all__ = [
    "ABORTED_ACTION_RESULT",
    "IN_SUBAGENT_RUN",
    "AgentMeta",
    "SubagentActionAbortedError",
    "SubagentBackend",
    "acp_snapshot_for",
    "agent_meta",
    "enabled_agents",
    "enabled_third_party",
    "format_agent_listing",
    "session_mcp_delivered",
    "session_mcp_effective",
    "third_party_agent_meta",
    "RavenLoopBackend",
    "AcpAgentBackend",
    "CliAgentBackend",
    "OpenAIApiBackend",
    "build_subagent_prompt",
    "build_third_party_backend",
]
