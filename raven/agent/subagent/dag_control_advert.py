"""Advertisement text for the DAG control tools, which are hidden from the
provider schema and reach the model only through another tool's result text.

Rendering lives in its own module because ``dag_control_tools`` imports
``dag_tool``, so neither of the two modules that need this text can own it.

What is rendered is the tool's real definition, straight from
``ToolRegistry.hidden_definition`` -- never a hand-written retelling of it. The
three prose copies that preceded this drifted from the tools in the ways prose
does: one named ``decision``, one named no arguments at all, and the model that
guessed ``action`` instead lost a call to it three runs running (2026-09-03/04).
"""

from __future__ import annotations

import copy
import json
from typing import Any

_NODE_SHAPE_REF = {
    "type": "object",
    "description": (
        "Same node shape as the `nodes` field of run_subagent_dag. Every field it declares "
        "applies here unchanged. Read it from your tool list, or, if run_subagent_dag is not "
        "in it, from tool_search for `run_subagent_dag`."
    ),
}
"""What replaces the inlined node schema in ``resolve_dag_node``'s replan field.

Carried in ``description`` rather than ``$comment``: JSON Schema defines the
latter as a note to developers that implementations MUST NOT present, so a
provider that normalizes the schema is entitled to drop it -- which would leave
``items`` an empty object and the pointer nowhere.

``run_subagent_dag`` is not schema-hidden, so re-sending its node shape here
costs ~845 tokens to repeat something the model can reach. What it cannot do is
promise the shape is *resident*: ``DEFAULT_ALWAYS_VISIBLE`` deliberately omits
the graph tool, on the grounds that a fan-out of minute-scale runs is not a
per-turn primitive, so above the compaction threshold it is cataloged rather
than offered. The pointer therefore names both routes. Collapse this while the
graph tool is schema-hidden too and the shape would be nowhere.
"""


def render(definition: dict[str, Any] | None) -> str | None:
    """One definition as JSON, the way every unhidden tool already reaches the model.

    ``None`` passes through, so a caller can hand over whatever
    ``hidden_definition`` gave it without branching first.
    """
    if not definition:
        return None
    return json.dumps(_without_inlined_node_shape(definition))


def _without_inlined_node_shape(definition: dict[str, Any]) -> dict[str, Any]:
    """Replace an inlined graph-node schema with a reference to the graph tool's.

    Copied before editing: ``hidden_definition`` may hand back the admitted
    snapshot, and a caller that edited it in place would change what every later
    reader of the schema sees.
    """
    out = copy.deepcopy(definition)
    nodes = out.get("function", {}).get("parameters", {}).get("properties", {}).get("nodes")
    if isinstance(nodes, dict) and isinstance(nodes.get("items"), dict):
        nodes["items"] = dict(_NODE_SHAPE_REF)
    return out


__all__ = ["render"]
