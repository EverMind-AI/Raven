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

from raven.agent.subagent.backends.base import (
    ABORTED_ACTION_RESULT,
    IN_SUBAGENT_RUN,
    SubagentActionAbortedError,
    SubagentBackend,
)
from raven.agent.subagent.backends.cli_agent import CliAgentBackend
from raven.agent.subagent.backends.openai_api import OpenAIApiBackend
from raven.agent.subagent.backends.raven_loop import RavenLoopBackend, build_subagent_prompt


class AgentMeta(NamedTuple):
    """One third-party sub-agent as the tool descriptions advertise it.

    Named rather than a bare tuple because every field here is a *choice the
    model has to make correctly* — which agent, whether an ``instance`` handle
    can carry context, whether a path may be handed over — and positional
    unpacking silently drops the ones a renderer forgets.
    """

    name: str
    description: str
    stateful: bool
    reads_local_files: bool


def third_party_agent_meta(cfg: Any) -> AgentMeta:
    """The advertised capabilities of one third-party subagent config.

    "Stateful" means the config carries a resume command, i.e. reusing an
    instance handle continues that agent's session instead of starting a fresh
    one — derived from ``resume_command``, never from the ``stateful``
    declaration, which the schema already forces to agree with it. OpenAI-kind
    configs have no resume command and are stateless. Defined once here because
    three callers derive it — the spawn manager, the DAG tool's roster, and the
    DAG capability pre-check — and a split definition would let them disagree.
    """
    return AgentMeta(
        getattr(cfg, "name", "") or "",
        getattr(cfg, "description", "") or "",
        bool(getattr(cfg, "resume_command", None)),
        bool(getattr(cfg, "reads_local_files", True)),
    )


def format_agent_listing(meta: Sequence[AgentMeta]) -> str:
    """Render the roster for a tool description.

    Shared by ``spawn`` and ``run_subagent_dag`` so the agent reads the same
    roster wherever it picks an agent. A blank description degrades to the bare
    name plus its tags; nameless entries are dropped.

    Both capabilities render as an explicit tag, positive or negative, rather
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
            )
        )
        head = f"{entry.name} [{tags}]"
        parts.append(f"{head} ({entry.description})" if entry.description else head)
    return "; ".join(parts)


def enabled_third_party(configs: Sequence[Any]) -> list[Any]:
    """The subset of third-party configs the model may dispatch to.

    Lives here beside ``third_party_agent_meta`` and ``format_agent_listing``
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
    "enabled_third_party",
    "format_agent_listing",
    "third_party_agent_meta",
    "RavenLoopBackend",
    "CliAgentBackend",
    "OpenAIApiBackend",
    "build_subagent_prompt",
    "build_third_party_backend",
]
