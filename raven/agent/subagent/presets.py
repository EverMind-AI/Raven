"""Built-in third-party subagent presets.

One preset per agent, and each one already carries the transport that agent is
reached over. That choice is made *here*, in the repo, from a measurement -- not
rediscovered on every user's machine at connect time. Whether hermes speaks ACP
is a fact about hermes, and probing for it per install would mean trying a second
transport whose own verification costs the user real tokens (the cli test
dispatches a task) while telling them nothing new.

So connecting is: verify the one path this table names. If it fails, say why
(adapter not installed, gateway not running, not logged in). There is no
fallback to another transport, because a silent fallback would hand the user an
agent with different capabilities than the one they asked for.

Measured on 2026-08-11, which is what fixed each transport below:

- ``Hermes Agent`` - ACP, native (``hermes acp``). resume + fork + load.
- ``Claude Code`` - ACP through the ACP project's adapter. It reads the *local*
  Claude Code credentials: a real prompt failed with exactly the error the local
  ``claude -p`` gives ("Credit balance is too low") while no ANTHROPIC_API_KEY /
  ANTHROPIC_AUTH_TOKEN / CLAUDE_CODE_OAUTH_TOKEN was set anywhere in the
  environment. That is the property that makes it usable at all: raven treats
  these as *external* agents, so an adapter demanding its own credential would
  not be acceptable.
- ``Codex`` - ACP through the ACP project's adapter. resume + load but **no
  fork**, which is exactly why capabilities are negotiated rather than declared.
- ``OpenCode`` - ACP, native (``opencode acp``). resume + fork + load.
- ``OpenClaw`` - ACP, native (``openclaw acp``). This one is a bridge backed by
  the OpenClaw Gateway rather than a self-contained server: with no reachable
  gateway it never answers ``initialize``, so pass ``--url`` / ``--token`` when
  yours needs them.
- ``MiroThinker`` - not a local agent at all: MiroMind deep-research over an
  OpenAI-compatible endpoint (set ``apiKey``).

What a row is allowed to fetch depends on what the package is. An **ACP shim** --
a dedicated adapter whose product lives elsewhere, as ``Claude Code`` and
``Codex`` are -- is
plumbing raven brings along: nobody installs one on purpose, it holds no
credential, and there is no local build to defer to, so raven fetches it and
pins the version, because ``npx -y`` would otherwise silently change which shim
build a user runs. The cost is that those need bumping deliberately.

The **agent itself** is never fetched. Its row names the bare executable
(``hermes acp``, ``opencode acp``), so the agent that answers is the one the user
installed, at the version they chose, holding the login they already granted --
and a machine without it says so, because ``_probe_acp`` resolves ``argv[0]`` and
an ``npx`` command always resolves whether the agent is there or not.
:data:`SHIM_LAUNCHED_PRESETS` is that split, declared; a test holds every
command to it.

Every preset runs unattended, so none of them may stop to ask permission: raven
answers whatever an ACP agent asks (``raven/acp_client/permissions.py``), and the
two adapters that also take a launch-time "never ask" setting are configured with
it below so the question is not asked in the first place. The rest have no such
setting -- checked, not assumed.

The cli transport is not offered by any preset any more, but it is not gone:
entries already configured that way keep working untouched, and a row whose
``kind`` disagrees with its preset's is what the UI reads as "upgradeable".

Statefulness and local-file access are not spelled out in the ``description``
text: both are structured fields the tool descriptions render as tags, so prose
saying the same thing only costs prompt tokens and can drift out of step with the
mechanism. For an acp preset they are not spelled out because they are not
*known* here -- the handshake reports them.
"""

from __future__ import annotations

from typing import Any

from raven.agent.subagent.acp_registry_presets import (
    ACP_REGISTRY_INSTALL_HINTS,
    ACP_REGISTRY_PRESETS,
    ACP_REGISTRY_SHIM_PRESETS,
)

