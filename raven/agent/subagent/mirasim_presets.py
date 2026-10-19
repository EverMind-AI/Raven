"""Presets for the agents Mirasim runs, one row per agent, each reached through Raven's own shim.

Mirasim (https://mirasim.ai) launches third-party coding agents on the accounts
and model routes its user configured, from the Mirasim host running on the
machine. A row here is that agent as Mirasim runs it -- its model menu is
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

Measured 2026-10-12 against Mirasim 0.0.465, which listed these nine as
installed, beside ``echo``: a stub that answers with its prompt, which is not an
agent to delegate to. The labels are the ones it reports -- 0.0.537's host lists
the same nine under the same labels, plus OpenCode, which Raven already offers
on its own -- so a row reads the way the agent does in Mirasim.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from raven.agent.subagent.vendored_agents import _resolved_python
from raven.utils.commands import command_argv, resolve_subagent_command

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
        # The first session waits on the host listing its agents, which it
        # answers progressively: measured at about two seconds, with the rows
        # probing every agent CLI on the machine. The budget covers the
        # adapter's own limits instead -- reaching the host (15 s), then the
        # agent list and the menus (60 s), twice when the first list says the
        # agent is not installed -- so a slow host is refused by the adapter,
        # saying why, rather than timed out as an agent that hung.
        "readyTimeoutMs": 150000,
        # The Mirasim host takes no MCP servers for one session, so nothing
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

INSTALLED_QUERY = "{PYTHON} -P -m raven.acp.mirasim --installed"

INSTALLED_NO_HOST_EXIT = 3
"""The exit status of ``--installed`` when no Mirasim host could be reached to ask."""

HOST_ENV_KEYS = ("RAVEN_MIRASIM_HOST", "MIRASIM_HOME")
"""The variables the adapter finds its host by (``raven.acp.mirasim.host``).

A row whose ``env`` sets one looks for a host of its own, which a question asked
in the login shell's environment says nothing about."""

INSTALLED_TIMEOUT_S = 60.0
"""How long the question may take: the host listing its agents, under the
presets' own ``readyTimeoutMs`` for the same work."""


async def installed_agents(*, path: str | None) -> frozenset[str] | None:
    """The agents Mirasim has installed, asked once through the adapter; ``None`` when it could not say.

    Asked before the boot handshakes the presets nobody has connected yet. One
    whose agent Mirasim lacks refuses its session, the boot keeps no preset's
    failure, and so it was handshaken again at every start for nothing. With no
    host running at all, none is installed as far as a handshake could tell --
    each would fail on that same missing host -- so the answer is empty. A host
    that does not answer in time is ``None``, which leaves it to the handshake.

    Asked in the environment the presets' adapters are launched in, the login
    shell's, so a ``RAVEN_MIRASIM_HOST`` set there is the one both read (a row
    that sets one in its own ``env`` is answered for by its own handshake).
    ``path`` is the login shell's PATH, the one the presets' own check for
    ``mirasim`` resolves against.
    """
    from raven.agent.subagent.backends.env import login_shell_env

    argv = command_argv(resolve_subagent_command(INSTALLED_QUERY, python=_resolved_python(), subagent_dir=""))
    login = await asyncio.to_thread(login_shell_env)
    env = {**login, "PATH": path} if path else dict(login)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        logger.debug("Mirasim was not asked which agents it has: {}", exc)
        return None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), INSTALLED_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.debug("Mirasim did not say which agents it has within {:.0f}s", INSTALLED_TIMEOUT_S)
        return None
    if proc.returncode == INSTALLED_NO_HOST_EXIT:
        logger.debug("Mirasim lists no agents: {}", err.decode("utf-8", "replace").strip())
        return frozenset()
    if proc.returncode != 0:
        logger.debug("Mirasim did not say which agents it has: {}", err.decode("utf-8", "replace").strip())
        return None
    try:
        listed = json.loads(out)
    except ValueError:
        return None
    if not isinstance(listed, list) or not all(isinstance(agent, str) for agent in listed):
        return None
    return frozenset(listed)


__all__ = [
    "INSTALLED_QUERY",
    "MIRASIM_AGENTS",
    "MIRASIM_EXECUTABLE",
    "MIRASIM_INSTALL_HINT",
    "MIRASIM_PRESETS",
    "MIRASIM_SHIM_PRESETS",
    "installed_agents",
]
