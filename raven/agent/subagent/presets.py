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
  fork** in the build measured then (1.13.1 adds it), which is exactly why
  capabilities are negotiated rather than declared.
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
an ``npx`` command always resolves whether the agent is there or not. For a shim
that reaches for a local install the probe therefore asks after that agent as
well (:data:`SHIM_REQUIRED_EXECUTABLES`), so a machine without ``pi`` reads Pi as
absent rather than as connectable. :data:`SHIM_LAUNCHED_PRESETS` is that split,
declared; a test holds every command to it.

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

from typing import Any, Literal, NamedTuple

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

# Pinned deliberately; see the module docstring. A bump reaches rows already
# configured only through the config migration that carries the previous stock
# command forward (``_RETIRED_SHIM_COMMANDS`` in raven/config/loader.py), since
# ``subagents.add`` copies this string into the row.
_CLAUDE_ACP = "npx -y @agentclientprotocol/claude-agent-acp@0.81.1"
_CODEX_ACP = "npx -y @agentclientprotocol/codex-acp@1.13.1"

SHIM_LAUNCHED_PRESETS = frozenset({"claude_code", "codex"}) | ACP_REGISTRY_SHIM_PRESETS
"""Presets whose command fetches an ACP shim rather than naming a local agent.

Membership is a fact about the package, not a preference -- see the module
docstring. Every other acp preset must name an executable the user installed.

Spelled with the preset key, not the row's ``name``: what a package is cannot
depend on what the row is called, and a configured row's name is the user's to
change (``test_provenance_survives_a_rename``).
"""

SHIM_REQUIRED_EXECUTABLES: dict[str, tuple[str, str]] = {
    "pi": ("pi", "npm install -g @earendil-works/pi-coding-agent"),
}
"""The local agent a shim drives, as ``(executable, install command)``, by preset key.

A shim is plumbing in front of an agent the user installs themselves, and its
``npx`` command resolves whether that agent is there or not. ``_probe_acp`` asks
after this executable as well, so a machine without it reports the row absent
with the install beside it, instead of offering a connect that fails a minute
later inside the adapter with the same sentence. Listed only where the shim
really does reach for a local install: ``pi-acp`` launches ``pi`` and fails
with "executable not found" without it. The other two shims bring their agent
along -- ``codex-acp`` ships it as its own binary, and ``claude-agent-acp``
runs the CLI its ``@anthropic-ai/claude-agent-sdk`` pin carries as a
per-platform optional dependency (read from the 0.66.0, 0.79.0 and 0.81.1
packages on 2026-09-20/21/23), so a ``claude`` on PATH is neither needed nor the
one that answers -- and what either wants is a login, not an install. The pin is
also what decides which Claude models the row can reach: 0.66.0 bundled CLI
2.1.220, which refuses a model newer than it knows ("version 2.1.251 or newer is
required"), so an account whose Claude settings name a recent model could not
connect at all, and its menu offers ``claude-fable-5[1m]`` where 0.79.0 and
0.81.1 offer ``claude-fable-5-1[1m]``; 0.79.0 bundles 2.1.274 and 0.81.1
bundles 2.1.280. The codex pin decides its menu the same
way, because ``codex-acp`` builds the model choices from its bundled codex's
``model/list``: 1.1.14 bundles codex 0.147.0, whose list stops at GPT-5.6, and
1.13.1 bundles 0.156.1, which lists the GPT-6 family (measured 2026-09-23).
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
        # Measured in codex-acp 1.1.14 and re-read in 1.13.1: AgentMode.AgentFullAccess
        # is approval policy "never", and INITIAL_AGENT_MODE still selects it.
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


class SignIn(NamedTuple):
    """Two spellings of one sign-in, and the executable that decides which.

    A shim-launched row runs on machines that never installed the agent's CLI
    globally -- that is the setup ``SHIM_REQUIRED_EXECUTABLES`` deliberately
    does not hold these rows to, because the adapter carries its own copy as a
    per-platform dependency and never links it onto PATH. So the obvious
    command is unavailable on exactly the setup this preset supports, and
    naming it there would answer a credential failure with a second one.

    ``local`` is what to run where the reader installed the CLI themselves --
    their build, their version. ``anywhere`` needs no install at all. Both end
    at the same place: the credential is the machine's, not the copy's, which
    is what makes the second one an answer rather than a detour. Measured --
    the adapter's own bundled binary and the published CLI fetched fresh report
    the same `auth status` on one machine.

    ``anywhere`` is ``None`` for a row whose command names a local install
    rather than a shim -- there is no second spelling to offer, because a
    reader who has no such install never reaches a credential failure in the
    first place: the probe stops at the absent executable, which is a different
    message with a different answer.
    """

    exe: str
    local: str
    anywhere: str | None = None
    does: Literal["sign_in", "setup"] = "sign_in"
    """What running the command does, for the page to say in its reader's words.

    ``sign_in`` signs in through a browser; ``setup`` opens the agent's own
    interactive setup, where a provider is chosen and signed in to. The English
    advice reads the same for both -- this only lets a page that renders the
    remedy itself describe the step truthfully."""
    then: str | None = None
    """What to type once the command is running, for an agent whose setup is not
    the first thing it shows.

    Qwen Code opens on its prompt, not on a sign-in, and the command that reaches
    its providers is a slash command typed there. Without this a reader told to
    run ``qwen`` is left at a prompt with nothing saying what to do next."""


SIGN_IN_HINTS: dict[str, SignIn] = {
    "claude_code": SignIn(
        exe="claude",
        local="claude auth login",
        anywhere="npx -y @anthropic-ai/claude-code auth login",
    ),
    "codex": SignIn(
        exe="codex",
        local="codex login",
        anywhere="npx -y @openai/codex login",
    ),
    # `hermes model` ("Interactively select your inference provider and default
    # model") is the first remedy hermes names when it refuses a session for want
    # of a provider -- "Hermes is not connected to any AI provider yet. Run
    # `hermes model` to pick one (the free Nous tier needs no API key)" -- and the
    # only one that needs nothing in hand. `hermes auth add <provider>`, which the
    # same sentence offers next, adds a pooled credential for a reader who already
    # holds a key. Shim-launched it is not: the command is a bare `hermes`, so the
    # local spelling is the only one it can reach.
    "hermes": SignIn(exe="hermes", local="hermes model", does="setup"),
    # `qwen auth` is gone from 0.24 -- run, it says so and names the replacement:
    # "Interactive -> run qwen and use /auth to configure providers" (measured
    # 2026-09-24, qwen 0.24.4). `/auth` ("Connect an LLM provider") is among the
    # commands the same build advertises over ACP. It covers every credential
    # refusal qwen gives -- never configured, a provider with its key unset, a
    # key the provider refuses, an expired Qwen OAuth -- because each is fixed by
    # choosing the provider again.
    "qwen_code": SignIn(exe="qwen", local="qwen", does="setup", then="/auth"),
    # `kimi login` ("Authenticate with Kimi Code CLI via the device-code flow")
    # is in `kimi --help`, and it is the command Kimi Code names over ACP too:
    # its `initialize` advertises a terminal auth method whose
    # `_meta.terminal-auth` is `kimi` with `login` (measured 2026-09-24, Kimi
    # Code 2.1.0). Its own installer puts `kimi` on PATH, so the local spelling
    # is the only one.
    "kimi_code": SignIn(exe="kimi", local="kimi login"),
}
"""How to sign in to the agent a row defers to, by preset key.

