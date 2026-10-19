"""How the host lays out the two tools that hand work to sub-agents.

``spawn`` and ``run_subagent_dag`` pick from one roster, and it used to be
rendered into both descriptions -- 566 tokens each on a real install, paid twice
on every model call. It is rendered once now, as the end of the tool array:
``spawn`` is listed last and its description ends with the roster, and
``run_subagent_dag``, listed just before it, points there. With ``spawn``
switched off the graph tool is the one left to say it, so it does.

Asserted through the assembled array, because that is what a turn sends. The
halves have their own files: ``test_tool_registry_order.py`` for the placement,
the spawn and DAG description tests for the wording.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.agent.loop.main import AgentLoop
from raven.agent.subagent.backends import format_agent_listing
from raven.config.schema import ThirdPartyCliSubagentConfig
from tests._wiring import wire


class _StubProvider:
    """Construction reads the default model; no turn is ever run here."""

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs):  # pragma: no cover - never invoked
        raise NotImplementedError


def _write_switches(path: Path, names: list[str]) -> None:
    path.write_text(json.dumps({"tools": {"disabledTools": names}}), encoding="utf-8")


def _loop(tmp_path: Path, monkeypatch, *, disabled: list[str] | None = None) -> AgentLoop:
    cfg = tmp_path / "config.json"
    _write_switches(cfg, disabled or [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    agents = [
        ThirdPartyCliSubagentConfig(
            name="Scribe", command="scribe {prompt}", description='turns a "source" document into a deck'
        ),
        ThirdPartyCliSubagentConfig(name="Surveyor", command="survey {prompt}", description="reads the live web"),
    ]
    return AgentLoop(provider=_StubProvider(), workspace=tmp_path / "ws", **wire(agents=agents))


def _functions(loop: AgentLoop) -> list[dict]:
    return [d["function"] for d in loop.tools.get_definitions()]


def _roster(loop: AgentLoop) -> str:
    roster = format_agent_listing(loop.subagents.list_agents())
    assert "Scribe" in roster and "Surveyor" in roster
    return roster


def _times_in_the_array(loop: AgentLoop, text: str) -> int:
    # Counted in the serialized array, the form a provider is sent, so a copy
    # hiding in a parameter description counts as much as one in a description.
    serialized = json.dumps(loop.tools.get_definitions(), ensure_ascii=False)
    return serialized.count(json.dumps(text, ensure_ascii=False)[1:-1])


class TestTheRosterIsSaidOnceAtTheEnd:
    def test_the_two_dispatch_tools_close_the_array(self, tmp_path: Path, monkeypatch) -> None:
        names = [fn["name"] for fn in _functions(_loop(tmp_path, monkeypatch))]

        assert names[-2:] == ["run_subagent_dag", "spawn"]

    def test_spawn_s_description_ends_with_it(self, tmp_path: Path, monkeypatch) -> None:
        loop = _loop(tmp_path, monkeypatch)

        assert _functions(loop)[-1]["description"].endswith(_roster(loop) + ".")

    def test_nothing_else_in_the_array_repeats_it(self, tmp_path: Path, monkeypatch) -> None:
        loop = _loop(tmp_path, monkeypatch)

        assert _times_in_the_array(loop, _roster(loop)) == 1

    def test_the_graph_tool_points_there_and_its_enum_still_names_every_agent(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        loop = _loop(tmp_path, monkeypatch)
        dag = _functions(loop)[-2]

        assert "the roster at the end of the `spawn` tool's description" in dag["description"]
        node = dag["parameters"]["properties"]["nodes"]["items"]["properties"]
        assert set(node["subagent"]["enum"]) == set(loop.subagents.registry.names())


class TestWithSpawnSwitchedOffTheGraphToolSaysItItself:
    def test_the_graph_tool_closes_the_array_with_the_roster(self, tmp_path: Path, monkeypatch) -> None:
        loop = _loop(tmp_path, monkeypatch, disabled=["spawn"])
        functions = _functions(loop)

        assert "spawn" not in [fn["name"] for fn in functions]
        assert functions[-1]["name"] == "run_subagent_dag"
        assert functions[-1]["description"].endswith(_roster(loop) + ".")
        assert _times_in_the_array(loop, _roster(loop)) == 1

    def test_and_points_again_once_spawn_is_back(self, tmp_path: Path, monkeypatch) -> None:
        """Asked per assembly, like the switch itself: a graph tool that kept the
        roster after spawn came back would put the second copy back on every call."""
        loop = _loop(tmp_path, monkeypatch, disabled=["spawn"])
        _write_switches(tmp_path / "config.json", [])

        functions = _functions(loop)

        assert functions[-1]["name"] == "spawn"
        assert _times_in_the_array(loop, _roster(loop)) == 1
