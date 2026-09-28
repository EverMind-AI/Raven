"""Show every granted implementation entry without copying its authority."""

from ...harness import Declaration
from ..context.render import tool

NAME = "submit_selection"
REVISE = "revise_selection"


def output(declaration: Declaration, *, revise=False) -> dict:
    return tool(
        REVISE if revise else NAME,
        "Submit initial targets, task diagnosis, choice rationale and unresolved questions. "
        "Changing selection invalidates the current design and draft; a new design is required.",
        declaration.selection_schema(),
    )


def materials(declaration: Declaration) -> list[dict]:
    return [
        {
            "target": target.name,
            "binding": target.binding,
            "channels": target.channels,
            "roles": target.roles,
            "effect": target.effect,
            "phases": target.phases,
            "configuration_fields": list(target.schema().get("properties", {})),
            "state": [state.model_dump() for state in target.state],
        }
        for target in declaration.targets
    ]
