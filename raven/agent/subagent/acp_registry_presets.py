"""Presets for the agents listed in the ACP registry.

Provenance is the official registry
(``https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json``, schema
1.0.0, read 2026-09-07), not a hand-kept list: it carries each agent's launch
package, arguments and licence as structured data, so the arguments below were
read rather than transcribed.

Two launch shapes, and which one a row gets is a fact about the package, not a
preference:

- The package **is the agent** (its npm ``bin`` is the product's own CLI and ACP
  is a flag on it). The row names the bare executable -- ``copilot --acp`` -- so
  the agent that answers is the one the user installed, at the version they
  chose, holding the login they already granted. Fetching a second copy here
  would run a build they never picked against the credentials they did, and would
  report the agent as installed on a machine that does not have it, because
  :func:`raven.agent.subagent.probe._probe_acp` resolves ``argv[0]`` and ``npx``
  always resolves. These rows carry no version: pinning one would contradict the
  whole point of deferring to the install.
- The package is an **ACP shim** (a dedicated adapter whose product lives
  elsewhere). Nobody installs a shim on purpose, it holds no credential of its
  own, and there is no local build to defer to -- so raven fetches it, pinned,
  because ``npx -y`` silently changes which shim build runs otherwise. Same
  reasoning as the two adapters in :mod:`raven.agent.subagent.presets`, and
  :data:`ACP_REGISTRY_SHIM_PRESETS` is the declaration a test holds the commands
  to.

What was measured, on 2026-09-07: every row below answered ``initialize`` and
reported its own ``agentInfo``, checked with the same handshake the Test button
runs (:func:`raven.acp_client.capabilities.verify_agent` -- two round trips, no
prompt, so no tokens). For a shim row that check ran the shipped command. For a
local-executable row it ran the registry's pinned ``npx`` form instead, none of
those products being installed on the machine this was measured from: it
establishes that the product speaks ACP and that the argument spelling is right,
which is what the row asserts. It does not establish that the build on any
particular machine still spells it that way -- an agent that renames the flag
shows up as a handshake failure on that row, with the launch error, and not as a
silent wrong answer.

Every row here also **completed a real turn**, rendered its tool calls into the
rows a reader gets, held context across two prompts on one session, and resumed a
session from a fresh process. That bar is why there are six rows and not sixteen:
the registry offered ten more that reach ``ready`` and then cannot do the work,
almost all of them waiting on a credential their owner has to grant. They are a
separate change, once logged in and re-measured, because a roster entry that
cannot run a task is worse than an absent one -- the model reads it as available.

Left out, deliberately:

- Ten registry agents that answered the handshake and no more: nine of them stop
  at the first model call (their sign-in, an API key, or an unfinished OAuth), and
  ``dimcode`` refuses ``session/load`` with ``-32603`` while advertising it.
- ``nova``, ``sigit``, ``fast_agent`` and ``minion_code`` never answered the
  handshake at all, so there is nothing measured to ship. ``nova`` sat silent for
  the whole 240s budget rather than failing, which is the shape a row would have
  had on every user's machine too. The last two were the registry's only ``uvx``
  entries, which is why no row here uses one.
- Registry entries distributed as a platform binary (cursor, kimi, antigravity,
  goose, junie, devin, amp, mistral-vibe and the rest). A preset is a command,
  not an installer, and the archive-per-platform table those carry is the
  installer's job.
- ``agoragentic``, which takes ``--acp`` but is an agent marketplace settling
  per-call payments in USDC rather than an agent, and does not belong in a
  default roster.
- Kiro CLI and OpenHands, which speak ACP but have no registry entry, so neither
  a pinnable shim nor a documented executable name could be read for them.

``sessionMcp`` is undeclared on every row, which resolves to ``True`` -- see
:func:`raven.agent.subagent.presets.session_mcp_for`. Whether one session's MCP
servers stay in that session takes two concurrent sessions and a live server to
measure, so it is not known for any agent here, and ``True`` is the ungated stdio
baseline: withholding MCP from an agent nobody has measured would break peers
that work. Nor does any row set a launch mode. ``session/set_mode`` is answered
inconsistently across these agents -- some accept an id and ignore it -- so
declaring one blind would be a claim, not a setting, and raven answers permission
requests anyway (:mod:`raven.acp_client.permissions`).
"""

from __future__ import annotations

from typing import Any

_SHIM_READY_TIMEOUT_MS = 120000
"""A shim is fetched on first connect, which is far slower than starting an
installed binary. A local executable needs no such allowance and takes the
schema default."""

ACP_REGISTRY_SHIM_PRESETS = frozenset({"pi"})
"""Rows raven fetches, because the package is an adapter and not the agent.

Preset keys, on the same reasoning as :data:`SHIM_LAUNCHED_PRESETS`."""

ACP_REGISTRY_INSTALL_HINTS: dict[str, str] = {
    "github_copilot": "npm i -g @github/copilot",
    "qwen_code": "npm i -g @qwen-code/qwen-code",
    "codebuddy": "npm i -g @tencent-ai/codebuddy-code",
    "qoder": "npm i -g @qoder-ai/qodercli",
    "grok": "npm i -g @xai-official/grok",
}
"""How to install the agent a row defers to, for the probe that reports it absent.

Keyed by preset key, so a row renamed by its owner keeps its hint and a row that
merely wears a preset's name gets none -- the same boundary the table draws
everywhere else.

Only the rows whose vendor publishes the agent on npm are listed. The three
:data:`ACP_REGISTRY_SHIM_PRESETS`-adjacent lanes need nothing: a shim command is
an ``npx`` one and always resolves, so the absent-executable branch these hints
serve is unreachable for them. An agent distributed as a per-platform binary is
absent here rather than guessed at, because one command cannot name the install
for six platform targets and a wrong one is worse than the executable name the
probe already reports.
"""

ACP_REGISTRY_PRESETS: dict[str, dict[str, Any]] = {
    "github_copilot": {
        "name": "GitHub Copilot",
        "preset": "github_copilot",
        "kind": "acp",
        "description": "GitHub's AI pair programmer.",
        "command": "copilot --acp",
    },
    "qwen_code": {
        "name": "Qwen Code",
        "preset": "qwen_code",
        "kind": "acp",
        "description": "Alibaba's Qwen coding assistant.",
        "command": "qwen --acp",
    },
    "codebuddy": {
        "name": "CodeBuddy",
        "preset": "codebuddy",
        "kind": "acp",
        "description": "Tencent Cloud's official intelligent coding tool.",
        "command": "codebuddy --acp",
    },
    "qoder": {
        "name": "Qoder",
        "preset": "qoder",
        "kind": "acp",
        "description": "An AI coding assistant with agentic capabilities.",
        "command": "qodercli --acp",
    },
    "grok": {
        "name": "Grok Build",
        "preset": "grok",
        "kind": "acp",
        "description": "xAI's coding agent and CLI.",
        "command": "grok agent stdio",
    },
    "pi": {
        "name": "Pi",
        "preset": "pi",
        "kind": "acp",
        "description": "A coding agent CLI with read, bash, edit, write tools and session management.",
        "command": "npx -y pi-acp@0.0.33",
        "readyTimeoutMs": _SHIM_READY_TIMEOUT_MS,
    },
}


__all__ = ["ACP_REGISTRY_PRESETS", "ACP_REGISTRY_SHIM_PRESETS"]
