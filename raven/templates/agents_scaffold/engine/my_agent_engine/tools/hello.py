"""The hello tool: the minimal Tool implementation, four members.

``name`` decides net-new versus replacement: a name no built-in uses adds a
tool, a built-in's name (web_search, ask_user) replaces it. ``description``
is a prompt -- the model decides from it alone when to call this tool.

The name is derived from the agent (``my_agent_hello``), never a bare word:
tool names are one namespace across every activated plugin in the process,
and two agents both shipping a plain ``hello`` would collide -- a conflict
that takes the whole plugin registry down, not just one tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.contracts.tool import Tool

if TYPE_CHECKING:
    from raven.plugins.context import PluginContext


class HelloTool(Tool):
    """Answers a greeting; exists to prove the injection seam end to end."""

    def __init__(self, greeting: str) -> None:
        self._greeting = greeting

    @property
    def name(self) -> str:
        return "my_agent_hello"

    @property
    def description(self) -> str:
        return "Greet someone by name. Call this when the user asks for a hello or a smoke-test greeting."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Who to greet; defaults to 'world'.",
                }
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        who = str(kwargs.get("name") or "world")
        return f"{self._greeting}, {who}! (from my-agent-engine)"


def make_hello(ctx: "PluginContext") -> Tool | None:
    """Factory the manifest names: Callable[[PluginContext], Tool | None].

    Reads its own config slice (ctx.config); returning None declines
    registration for this run.
    """
    # Default False, not True: an entry-point plugin activates in EVERY raven
    # process in the environment (admission reads enabled_by_default only), so
    # scoping this engine to its own agent is this factory's job -- decline
    # unless the agent's rendered config slice explicitly enables it. The
    # scaffolded agent's config.json carries enabled: true; the host and
    # sibling agents carry no slice and get nothing.
    if not ctx.config.get("enabled", False):
        return None
    return HelloTool(str(ctx.config.get("greeting", "Hello")))
