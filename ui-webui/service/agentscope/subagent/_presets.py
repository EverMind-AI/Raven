# -*- coding: utf-8 -*-
"""Built-in sub-agent presets (Claude Code, Codex, MiroMind).

Each preset is a ready-to-instantiate config payload (without an ``id``,
which the create endpoint mints). The frontend's "Add from preset" control
POSTs a preset's ``data`` to ``POST /subagent/``.
"""

_CODEX_COMMAND = (
    "codex -a never exec -s workspace-write "
    "-c 'sandbox_workspace_write.network_access=true' --json {prompt}"
)
_CODEX_RESUME_COMMAND = (
    "codex -a never exec -s workspace-write "
    "-c 'sandbox_workspace_write.network_access=true' "
    "--json resume {agent_id} {prompt}"
)


def list_subagent_presets() -> list[dict]:
    """Return the built-in sub-agent presets.

    Returns:
        `list[dict]`:
            One dict per preset with keys ``preset_id`` (stable id),
            ``label`` (human name), and ``data`` (a valid sub-agent
            config payload without an ``id``).
    """
    return [
        {
            "preset_id": "claude_code",
            "label": "Claude Code",
            "data": {
                "type": "cli_subagent",
                "name": "claude_code",
                "description": (
                    "Delegate a coding task to Claude Code. Stateful: a "
                    "new instance handle starts a fresh session; reusing "
                    "a handle resumes it."
                ),
                "command": (
                    "claude -p {prompt} --permission-mode auto "
                    "--session-id {agent_id}"
                ),
                "resume_command": (
                    "claude -p {prompt} --permission-mode auto "
                    "--resume {agent_id}"
                ),
                "id_source": "provisioned",
                "transcript_format": "text",
                "session_id_pattern": None,
                "output_pattern": None,
                "cwd": None,
                "env": None,
                "timeout": 600,
            },
        },
        {
            "preset_id": "codex",
            "label": "Codex",
            "data": {
                "type": "cli_subagent",
                "name": "codex",
                "description": (
                    "Delegate a coding task to OpenAI Codex. Stateful: "
                    "the session id and reply are parsed from the "
                    "`exec --json` JSONL stream and the id is reused on "
                    "resume."
                ),
                "command": _CODEX_COMMAND,
                "resume_command": _CODEX_RESUME_COMMAND,
                "id_source": "derived",
                "transcript_format": "codex_jsonl",
                "session_id_pattern": None,
                "output_pattern": None,
                "cwd": None,
                "env": None,
                "timeout": 600,
            },
        },
        {
            "preset_id": "miromind_deepresearch",
            "label": "MiroThinker Deep Research (MiroMind)",
            "data": {
                "type": "openai_subagent",
                "name": "miro_deepresearch",
                "description": (
                    "Delegate a deep-research question to MiroMind "
                    "mirothinker-1-7-deepresearch (web search, code "
                    "execution, tool use). Returns a sourced report with "
                    "citations. Stateful: reuse an instance handle to "
                    "continue the same research thread. Requires an "
                    "OpenAI-compatible credential (base_url "
                    "https://api.miromind.ai/v1)."
                ),
                "credential_id": "",
                "model": "mirothinker-1-7-deepresearch",
                "stateful": True,
                "system_prompt": None,
                "temperature": None,
                "max_tokens": None,
                "timeout": 1200,
            },
        },
    ]
