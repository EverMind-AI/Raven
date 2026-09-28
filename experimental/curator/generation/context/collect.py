"""Keep task intent, host facts, previous expectations and observations distinct."""

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ...harness import Declaration, Plan


@dataclass(frozen=True)
class Context:
    task: str
    declaration: Declaration
    facts: dict[str, Any]
    sources: dict[str, dict]
    read_source: Callable[..., dict]
    feedback: Any = None
    observations: tuple[dict, ...] = ()
    previous_plan: Plan | None = None
    exploration: dict | None = None


def collect(
    task: str,
    declaration: Declaration,
    *,
    facts: Mapping[str, Any],
    sources: Mapping[str, dict],
    read_source: Callable[..., dict],
    feedback=None,
    observations: Sequence[dict] = (),
    previous_plan: Plan | None = None,
    exploration: dict | None = None,
) -> Context:
    if not task.strip():
        raise ValueError("generation requires the current task")
    return Context(
        task,
        declaration,
        deepcopy(dict(facts)),
        deepcopy(dict(sources)),
        read_source,
        deepcopy(feedback),
        tuple(deepcopy(observations)),
        previous_plan,
        deepcopy(exploration),
    )


def read_reference(context: Context, name: str) -> dict | None:
    """Deliver a registered reference completely, using its version-checked reader."""
    if name not in context.sources:
        return None
    pages = []
    offset = 0
    while True:
        page = context.read_source(name=name, offset=offset, length=24000)
        pages.append(page["text"])
        following = page.get("next_offset")
        if following is None:
            break
        if following <= offset:
            raise ValueError(f"reference reader did not advance: {name}")
        offset = following
    return {"source": name, "digest": context.sources[name].get("digest"), "text": "".join(pages)}