# Command templates for the cli transport, kept as reference rather than as
# presets. Nothing reads them: they exist because the cli backend still runs
# entries configured before their agent's preset moved to acp, and someone
# hand-writing or debugging one needs the flag knowledge that was verified the
# hard way:
#
#   claude:   claude -p {prompt} --permission-mode auto --session-id {agent_id}
#             --output-format stream-json --verbose [--include-partial-messages]
#             (stream-json is required: plain output buries the answer in tens of
#             kilobytes of hook and init noise that maxOutputChars would truncate.
#             --include-partial-messages is what makes a direct chat's reply
#             stream: without it the same run prints its answer once, at the end.
#             Put it on the resume template too, or the instance streams its
#             first turn and nothing afterwards -- see CliAgentBackend._can_stream.)
#   codex:    codex -a never exec --skip-git-repo-check -s workspace-write
#             -c 'sandbox_workspace_write.network_access=true' --json {prompt}
#             (-a is a root-level flag: `codex exec -a never` exits 2. And
#             --skip-git-repo-check is required or every spawn fails at raven's
#             non-git workspace cwd. Verified against codex-cli 0.144.5.
#             No streaming to configure: `exec --json` emits no partial event,
#             so a whole reply arrives as one `item.completed`. Measured.)
#   openclaw: openclaw agent --json --session-id {agent_id} -m {prompt}
#             (create and resume are the same call; --json is required because
#             plain output interleaves ANSI plugin diagnostics on stdout.
#             NOT --local, which the shipped preset used to carry: it bypasses
#             the OpenClaw Gateway and resolves models in-process, so it needs
#             every model registered under models.providers[].models[] -- a table
#             an install that talks to its gateway never has to fill in.
#             Measured: the same prompt fails through --local with a chain of
#             `Unknown model ... no matching models.providers["openrouter"]`
#             failovers and succeeds in reaching the gateway without it. The
#             general rule this is an instance of: invoke an external agent the
#             way its user already runs it, rather than through a flag that
#             bypasses their working setup.)
#   opencode: opencode run --format json --auto {prompt}, resume with
#             --session {agent_id} (--format json is the only output carrying the
#             session id, so anything else is non-resumable)
#   hermes:   hermes --yolo chat -Q -q {prompt}, resume with --resume {agent_id}
#             (chat -Q joins a session and keeps stdout to the answer; the id is
#             printed on stderr, matched by sessionIdPattern)

# Pinned deliberately; see the module docstring.
_CLAUDE_ACP = "npx -y @agentclientprotocol/claude-agent-acp@0.66.0"
_CODEX_ACP = "npx -y @agentclientprotocol/codex-acp@1.1.14"

SHIM_LAUNCHED_PRESETS = frozenset({"claude_code", "codex"}) | ACP_REGISTRY_SHIM_PRESETS
"""Presets whose command fetches an ACP shim rather than naming a local agent.

Membership is a fact about the package, not a preference -- see the module
docstring. Every other acp preset must name an executable the user installed.

Spelled with the preset key, not the row's ``name``: what a package is cannot
depend on what the row is called, and a configured row's name is the user's to
change (``test_provenance_survives_a_rename``).
"""


THIRD_PARTY_SUBAGENT_PRESETS: dict[str, dict[str, Any]] = {
    "claude_code": {
        "name": "Claude Code",
        "preset": "claude_code",
        "kind": "acp",
        "description": (
            "An agentic coding tool that reads your codebase, edits files, runs commands, and integrates with your "
            "development tools."
        ),
        "command": _CLAUDE_ACP,
        # npx may have to download the adapter on the first connect, which is far
        # slower than starting an installed binary.
        "readyTimeoutMs": 120000,
    },
    "codex": {
        "name": "Codex",
        "preset": "codex",
        "kind": "acp",
        "description": "A coding agent from OpenAI that runs locally on your computer.",
        "command": _CODEX_ACP,
        "readyTimeoutMs": 120000,
        # Its default mode ("agent") is approval `on-request` with
        # `networkAccess: false`, which is the cli preset's sandbox minus the
        # network the cli preset explicitly turned on. Raven approves whatever
        # an ACP agent asks for anyway (raven/acp_client/permissions.py), so
        # asking buys nothing but a round trip per tool call -- and the mode is
        # also what restores network access, which no per-call approval does.
        # Measured in codex-acp 1.1.14: AgentMode.AgentFullAccess is approval
        # policy "never".
        "env": {"INITIAL_AGENT_MODE": "agent-full-access"},
    },
    "opencode": {
        "name": "OpenCode",
        "preset": "opencode",
        "kind": "acp",
        "description": "The open source coding agent.",
        "command": "opencode acp",
        # Measured on 1.18.16, when this row still fetched that build: two sessions
        # on one connection see each other's MCP servers, so a dispatch's grant
        # would reach every concurrent sub-agent of this agent. The two adapter
        # presets isolate and report the same mcpCapabilities, which is why this is
        # declared here rather than read off the handshake. Kept now that the row
        # runs whatever opencode the user installed (1.18.25 on the host this moved
        # on): it describes how opencode shares one connection rather than a quirk
        # of one build, and it withholds delivery rather than degrading it, so the
        # conservative answer is the right one to carry forward unmeasured.
        "sessionMcp": False,
    },
    "hermes": {
        "name": "Hermes Agent",
        "preset": "hermes",
        "kind": "acp",
        "description": "Nous Research's open-source agent that grows with you - a general assistant with tool calling.",
        # --accept-hooks: auto-approve unseen shell hooks. Its own prompt is a
        # TTY prompt, which a pooled stdio connection has no way to answer, so
        # without this the agent waits on a terminal that is not there.
        "command": "hermes acp --accept-hooks",
    },
    "openclaw": {
        "name": "OpenClaw",
        "preset": "openclaw",
        "kind": "acp",
        "description": (
            "A multi-channel AI gateway with extensible messaging integrations - a general assistant with its own "
            "tool set."
        ),
        "command": "openclaw acp",
        # Measured: it did not answer `initialize` within 20s while waiting on a
        # gateway, so the shared default would report a working install as
        # unreachable.
        "readyTimeoutMs": 45000,
    },
    "mirothinker": {
        "name": "MiroThinker",
        "preset": "mirothinker",
        "kind": "openai",
        "description": (
            "A deep research agent optimized for complex research and prediction tasks. Returns a sourced report "
            "with citations."
        ),
        "baseUrl": "https://api.miromind.ai/v1",
        "model": "mirothinker-1-7-deepresearch",
        "apiKey": "",
        # Stated, not left to the default: raven can replay a message list at any
        # openai endpoint, so the mechanism never distinguishes them -- what does
        # is whether the endpoint is meaningful under replay. This one ignores a
        # system prompt entirely, so a replayed transcript continues nothing.
        "stateful": False,
    },
    **ACP_REGISTRY_PRESETS,
}


