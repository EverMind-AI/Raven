"""Compose shared rules, task materials, stage instructions and stage data, stable parts first."""

import json
from copy import deepcopy
from pathlib import Path
from string import Template

from raven.contracts.tool import RAW_ARGUMENTS_KEY
from raven.providers.tool_calls import openai_tool_call
from raven.utils.messages import build_assistant_message

_PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
_COMMON = ("common/background", "common/contracts", "common/materials", "common/interaction")


def tool(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def _prompt(name: str) -> str:
    return (_PROMPTS / f"{name}.md").read_text().strip()


def messages(
    stages: tuple[str, ...], materials: dict, stage_materials: dict, *, tools: list[dict], available: set[str]
) -> list[dict]:
    """A stage request: shared rules, the curation's materials, then this stage's instructions and its own data.

    The rules, the materials and the tool list are the same for every stage of one curation, so a provider's prefix
    cache serves them after the first request; a stage adds only its instructions (with the actions it may use) and
    its data (selection, plan, submission schemas, history) at the end. Host instructions and task data stay in
    separate messages.
    """
    scope = materials.get("worker", {}).get("scope", {}).get("kind")
    local = (f"composition/{scope}",) if scope in {"root", "child"} else ()
    offered = [entry for entry in tools if entry["function"]["name"] in available]
    actions = "\n".join(f"- `{entry['function']['name']}`: {entry['function']['description']}" for entry in offered)
    request = Template(_prompt("request")).substitute(stages=" → ".join(stages), actions=actions)
    return [
        {"role": "system", "content": "\n\n".join(_prompt(name) for name in (*_COMMON, *local))},
        {"role": "user", "content": json.dumps(materials, ensure_ascii=False, indent=2)},
        {"role": "user", "content": "\n\n".join((*(_prompt(name) for name in stages), request))},
        {"role": "user", "content": json.dumps(stage_materials, ensure_ascii=False, indent=2)},
    ]


def budget(*, calls: int, queries: int, checks: int, repairs: int) -> str:
    return (
        Template((_PROMPTS / "budget.md").read_text())
        .substitute(calls=calls, queries=queries, checks=checks, repairs=repairs)
        .strip()
    )


OBSERVATION_BYTES = 300_000
OBSERVATION_ROW_BYTES = 12_000


def observations(rows, *, budget=OBSERVATION_BYTES, row_bytes=OBSERVATION_ROW_BYTES) -> list:
    """Preflight observations as a `check_candidate` result returns them: rows in order, each cut to `row_bytes`,
    until `budget`.

    A check whose probe ran subagents can record megabytes of rows, past what a provider accepts in one request. A cut
    row keeps its scalar fields and the start of its JSON; rows past the budget are counted.
    """
    shown, used = [], 0
    for index, row in enumerate(rows):
        text = json.dumps(row, ensure_ascii=False, default=str)
        if len(text.encode()) > row_bytes:
            scalars = {
                key: value for key, value in row.items() if isinstance(value, (str, int, float, bool, type(None)))
            }
            head = text.encode()[: row_bytes // 2].decode(errors="ignore")
            row = {
                **{key: value for key, value in scalars.items() if len(str(value)) < 400},
                "cut": len(text.encode()),
                "start": head,
            }
            text = json.dumps(row, ensure_ascii=False, default=str)
        if used + len(text.encode()) > budget:
            shown.append({"kind": "observations.omitted", "rows": len(rows) - index})
            break
        shown.append(row)
        used += len(text.encode())
    return shown


INDEX_BYTES = 1_000_000


def observation_index(rows) -> list | dict:
    """What a request carries of the execution's observations: one entry per row to choose from, read on demand.

    Each entry gives the row's index, its short scalar fields, its size and the kinds of records nested in it; the
    rows themselves are read with `read_observation`. Only when the index alone would outgrow a request is it
    reduced to counts per kind.
    """
    index = []
    for number, row in enumerate(rows):
        entry = {"index": number, "bytes": len(json.dumps(row, ensure_ascii=False, default=str).encode())}
        for key, value in row.items():
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 120:
                entry[key] = value
            elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                kinds = {}
                for item in value:
                    kinds[str(item.get("kind"))] = kinds.get(str(item.get("kind")), 0) + 1
                entry[f"{key}_kinds"] = kinds
        index.append(entry)
    if len(json.dumps(index, ensure_ascii=False).encode()) <= INDEX_BYTES:
        return index
    kinds = {}
    for row in rows:
        kinds[str(row.get("kind"))] = kinds.get(str(row.get("kind")), 0) + 1
    return {"rows": len(rows), "kinds": kinds, "read": "read_observation by index"}


def trailing(messages: list[dict], note: str) -> list[dict]:
    """A request copy ending with `note` as its own user message.

    Per-call notes such as the remaining budget go last so every earlier token stays a stable prefix the provider
    can serve from its prompt cache; a note inside the system message would invalidate everything after it. Keeping
    it out of tool results also keeps host instructions apart from untrusted tool output.
    """
    return [*deepcopy(messages), {"role": "user", "content": note}]


def assistant_message(response) -> dict:
    calls = []
    for call in response.tool_calls:
        payload = openai_tool_call(call)
        if RAW_ARGUMENTS_KEY in call.arguments:
            raw = call.arguments[RAW_ARGUMENTS_KEY]
            payload["function"]["arguments"] = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        calls.append(payload)
    return build_assistant_message(
        response.content or "",
        tool_calls=calls,
        reasoning_content=response.reasoning_content,
        thinking_blocks=response.thinking_blocks,
    )
