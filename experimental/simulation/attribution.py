"""Which owner rule each intervention of a sedimented mechanism enforces, read by a model from the reason it gave.

The value judge credits a rule with an intervention only when the intervention was for that rule (see
`experimental.simulation.value`). A mechanism's reasons are its own words and rarely cite a rule's number, so a model
reads each reason against the rules its target was sedimented for and names the rules it enforces; a block for another
rule, or a block of work the rule allows, enforces none. The result, `attribution.json` beside the records, is read
into the record; without it the judge falls back to rule ids and SOP markers named in the reasons.
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..curator.generation.context.render import tool
from ..curator.harness.declaration import schema_for
from ..iteration.exchange import exchange, messages
from .value import candidates

NAME = "submit_attribution"
RESULT = "attribution.json"
_PROMPT = Path(__file__).resolve().parent / "prompts" / "attribution.md"


class Link(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    rules: list[str] = Field(description="Ids of the rules this intervention enforces; empty when it enforces none.")


class Links(BaseModel):
    model_config = ConfigDict(extra="forbid")

    links: list[Link]


async def attribute(record: dict, provider, *, model=None, timeout=180) -> dict:
    """`links` maps each intervention reason (by `value.reason_key`) to the rules it enforces."""
    found = candidates(record)
    if not found:
        return {"links": {}, "interventions": 0}
    ids = {f"r{index}": key for index, key in enumerate(sorted(found), start=1)}
    asked = {rule for slot in found.values() for rule in slot["rules"]}
    packet = {
        "rules": [
            {"id": entry["criterion"], "rule": entry.get("rule", "")}
            for entry in record.get("ledger", [])
            if entry["criterion"] in asked
        ],
        "interventions": [
            {
                "id": number,
                "mechanism": found[key]["target"],
                "harness": found[key]["scope"],
                "reason": found[key]["reason"],
                "times": found[key]["count"],
                "may_enforce": sorted(found[key]["rules"]),
            }
            for number, key in ids.items()
        ],
    }

    def accept(arguments):
        parsed = Links.model_validate(arguments)
        if sorted(link.id for link in parsed.links) != sorted(ids):
            raise ValueError(f"give exactly one entry per intervention id: {sorted(ids)}")
        for link in parsed.links:
            allowed = found[ids[link.id]]["rules"]
            if set(link.rules) - allowed:
                raise ValueError(f"{link.id} may name only {sorted(allowed)}")
        return parsed

    _, parsed = await exchange(
        provider,
        messages(_PROMPT.read_text(), packet),
        [tool(NAME, "Submit, for every intervention id, the rules it enforces.", schema_for(Links))],
        submit={NAME: accept},
        model=model,
        max_calls=4,
        timeout=timeout,
        label="attribution",
    )
    return {"links": {ids[link.id]: sorted(link.rules) for link in parsed.links}, "interventions": len(ids)}


async def write(root: Path, provider, *, model=None, timeout=180) -> dict:
    """Attribute a finished run's interventions and keep the result beside its records."""
    from .record import build_record

    result = await attribute(build_record(Path(root)), provider, model=model, timeout=timeout)
    (Path(root) / RESULT).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
