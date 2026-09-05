"""Factories for the bundled playbook plugin.

Both return the tool unbound: the funnel they serve is assembled by the loop
after every factory has run, and arrives through ``bind_runtime`` (see
``RuntimeHandles.playbook_runtime``). A loop that builds no funnel -- the
feature is off, or its assembly failed -- leaves the handle ``None`` and the
tools decline, which unregisters them quietly.

The imports live inside the factories: a factory reference is the natural
lazy boundary, so listing plugins never pays for the playbook stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.plugins.context import PluginContext


def make_load_playbook_tool(_ctx: "PluginContext") -> Any:
    from raven.agent.tools.load_playbook import LoadPlaybookTool

    return LoadPlaybookTool()


def make_create_playbook_tool(_ctx: "PluginContext") -> Any:
    from raven.agent.tools.create_playbook import CreatePlaybookTool

    return CreatePlaybookTool()
