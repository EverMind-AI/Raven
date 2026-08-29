"""The tool registry's door (tool specification): admit, check, dispense frozen.

The measured disease: 4 authored members on the paper, 13 consumed off the
live object — every consumer convenience widened the de-facto contract
silently. The door checks the authored four at registration and freezes the
data the registry serves afterwards; these tests are the door's bite-tests,
mutation audit included.
"""

from __future__ import annotations

import pytest

from raven.agent.tools.registry import ToolAdmissionError, ToolRegistry, admit_tool
from raven.contracts.tool import Tool


class _EchoTool(Tool):
    def __init__(self) -> None:
        self._parameters: dict = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo the text back."

    @property
    def parameters(self) -> dict:
        return self._parameters

    async def execute(self, **kwargs) -> str:
        return str(kwargs.get("text", ""))


class _Duck:
    """A non-Tool duck: the registry historically accepted anything shaped
    roughly right, so the door must judge shape, not inheritance."""

    name = "duck"
    description = "quacks"
    parameters: dict = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "quack"


def test_a_well_formed_duck_is_admitted():
    spec = admit_tool(_Duck())
    assert spec.name == "duck"
    assert spec.schema["function"]["name"] == "duck"
    assert spec.channels is None and spec.timeout_seconds is None


@pytest.mark.parametrize(
    "breakage,message",
    [
        (("name", ""), "usable name"),
        (("name", None), "usable name"),
        (("description", 7), "description"),
        (("parameters", "not-a-mapping"), "parameters"),
        (("execute", "not-callable"), "execute"),
    ],
)
def test_the_door_refuses_a_missing_or_misshapen_authored_member(breakage, message):
    duck = _Duck()
    setattr(duck, breakage[0], breakage[1])
    with pytest.raises(ToolAdmissionError, match=message):
        admit_tool(duck)


def test_refusal_happens_at_registration_not_mid_turn():
    duck = _Duck()
    duck.name = ""
    registry = ToolRegistry()
    with pytest.raises(ToolAdmissionError):
        registry.register(duck)
    assert len(registry) == 0


def test_the_advertised_schema_is_frozen_at_admission():
    """Mutation audit: growing or mutating the live object after registration
    must not leak into what the model is shown."""
    tool = _EchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    before = registry.get_definitions()

    tool._parameters["properties"]["sneaked"] = {"type": "string"}

    after = registry.get_definitions()
    assert after == before
    assert "sneaked" not in after[0]["function"]["parameters"]["properties"]


def test_unregister_drops_the_spec_with_the_body():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    registry.unregister("echo")
    assert registry.get_definitions() == []
    assert registry.get("echo") is None


async def test_execution_reads_the_admitted_timeout_and_runs_the_body():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    assert await registry.execute("echo", {"text": "hi"}) == "hi"


class _RosterTool(Tool):
    """A declared-dynamic tool: authoring to_schema is the signed escape from
    the admission freeze (the load_playbook pattern)."""

    def __init__(self) -> None:
        self.roster = ["a"]

    @property
    def name(self) -> str:
        return "roster"

    @property
    def description(self) -> str:
        return "Pick a roster entry."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"pick": {"type": "string", "enum": list(self.roster)}}}

    def to_schema(self) -> dict:
        return super().to_schema()

    async def execute(self, **kwargs) -> str:
        return "ok"


def test_an_authored_to_schema_is_served_live():
    """Declared dynamism: the override IS the declaration, so the registry
    serves the live schema — unlike the frozen default path."""
    tool = _RosterTool()
    registry = ToolRegistry()
    registry.register(tool)
    assert registry.get_definitions()[0]["function"]["parameters"]["properties"]["pick"]["enum"] == ["a"]

    tool.roster.append("b")

    assert registry.get_definitions()[0]["function"]["parameters"]["properties"]["pick"]["enum"] == ["a", "b"]
