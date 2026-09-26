"""Develop selected entries into a checked mechanism before generating code."""

import json

from ...harness import Declaration, Plan
from ...harness.artifact import Selection
from ..context.collect import Context
from ..context.render import tool

NAME = "submit_plan"


def _decoded(value):
    """A list some models send as JSON text instead of a list; anything else is left to validation."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        return decoded if isinstance(decoded, list) else value
    return value


def materials(context: Context, selection: Selection) -> dict:
    """What design adds to the curation's materials: the current selection and its expanded contracts."""
    return {
        "selection": selection.model_dump(mode="json"),
        "selected_contracts": [context.declaration.target(name).describe() for name in selection.targets],
    }


def schema(declaration: Declaration, selection: Selection) -> dict:
    return declaration.restrict(selection.targets).plan_schema()


def output(declaration: Declaration) -> dict:
    """The design submission for the shared tool list; its schema admits any declared target."""
    return tool(
        NAME,
        "Submit a concrete design for exactly the selected targets, including effects and verification.",
        declaration.plan_schema(),
    )


def parse(declaration: Declaration, selection: Selection, arguments: dict) -> Plan:
    if isinstance(arguments, dict):
        arguments = {key: _decoded(value) if key in ("changes", "state") else value for key, value in arguments.items()}
    plan = declaration.parse_plan(arguments)
    if {change.target for change in plan.changes} != set(selection.targets):
        raise ValueError("design must cover exactly the current selection; revise selection first")
    return plan
