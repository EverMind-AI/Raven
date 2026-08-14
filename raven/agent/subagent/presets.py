"""Built-in third-party subagent presets (req5).

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

- ``hermes`` - ACP, native (``hermes acp``). resume + fork + load.
- ``claude_code`` - ACP through the ACP project's adapter. It reads the *local*
  Claude Code credentials: a real prompt failed with exactly the error the local
  ``claude -p`` gives ("Credit balance is too low") while no ANTHROPIC_API_KEY /
  ANTHROPIC_AUTH_TOKEN / CLAUDE_CODE_OAUTH_TOKEN was set anywhere in the
  environment. That is the property that makes it usable at all: raven treats
  these as *external* agents, so an adapter demanding its own credential would
  not be acceptable.
- ``codex`` - ACP through the ACP project's adapter. resume + load but **no
  fork**, which is exactly why capabilities are negotiated rather than declared.
- ``opencode`` - ACP, native (``opencode acp``). resume + fork + load.
- ``openclaw`` - ACP, native (``openclaw acp``). This one is a bridge backed by
  the OpenClaw Gateway rather than a self-contained server: with no reachable
  gateway it never answers ``initialize``, so pass ``--url`` / ``--token`` when
  yours needs them.
- ``mirothinker`` - not a local agent at all: MiroMind deep-research over an
  OpenAI-compatible endpoint (set ``apiKey``).

Adapter versions are pinned. ``npx -y`` will fetch one that is not present, so an
unpinned command would silently change which adapter build a user runs; the cost
is that these need bumping deliberately.

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

# Command templates for the cli transport, kept as reference rather than as
# presets. Nothing reads them: they exist because the cli backend still runs
# entries configured before their agent's preset moved to acp, and someone
# hand-writing or debugging one needs the flag knowledge that was verified the
# hard way:
#
#   claude:   claude -p {prompt} --permission-mode auto --session-id {agent_id}
#             --output-format stream-json --verbose
#             (stream-json is required: plain output buries the answer in tens of
#             kilobytes of hook and init noise that maxOutputChars would truncate)
#   codex:    codex -a never exec --skip-git-repo-check -s workspace-write
#             -c 'sandbox_workspace_write.network_access=true' --json {prompt}
#             (-a is a root-level flag: `codex exec -a never` exits 2. And
#             --skip-git-repo-check is required or every spawn fails at raven's
#             non-git workspace cwd. Verified against codex-cli 0.144.5.)
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
_OPENCODE_ACP = "npx -y opencode-ai@1.18.16 acp"

THIRD_PARTY_SUBAGENT_PRESETS: dict[str, dict[str, Any]] = {
    "claude_code": {
        "name": "claude_code",
        "preset": "claude_code",
        "kind": "acp",
        "description": (
            "Claude Code over ACP - strong general coding / agent tasks. Uses this machine's "
            "existing Claude Code login. Fetched on first use via npx."
        ),
        "command": _CLAUDE_ACP,
        # npx may have to download the adapter on the first connect, which is far
        # slower than starting an installed binary.
        "readyTimeoutMs": 120000,
    },
    "codex": {
        "name": "codex",
        "preset": "codex",
        "kind": "acp",
        "description": (
            "OpenAI Codex over ACP - coding tasks. Fetched on first use via npx. Supports "
            "resuming a session but not forking one."
        ),
        "command": _CODEX_ACP,
        "readyTimeoutMs": 120000,
    },
    "opencode": {
        "name": "opencode",
        "preset": "opencode",
        "kind": "acp",
        "description": (
            "OpenCode over ACP - open-source coding agent. Uses whichever provider and model "
            "the local opencode install is configured for. Fetched on first use via npx."
        ),
        "command": _OPENCODE_ACP,
        "readyTimeoutMs": 120000,
    },
    "hermes": {
        "name": "hermes",
        "preset": "hermes",
        "kind": "acp",
        "description": "Hermes Agent over ACP - general assistant with tool calling.",
        "command": "hermes acp",
    },
    "openclaw": {
        "name": "openclaw",
        "preset": "openclaw",
        "kind": "acp",
        "description": (
            "OpenClaw over ACP - general assistant with its own tool set. This is a bridge "
            "backed by the OpenClaw Gateway, not a self-contained server: with no reachable "
            "gateway it never answers the handshake, so pass --url / --token when yours needs "
            "them."
        ),
        "command": "openclaw acp",
        # Measured: it did not answer `initialize` within 20s while waiting on a
        # gateway, so the shared default would report a working install as
        # unreachable.
        "readyTimeoutMs": 45000,
    },
    "mirothinker": {
        "name": "mirothinker",
        "preset": "mirothinker",
        "kind": "openai",
        "description": (
            "MiroMind deep-research (OpenAI-compatible HTTP). Returns a sourced report "
            "with citations. Requires an apiKey."
        ),
        "baseUrl": "https://api.miromind.ai/v1",
        "model": "mirothinker-1-7-deepresearch",
        "apiKey": "",
    },
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

    validated = SubagentsConfig(third_party=entries)
    return [cfg.model_dump(by_alias=True) for cfg in validated.third_party]


def third_party_subagent_presets() -> list[dict[str, Any]]:
    """Return all built-in third-party subagent presets as wire-shaped config dicts."""
    return _normalized(list(THIRD_PARTY_SUBAGENT_PRESETS.values()))


def third_party_subagent_preset(name: str) -> dict[str, Any]:
    """Return one preset by name as a wire-shaped config dict (KeyError if unknown)."""
    return _normalized([THIRD_PARTY_SUBAGENT_PRESETS[name]])[0]


__all__ = [
    "THIRD_PARTY_SUBAGENT_PRESETS",
    "third_party_subagent_presets",
    "third_party_subagent_preset",
]
