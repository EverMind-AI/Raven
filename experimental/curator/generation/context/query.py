"""Bounded read-only access to registered source, actual worker facts and the execution's observations."""

import json

from pydantic import BaseModel, ConfigDict, Field

from ...harness.declaration import schema_for
from .collect import Context
from .render import tool


class SourceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=12000, ge=1, le=24000)
    find: str | None = Field(default=None, min_length=1, max_length=160)


class FactQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    path: tuple[str | int, ...] = ()
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=12000, ge=1, le=24000)


class ObservationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    path: tuple[str | int, ...] = ()
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=12000, ge=1, le=24000)


def tools(context: Context) -> list[dict]:
    source = schema_for(SourceQuery)
    source["properties"]["name"]["enum"] = list(context.sources)
    fact = schema_for(FactQuery)
    fact["properties"]["name"]["enum"] = list(context.facts)
    return [
        tool(
            "read_source",
            "Read a registered native or generated source; optionally find literal text within it.",
            source,
        ),
        tool("read_fact", "Read a current worker fact section. Long sections can be read in bounded slices.", fact),
        tool(
            "read_observation",
            "Read one execution observation listed in `observations` by its index, optionally at a path inside it; "
            "long rows can be read in bounded slices.",
            schema_for(ObservationQuery),
        ),
    ]


def execute(context: Context, name: str, arguments: dict) -> dict:
    if name == "read_source":
        request = SourceQuery.model_validate(arguments)
        if request.name not in context.sources:
            raise ValueError(f"unknown registered source: {request.name}")
        return context.read_source(**request.model_dump())
    if name == "read_fact":
        request = FactQuery.model_validate(arguments)
        if request.name not in context.facts:
            raise ValueError(f"unknown fact section: {request.name}")
        value = context.facts[request.name]
        for key in request.path:
            value = value[key]
        text = json.dumps(value, ensure_ascii=False, indent=2)
        end = min(len(text), request.offset + request.length)
        if request.offset > len(text):
            raise ValueError("fact offset exceeds the available material")
        return {
            "fact": request.name,
            "text": text[request.offset : end],
            "total_characters": len(text),
            "next_offset": end if end < len(text) else None,
        }
    if name == "read_observation":
        request = ObservationQuery.model_validate(arguments)
        if request.index >= len(context.observations):
            raise ValueError(f"no observation has the index {request.index}; there are {len(context.observations)}")
        value = context.observations[request.index]
        for key in request.path:
            value = value[key]
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        if request.offset > len(text):
            raise ValueError("observation offset exceeds the available material")
        end = min(len(text), request.offset + request.length)
        return {
            "index": request.index,
            "text": text[request.offset : end],
            "total_characters": len(text),
            "next_offset": end if end < len(text) else None,
        }
    raise ValueError(f"unknown read-only query: {name}")