For the failure the tables above cannot catch. ``SHIM_REQUIRED_EXECUTABLES``
answers "the agent is not installed", which the probe can see before it spends
anything; this answers "it is installed and has no credential", which only the
agent itself can report, and which it reports as prose in whatever words its
vendor chose.

Every command here was read from the installed tool's own help rather than
from its documentation, because the two disagree: measured on 2026-09-23,
`codex --help` lists `login`, and `hermes model` is the command `hermes`
itself names when it refuses a session for want of a provider.

Only agents whose sign-in was read from the installed tool are listed. An
unlisted agent gets the sentence without a command, which is still the
difference between "go and sign in" and a JSON-RPC error code -- and a command
that is not there to run is the same mistake as a guessed one.
"""


class InAgent(NamedTuple):
    """A fix made from inside the agent: the command that opens it, and what to type there."""

    command: str
    then: str | None = None


MODEL_SWITCH_HINTS: dict[str, InAgent] = {
    # `/auth`, not `/model`. `/model` only picks among the models already
    # registered in ~/.qwen/settings.json, and qwen 0.24.4's own OpenRouter
    # preset registers exactly two free models, both since withdrawn from
    # OpenRouter's free tier (measured 2026-09-24: each answers 404 "unavailable
    # for free"), so for anyone set up the default way the list it offers is the
    # dead ones. `/auth` re-runs the provider setup, whose "Model IDs" step takes
    # any id the provider serves.
    "qwen_code": InAgent("qwen", "/auth"),
    # `/model` ("Switch LLM model") saves the pick as `default_model` in
    # ~/.kimi-code/config.toml: its picker's plain select persists the choice
    # (`persistModelSelection`, "Saved ... as default"), and only a separate
    # "this session only" select does not (read from Kimi Code 2.1.0).
    "kimi_code": InAgent("kimi", "/model"),
}
"""How to change the model the agent a row defers to is set to use, by preset key.

