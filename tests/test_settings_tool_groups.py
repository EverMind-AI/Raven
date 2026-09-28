"""The settings page's tool groups cover the tools the agent loop registers.

``ui-web/src/features/settings/toolGroups.json`` is the page's table: eight
groups by tool name, read by the Tools page. A tool the loop registers and the
table omits would have no switch anywhere, so the table is diffed here against
a loop built with every switchable tool the bare wiring can register.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.tools.media_gen import ImageGenerateTool, SpeechGenerateTool, VideoGenerateTool
from raven.config.schema import ToolSearchConfig
from raven.providers.base import LLMProvider, LLMResponse

GROUPS = Path(__file__).resolve().parents[1] / "ui-web" / "src" / "features" / "settings" / "toolGroups.json"

# Tools the loop registers only once something outside a bare loop is wired:
# a cron service, the playbook engine, the multimodal memory section, a
# deliverables sink. Each is a tool name the page still has to offer a switch
# for, and the set is pinned so a tool that stops registering shows up here.
GATED = {"cron", "load_playbook", "create_playbook", "understand_media", "deliver_files"}


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **kwargs) -> LLMResponse:
        return LLMResponse(content="")

    def get_default_model(self) -> str:
        return "stub"

    @property
    def name(self) -> str:
        return "stub"


def _listed() -> dict[str, list[str]]:
    return json.loads(GROUPS.read_text(encoding="utf-8"))


def test_every_tool_is_in_exactly_one_group() -> None:
    groups = _listed()
    names = [t for tools in groups.values() for t in tools]
    assert len(names) == len(set(names)), "a tool sits in two groups"
    assert set(groups) == {"file", "run", "net", "generate", "collab", "skills", "memory", "search"}


def test_tool_groups_cover_the_default_tools() -> None:
    with tempfile.TemporaryDirectory() as td:
        loop = AgentLoop(
            provider=_StubProvider(),
            workspace=Path(td),
            model="stub",
            policy=TurnPolicy(max_iterations=2),
            tools=ToolWiring(
                restrict_to_workspace=True,
                search_api_key="test-serper-key",
                jina_api_key="test-jina-key",
                image_search=True,
                # Opt-in like image_search: without the flag the loop cannot
                # register the tool, and an ungrouped one would pass unseen.
                connection_add=True,
                tool_search_config=ToolSearchConfig(enabled=True),
            ),
            engine=EngineWiring(),
        )
        registered = set(loop.tools.names())
    listed = {t for tools in _listed().values() for t in tools}
    assert registered <= listed, f"registered but ungrouped: {sorted(registered - listed)}"
    assert listed - registered == GATED, f"grouped but never registered: {sorted(listed - registered - GATED)}"
    # The media tools register on a bare loop under the names the table uses.
    assert {ImageGenerateTool.name, SpeechGenerateTool.name, VideoGenerateTool.name} <= registered