def _normalized(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate entries against the schema and dump them under their wire aliases.

    The web UI fills its form straight from these, so the payload has to be
    camelCase whatever the literal above happens to say. Returning the dicts as
    written made the wire shape depend on how someone typed them: a key spelled
    snake_case arrived as an unknown field, the form read undefined, and the
    user saw a bare "cannot read properties of undefined" with nothing pointing
    back here. Validating also means a malformed preset fails at import rather
    than at the user's click.
    """
    from raven.config.schema import SubagentsConfig

    validated = SubagentsConfig(agents=entries)
    return [cfg.model_dump(by_alias=True) for cfg in validated.agents]


def third_party_subagent_presets() -> list[dict[str, Any]]:
    """Return all built-in third-party subagent presets as wire-shaped config dicts."""
    return _normalized(list(THIRD_PARTY_SUBAGENT_PRESETS.values()))


def third_party_subagent_preset(name: str) -> dict[str, Any]:
    """Return one preset by name as a wire-shaped config dict (KeyError if unknown)."""
    return _normalized([THIRD_PARTY_SUBAGENT_PRESETS[name]])[0]


def session_mcp_for(cfg: Any) -> bool:
    """Whether one configured acp agent keeps a session's MCP servers to it.

    The row's own ``sessionMcp`` when it declares one -- an operator who wrote it
    is answering for their own build, and that answer wins.

    Otherwise the measured answer for the preset the row was created from. A
    stored row is a full config and is never re-merged from the preset table when
    it loads, so a row written before this field existed carries no key. Reading
    that absence as "isolates" would leave every opencode agent already in a
    config delivering as if it did, which is the one agent measured not to -- and
    the operator would have to know a field they never wrote now needs writing.

    ``True`` where neither says anything: a hand-written row, or a preset with no
    measurement. That is the ungated stdio baseline, and withholding from an agent
    nobody has checked would turn off MCP for peers that work.
    """
    declared = getattr(cfg, "session_mcp", None)
    if declared is not None:
        return bool(declared)
    preset = THIRD_PARTY_SUBAGENT_PRESETS.get(getattr(cfg, "preset", None) or "") or {}
    return bool(preset.get("sessionMcp", True))


def install_hint_for(cfg: Any) -> str | None:
    """How to install the agent this row defers to, or ``None`` when unknown.

    Read from the row's ``preset`` provenance rather than its name, for the reason
    :func:`session_mcp_for` reads the same field: a name is the owner's to edit,
    and a hand-written row wearing a preset's name is not that agent, so telling
    its owner to install a package they did not ask for would be a guess.

    ``None`` for every hand-written row and for every agent whose install this
    repo does not know. The caller keeps its own message in that case: the
    executable name it already reports is what a person searches with.
    """
    preset = getattr(cfg, "preset", None)
    return ACP_REGISTRY_INSTALL_HINTS.get(preset) if preset else None


__all__ = [
    "SHIM_LAUNCHED_PRESETS",
    "install_hint_for",
    "THIRD_PARTY_SUBAGENT_PRESETS",
    "session_mcp_for",
    "third_party_subagent_presets",
    "third_party_subagent_preset",
]