For a provider that answered but would not serve that model: gone from its free
tier, not found, out of credit or out of quota on it. Read from the installed
tool, like :data:`SIGN_IN_HINTS`; an unlisted agent is told what happened
without a command."""


DIAGNOSE_HINTS: dict[str, str] = {
    # Qwen Code retries a refused model call for minutes and says nothing while
    # it does -- measured, 90 s of ACP traffic under a 429 carried no update and
    # no stderr -- so a connect that waits 60 s learns nothing. Run in a terminal
    # the same failure prints its reason: after 91 s for a 429 ("Retrying in 60s
    # (attempt 1/10): [API Error: 429 ...]"), 93 s for an unresolvable host, 104
    # s for a provider's 500, a second or two for the rest. The positional prompt
    # is the one-shot form 0.24's own help names; `-p` is marked deprecated.
    "qwen_code": "qwen hi",
    # Kimi Code does the same: 5xx, 429, an unreachable or refused host and a
    # reply it cannot read are retried ten times over about 150 s, with nothing
    # over ACP while it does. `-p` ("Run one prompt non-interactively") prints
    # the reason once it gives up, and within a second for everything it does not
    # retry (measured 2026-09-24, Kimi Code 2.1.0).
    "kimi_code": "kimi -p hi",
}
"""A command that makes the agent a row defers to say why it is not answering.

For the failure that carries no reason at all: a connect that timed out while
the agent kept working. Only for an agent measured to fail that way, because
for the rest a timeout is not known to mean anything in particular."""


NODE_RUNTIME_PRESETS: frozenset[str] = frozenset(
    {
        # `#!/usr/bin/env node`, and a hard floor: measured 2026-09-24 on 0.24.4,
        # Node 18.20.8 exits at import ("does not provide an export named
        # 'openAsBlob'") while 20.20.2 and 22 run; the package asks for >=22.
        "qwen_code",
    }
)
"""Presets whose agent is a Node.js script, so a launch that quits may be the
wrong Node.js rather than the agent (`node_runtime.node_too_old`).

Only for an agent measured to die that way: the check runs ``node --version``
on a failure, and naming Node.js for an agent that does not care about it would
send its reader after the wrong fix."""


def runs_on_node(cfg: Any) -> bool:
    """Whether this row's agent is one of :data:`NODE_RUNTIME_PRESETS`, by provenance."""
    return getattr(cfg, "preset", None) in NODE_RUNTIME_PRESETS


def model_switch_hint_for(cfg: Any) -> InAgent | None:
    """How to change this row's model, by provenance like :func:`sign_in_hint_for`."""
    preset = getattr(cfg, "preset", None)
    return MODEL_SWITCH_HINTS.get(preset) if preset else None


def diagnose_hint_for(cfg: Any) -> str | None:
    """How to see why this row's agent is silent, by provenance like :func:`sign_in_hint_for`."""
    preset = getattr(cfg, "preset", None)
    return DIAGNOSE_HINTS.get(preset) if preset else None


def upgrade_hint_for(cfg: Any) -> str | None:
    """How to move this row's agent to its latest release, or ``None`` when unknown.

    The install hint with the package pinned to ``latest``: an npm global install
    of a bare package name installs whatever is newest only when nothing is
    installed yet, so over an old copy the plain hint can leave it where it is.
    Only for an ``npm i -g <package>`` hint, the one shape whose version is known
    to go on the package's own word.
    """
    hint = install_hint_for(cfg)
    if not hint:
        return None
    words = hint.split()
    if words[:3] not in (["npm", "i", "-g"], ["npm", "install", "-g"]) or len(words) != 4:
        return None
    package = words[3]
    # A scoped package keeps its leading "@"; a version is the "@" after that.
    if "@" in package[1:]:
        return None
    return f"{hint}@latest"


def sign_in_hint_for(cfg: Any) -> SignIn | None:
    """How to sign in to the agent this row defers to, or ``None`` when unknown.

    By provenance, like :func:`install_hint_for` and for the same reason: the
    name is the owner's to edit, so a hand-written row wearing a preset's name
    is not that agent and must not be told to run that agent's login.
    """
    preset = getattr(cfg, "preset", None)
    return SIGN_IN_HINTS.get(preset) if preset else None


def shim_requirement_for(cfg: Any) -> tuple[str, str] | None:
    """The agent a shim-launched row needs installed, with its install, or ``None``.

    By provenance, like :func:`install_hint_for`, and for the same reason: the
    row's name is its owner's to change, and a hand-written row that merely
    wears a preset's name runs whatever command it wrote.
    """
    preset = getattr(cfg, "preset", None)
    return SHIM_REQUIRED_EXECUTABLES.get(preset) if preset else None


__all__ = [
    "SHIM_LAUNCHED_PRESETS",
    "SHIM_REQUIRED_EXECUTABLES",
    "SIGN_IN_HINTS",
    "SignIn",
    "install_hint_for",
    "shim_requirement_for",
    "sign_in_hint_for",
    "THIRD_PARTY_SUBAGENT_PRESETS",
    "session_mcp_for",
    "third_party_subagent_presets",
    "third_party_subagent_preset",
]
