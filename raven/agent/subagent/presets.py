"""Built-in third-party subagent presets (req5).

Copy any of these into ``subagents.thirdParty`` in ``~/.raven/config.json`` (or
add them from the web UI) so the main agent can dispatch to that agent via
``spawn(agent=<name>)``. They are templates: adjust ``command`` / ``baseUrl`` /
``model`` to your install.

- ``claude_code`` - Claude Code CLI headless, stateful. ``--output-format
  stream-json --verbose`` is required: the plain text output buries the answer
  in tens of kilobytes of hook and init noise, which ``maxOutputChars`` would
  truncate away.
- ``codex`` - OpenAI Codex CLI headless, stateful. The session id and the reply
  are both read out of the ``exec --json`` JSONL stream.
- ``mirothinker`` - MiroMind deep-research over an OpenAI-compatible endpoint
  (set ``apiKey``).
- ``openclaw`` - OpenClaw agent CLI, stateful. ``--json`` is required: plain
  output interleaves ANSI-coloured diagnostics on stdout.
- ``hermes`` - Hermes Agent CLI, stateful. The ``chat -Q`` subcommand is
  required to join a session; the reply comes off stdout and the session id
  off a stderr line.
- ``opencode`` - OpenCode CLI, stateful. ``--format json`` is required for the
  session id: the default output has no line carrying it, so a text transcript
  could only ever produce a non-resumable run.

Statefulness and local-file access are not spelled out in the ``description``
text: both are structured fields the tool descriptions render as tags, so prose
saying the same thing only costs prompt tokens and can drift out of step with
the mechanism.
"""

from __future__ import annotations

from typing import Any

_CLAUDE_TAIL = "--output-format stream-json --verbose"
# The approval policy is a root-level codex flag: `codex exec -a never` is a
# parse error, so it must sit before the subcommand.
_CODEX_HEAD = (
    "codex -a never exec --skip-git-repo-check -s workspace-write "
    "-c 'sandbox_workspace_write.network_access=true' --json"
)
# Create and resume are the same call: an openclaw session is addressed by the id
# the caller supplies. --json is required, not cosmetic - plain output interleaves
# ANSI-coloured plugin and transport diagnostics on stdout, and --verbose off does
# not suppress them, so a text transcript cannot yield the reply.
_OPENCLAW_CMD = "openclaw agent --local --json --session-id {agent_id} -m {prompt}"
# `chat -q` rather than the global `-z`: -z does not join a session, so
# `-z --resume <id>` silently starts a new one. -Q keeps stdout to the final
# answer alone, and prints the session id on stderr.
_HERMES_ONESHOT = "hermes --yolo chat -Q -q {prompt}"
# --format json is required, not cosmetic: it is the only output that carries the
# session id, so without it every run would be non-resumable. --auto is the
# headless-permission flag (the counterpart of claude's --permission-mode auto
# and codex's -a never); without it a run needing a tool waits for an approval
# nobody is there to give. Create takes no {agent_id}: `run --session` only
# resumes an existing session and errors with "Session not found" on an id it
# does not know, so opencode mints its own id and raven reads it back out.
_OPENCODE_HEAD = "opencode run --format json --auto"

THIRD_PARTY_SUBAGENT_PRESETS: dict[str, dict[str, Any]] = {
    "claude_code": {
        "name": "claude_code",
        "preset": "claude_code",
        "kind": "cli",
        "description": "Claude Code CLI - strong general coding / agent tasks.",
        "command": f"claude -p {{prompt}} --permission-mode auto --session-id {{agent_id}} {_CLAUDE_TAIL}",
        "resumeCommand": f"claude -p {{prompt}} --permission-mode auto --resume {{agent_id}} {_CLAUDE_TAIL}",
        "idSource": "provisioned",
        "transcriptFormat": "claude_stream_json",
    },
    "codex": {
        "name": "codex",
        "preset": "codex",
        "kind": "cli",
        "description": "OpenAI Codex CLI - coding tasks.",
        "command": f"{_CODEX_HEAD} {{prompt}}",
        "resumeCommand": f"{_CODEX_HEAD} resume {{agent_id}} {{prompt}}",
        "idSource": "derived",
        "transcriptFormat": "codex_jsonl",
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
    "openclaw": {
        "name": "openclaw",
        "preset": "openclaw",
        "kind": "cli",
        "description": (
            "OpenClaw agent CLI - general assistant with its own tool set. Needs a node "
            "the CLI supports on PATH, and an agent id whose workspace has no persona "
            "files, or the first turn of a fresh session answers the bootstrap instead "
            "of the task."
        ),
        "command": _OPENCLAW_CMD,
        "resumeCommand": _OPENCLAW_CMD,
        "idSource": "provisioned",
        "transcriptFormat": "openclaw_json",
    },
    "opencode": {
        "name": "opencode",
        "preset": "opencode",
        "kind": "cli",
        "description": (
            "OpenCode CLI - open-source coding agent. Uses whichever provider and model "
            "the local opencode install is configured for."
        ),
        "command": f"{_OPENCODE_HEAD} {{prompt}}",
        "resumeCommand": f"{_OPENCODE_HEAD} --session {{agent_id}} {{prompt}}",
        "idSource": "derived",
        "transcriptFormat": "opencode_json",
    },
    "hermes": {
        "name": "hermes",
        "preset": "hermes",
        "kind": "cli",
        "description": "Hermes Agent CLI - general assistant with tool calling.",
        "command": _HERMES_ONESHOT,
        "resumeCommand": f"{_HERMES_ONESHOT} --resume {{agent_id}}",
        "idSource": "derived",
        "transcriptFormat": "text",
        "sessionIdPattern": r"session_id:\s*(\S+)",
        "outputPattern": r"(?s)\A(.*?)\s*\Z",
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
