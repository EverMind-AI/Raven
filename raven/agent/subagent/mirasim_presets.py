"""Presets for the agents Mirasim runs, one row per agent, each reached through Raven's own shim.

Mirasim (https://mirasim.ai) launches third-party coding agents on the accounts
and model routes its user configured, and drives them through ``mirasim
ui-cli``. A row here is that agent as Mirasim runs it -- its model menu is
Mirasim's for that agent, the platform's models and the user's own keys beside
the agent's own -- which is a different agent to delegate to than the same
agent's own row in :mod:`raven.agent.subagent.presets`, launched on its own login.
That is why the two coexist: not two transports for one install, which the preset
table rules out, but two ways of paying for and routing one tool.

The command runs :mod:`raven.acp.mirasim` on the interpreter Raven itself runs
on, so ``{PYTHON}`` is expanded when a preset is read (see
:func:`raven.agent.subagent.presets.third_party_subagent_presets`). ``-P`` keeps
the launch directory -- the caller's working directory, which may itself be a
checkout of this repository -- off ``sys.path``, so the adapter that answers is the
installed one.

Measured 2026-10-12 against Mirasim 0.0.465, where ``mirasim ui-cli agents``
listed these nine as installed, beside ``echo``: a stub that answers with its
prompt, which is not an agent to delegate to. The labels are the ones it
reports, so a row reads the way the agent does in Mirasim.
"""

from __future__ import annotations

from typing import Any

MIRASIM_AGENTS: dict[str, tuple[str, str, str]] = {
    "mirasim_claude": ("claude", "Claude Code", "Anthropic's agentic coding tool"),
    "mirasim_codex": ("codex", "Codex", "OpenAI's coding agent"),
    "mirasim_kimi": ("kimi", "Kimi Code", "Moonshot AI's coding assistant"),
    "mirasim_antigravity": ("antigravity", "Antigravity", "Google's agentic coding tool"),
    "mirasim_qwen": ("qwen", "Qwen Code", "Alibaba's Qwen coding assistant"),
    "mirasim_grok": ("grok", "Grok Build", "xAI's coding agent"),
    "mirasim_dsh": ("dsh", "DSH", "DeepSeek Harness, a coding agent on DeepSeek models"),
    "mirasim_pi": ("pi", "Pi", "A coding agent CLI with read, bash, edit and write tools"),
    "mirasim_zcode": ("zcode", "ZCode CLI", "Z.ai's ZCode coding agent"),
}
"""Preset key -> (Mirasim agent id, Mirasim's label for it, what it is)."""

MIRASIM_SHIM_PRESETS = frozenset(MIRASIM_AGENTS)

MIRASIM_EXECUTABLE = "mirasim"
MIRASIM_INSTALL_HINT = "the Mirasim CLI from https://mirasim.ai/download"


def _row(key: str, agent: str, label: str, what: str) -> dict[str, Any]:
    return {
        "name": f"{label}-Mirasim",
        "preset": key,
        "kind": "acp",
        "description": f"{what}, run through Mirasim.",
        "command": f"{{PYTHON}} -P -m raven.acp.mirasim --agent {agent}",
        # The first session waits on two Mirasim reads, each starting a server
        # of its own: 7 s for one adapter, 52 s for nine adapters at once.
        "readyTimeoutMs": 120000,
        # Mirasim's ui-cli takes no MCP servers for a session, so nothing
        # handed over could reach the agent. False is the one declaration that
        # withholds them and also tells the roster none arrive, so a DAG node's
        # `mcps` is reported as ignored rather than assumed. The withheld note,
        # which shows only when a row's own `mcps` names servers, gives
        # isolation as its reason: the one the field was written for.
        "sessionMcp": False,
    }


MIRASIM_PRESETS: dict[str, dict[str, Any]] = {
    key: _row(key, agent, label, what) for key, (agent, label, what) in MIRASIM_AGENTS.items()
}

__all__ = [
    "MIRASIM_AGENTS",
    "MIRASIM_EXECUTABLE",
    "MIRASIM_INSTALL_HINT",
    "MIRASIM_PRESETS",
    "MIRASIM_SHIM_PRESETS",
]
