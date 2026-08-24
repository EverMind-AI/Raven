"""Prompt-mode graph composition (raven/playbook/prompt.py).

The CLI path for a ``mode: prompt`` playbook -- ``raven playbook run`` --
composes a graph through a forced tool call. What is
guarded here is that the two things the model is handed agree with each other and
with the node model: the schema attached to the call, and the prose that tells it
which fields to write. They disagreed once, and the failure was quiet: the schema
carried no fields at all, so the prose was the only guidance, and it named a
field the node model forbids.
"""

from __future__ import annotations

import pytest

from raven.playbook.prompt import build_compose_prompt, compose_tool
from raven.playbook.types import NodeSpec


def _items() -> dict:
    return compose_tool()[0]["function"]["parameters"]["properties"]["nodes"]["items"]


def test_the_composition_call_carries_the_node_schema() -> None:
    """Found by following the reference, not by naming the definition.

    ``nodes`` is optional, so pydantic wraps it in ``anyOf`` and the ``$ref``
    sits a level down; and ``NodeSpec`` is an alias, so the definition is titled
    after the class it points at. A lookup by name at one level got both wrong
    and returned ``{}`` -- an empty ``items``, which is a schema that forbids
    nothing and describes nothing.
    """
    props = _items().get("properties") or {}

    assert props, "an empty items leaves the prose as the only field guidance"
    assert "subagent" in props and "promptTemplate" in props
    assert "agent" not in props, "the pre-rename name is not a field any more"


def test_the_prose_names_only_fields_the_node_model_accepts() -> None:
    """A model follows the prose. `extra="forbid"` then rejects what it wrote.

    The repair round that follows is handed the validation error and not its own
    submission, so "extra inputs are not permitted" arrives without the name it
    should have used -- one round, spent, and the run ends at "graph assembly
    failed". Which is why prose drifting from the schema is not cosmetic here.
    """
    text = build_compose_prompt("do a thing", {"research-raven": "digs"}, "topic")

    assert "subagent" in text
    assert "/ agent /" not in text and "Rules: agent " not in text

    # The invariant, stated once: every field the prose lists is a field the
    # schema attached to the same call accepts. Checked against the schema rather
    # than by constructing nodes, because that is the pair the model reads and
    # the pair that drifted.
    listed = [w.strip() for w in text.split("camelCase fields:", 1)[-1].split(")", 1)[0].split("/")]
    named = {w for w in listed if w and w.isidentifier()}
    accepted = set((_items().get("properties") or {}).keys())

    assert named, "the prose still lists the fields"
    assert named <= accepted, f"prose names fields the schema forbids: {sorted(named - accepted)}"


@pytest.mark.parametrize("field", ["agent"])
def test_the_old_spelling_is_refused_so_the_drift_cannot_be_silent(field: str) -> None:
    with pytest.raises(Exception, match="not permitted"):
        NodeSpec.model_validate({"id": "n", field: "research-raven", "promptTemplate": "x"})


def test_a_schema_with_no_node_reference_degrades_to_an_empty_shape() -> None:
    """What the fall-through yields, stated rather than discovered later.

    An unfindable reference means no field guidance on the call -- which is the
    state this whole seam existed in until now, and it was invisible because
    ``{}`` is a valid schema. Kept as a fall-through rather than an exception: a
    pydantic version that titles or nests things differently should not stop a
    playbook running. The test is here so the next person meets the behaviour in
    writing instead of in a model's output.
    """
    from raven.playbook.prompt import _node_schema_of

    assert _node_schema_of({"properties": {"nodes": {"type": "array"}}, "$defs": {"X": {"a": 1}}}) == {}
    assert _node_schema_of({}) == {}
    # A ref inside a list branch still resolves: `anyOf` is a list, which is
    # exactly how an optional `nodes` arrives.
    found = _node_schema_of(
        {
            "properties": {"nodes": {"anyOf": [{"items": {"$ref": "#/$defs/X"}}, {"type": "null"}]}},
            "$defs": {"X": {"a": 1}},
        }
    )
    assert found == {"a": 1}
