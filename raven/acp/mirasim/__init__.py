"""An ACP agent for each agent Mirasim runs, served by Raven over Mirasim's ``ui-cli``.

Mirasim (https://mirasim.ai) runs third-party coding agents -- Claude Code,
Codex, Kimi Code and others -- behind one workbench, with a model menu that adds
its own platform's models and the user's own keys to each agent's own. It has no
ACP surface, so Raven brings one: ``python -m raven.acp.mirasim --agent <id>``
serves one Mirasim agent on stdio, and the ``mirasim_*`` presets in
:mod:`raven.agent.subagent.mirasim_presets` launch it.

The layers, outermost first:

* :mod:`raven.acp.mirasim.server` -- the frame loop and the ACP methods.
* :mod:`raven.acp.mirasim.interactions` -- what a running turn stops to ask, as
  the request that puts it to the client, and the client's reply as an answer.
* :mod:`raven.acp.mirasim.store` -- which Mirasim conversation an ACP session
  continues, kept across adapter processes.
* :mod:`raven.acp.mirasim.catalog` -- the agent's models and effort rungs, as
  the menus Raven reads off any ACP agent.
* :mod:`raven.acp.mirasim.uicli` -- one ``mirasim ui-cli`` command: its argv,
  its environment, its JSON, and ending it with everything it started.
"""
