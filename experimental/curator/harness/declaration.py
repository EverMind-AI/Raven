"""One host declaration supplies model schemas, grants and payload checks."""

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from inspect import getdoc, getsource, iscoroutinefunction, signature
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model
from pydantic.json_schema import GenerateJsonSchema

from .artifact import Artifact, Candidate, Plan, Selection
from .state import StateUse


class _ClosedModels(GenerateJsonSchema):
    """Match the extra='forbid' policy used at every model input boundary."""

    def model_schema(self, schema):
        result = super().model_schema(schema)
        if not schema.get("root_model"):
            result["additionalProperties"] = False
        return result


def schema_for(annotation: Any) -> dict[str, Any]:
    return TypeAdapter(annotation).json_schema(by_alias=False, schema_generator=_ClosedModels)


def _prune_definitions(schema: dict[str, Any]) -> dict[str, Any]:
    def references(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/$defs/"):
                yield ref.removeprefix("#/$defs/")
            for child in value.values():
                yield from references(child)
        elif isinstance(value, list):
            for child in value:
                yield from references(child)

    definitions = schema.get("$defs", {})
    result = {key: value for key, value in schema.items() if key != "$defs"}
    pending = list(references(result))
    kept = {}
    while pending:
        name = pending.pop()
        if name in kept or name not in definitions:
            continue
        kept[name] = definitions[name]
        pending.extend(references(definitions[name]))
    if kept:
        result["$defs"] = kept
    return result


def parse_as(annotation: Any, value: Any, *, strict: bool = False) -> Any:
    return TypeAdapter(annotation).validate_python(value, extra="forbid", strict=strict, by_alias=False, by_name=True)


def typed(annotation, value):
    """Revalidate instance contents as well as ordinary JSON inputs."""
    data = TypeAdapter(type(value)).dump_json(value, warnings="error")
    return TypeAdapter(annotation).validate_json(data, strict=True, extra="forbid", by_alias=False, by_name=True)


@dataclass(frozen=True)
class Target:
    """A host authoring entry; a catalogue entry alone is not a worker grant."""

    name: str
    contract: type | Callable[..., Any]
    binding: str
    payload: Any
    channels: tuple[str, ...]
    effect: str
    roles: tuple[str, ...] = ()
    phases: tuple[str, ...] = ()
    result: Any = None
    fields: tuple[str, ...] | None = None
    state: tuple[StateUse, ...] = ()
    knowledge: tuple[type | Callable[..., Any] | Path, ...] = ()

    def schema(self) -> dict[str, Any]:
        return _prune_definitions(self.project_schema(schema_for(self.payload)))

    def project_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        if self.fields is None:
            return schema
        if "properties" not in schema:
            raise ValueError(f"{self.name}: field restrictions require a native model with named fields")
        properties = schema["properties"]
        unknown = set(self.fields) - properties.keys()
        missing = set(schema.get("required", ())) - set(self.fields)
        if unknown or missing:
            raise ValueError(
                f"{self.name}: invalid field restriction; unknown={sorted(unknown)}, required={sorted(missing)}"
            )
        return {**schema, "properties": {key: value for key, value in properties.items() if key in self.fields}}

    def parse(self, value: Any) -> Any:
        if self.fields is not None:
            if not isinstance(value, Mapping):
                raise ValueError(f"{self.name}: a field-restricted payload must be an object")
            unknown = value.keys() - set(self.fields)
            if unknown:
                raise ValueError(f"{self.name}: fields not granted: {sorted(unknown)}")
        return parse_as(self.payload, value)

    def parse_result(self, value: Any, *, phase: str | None = None) -> Any:
        if self.result is None:
            raise ValueError(f"{self.name}: no participant result contract")
        if self.phases and phase not in self.phases:
            raise ValueError(f"{self.name}: phase not granted: {phase}")
        if isinstance(value, BaseModel):
            value = dict(value)
        parsed = parse_as(self.result, value, strict=True)
        if isinstance(parsed, BaseModel):
            if type(parsed).__pydantic_root_model__:
                return parsed.root
            return {key: getattr(parsed, key) for key in parsed.model_fields_set}
        return parsed

    def describe(self) -> dict[str, Any]:
        contract = f"{self.contract.__module__}:{self.contract.__qualname__}"
        return {
            "target": self.name,
            "contract": contract,
            "documentation": getdoc(self.contract) or "",
            "signature": str(signature(self.contract)) if not isinstance(self.contract, type) else None,
            "coroutine": iscoroutinefunction(self.contract),
            "binding": self.binding,
            "schema": self.schema(),
            "channels": list(self.channels),
            "roles": list(self.roles),
            "effect": self.effect,
            "phases": list(self.phases),
            "result_schema": schema_for(self.result) if self.result is not None else None,
            "state": [state.model_dump() for state in self.state],
            "knowledge": [
                {
                    "source": str(item) if isinstance(item, Path) else f"{item.__module__}:{item.__qualname__}",
                    "content": item.read_text() if isinstance(item, Path) else getsource(item),
                }
                for item in self.knowledge
            ],
        }


@dataclass(frozen=True)
class Declaration:
    """Authoring entries explicitly supplied by the host for one baseline."""

    baseline: str
    targets: tuple[Target, ...]

    def __post_init__(self) -> None:
        if not self.baseline:
            raise ValueError("a declaration requires its host baseline identity")
        names = [target.name for target in self.targets]
        if len(names) != len(set(names)):
            raise ValueError("a target must be declared once")
        for target in self.targets:
            if not target.name or not target.binding:
                raise ValueError("every granted target needs a name and native binding")
            target.schema()

    @property
    def contract_id(self) -> str:
        content = json.dumps(self.describe(), sort_keys=True, ensure_ascii=False)
        return sha256(content.encode()).hexdigest()

    def target(self, name: str) -> Target:
        for target in self.targets:
            if target.name == name:
                return target
        raise ValueError(f"target not granted: {name}")

    def restrict(
        self,
        names: Iterable[str],
        *,
        fields: Mapping[str, Iterable[str]] | None = None,
        phases: Mapping[str, Iterable[str]] | None = None,
    ) -> "Declaration":
        """Apply a manual by removing targets, fields or phases, never by adding them."""
        selected = tuple(names)
        field_limits = {key: tuple(value) for key, value in (fields or {}).items()}
        phase_limits = {key: tuple(value) for key, value in (phases or {}).items()}
        if (field_limits.keys() | phase_limits.keys()) - set(selected):
            raise ValueError("restrictions must refer to selected targets")
        narrowed = []
        for name in selected:
            target = self.target(name)
            if name in field_limits:
                allowed = target.schema().get("properties", {})
                if set(field_limits[name]) - allowed.keys():
                    raise ValueError(f"{name}: a manual cannot add fields")
                target = replace(target, fields=field_limits[name])
            if name in phase_limits:
                allowed = phase_limits[name]
                if not allowed or set(allowed) - set(target.phases):
                    raise ValueError(f"{name}: a manual cannot add phases or remove every phase")
                target = replace(target, phases=allowed)
            narrowed.append(target)
        return Declaration(self.baseline, tuple(narrowed))

    def describe(self) -> list[dict[str, Any]]:
        return [target.describe() for target in self.targets]

    def selection_schema(self) -> dict[str, Any]:
        schema = schema_for(Selection)
        if self.targets:
            schema["properties"]["targets"]["items"]["enum"] = [target.name for target in self.targets]
        else:
            schema["properties"]["targets"]["maxItems"] = 0
        return schema

    def parse_selection(self, value: Any) -> Selection:
        selection = Selection.model_validate(value)
        for name in selection.targets:
            self.target(name)
        return selection

    def plan_schema(self) -> dict[str, Any]:
        schema = schema_for(Plan)
        if self.targets:
            change_ref = schema["properties"]["changes"]["items"]["$ref"]
            change_schema = schema["$defs"][change_ref.rsplit("/", 1)[-1]]
            change_schema["properties"]["target"]["enum"] = [target.name for target in self.targets]
        else:
            schema["properties"]["changes"]["maxItems"] = 0
        return schema

    def parse_plan(self, value: Any) -> Plan:
        if isinstance(value, Plan):
            value = value.model_dump()
        plan = Plan.model_validate(value)
        for change in plan.changes:
            self.target(change.target)
        return plan

    def _selected(self, plan: Plan) -> tuple[Target, ...]:
        return tuple(self.target(change.target) for change in self.parse_plan(plan).changes)

    def artifact_schema(self, plan: Plan) -> dict[str, Any]:
        """Project native payload schemas into exactly the targets selected by the plan."""
        model = self._artifact_model(plan)
        schema = schema_for(model)
        values_ref = schema["properties"]["values"]["$ref"]
        values = schema["$defs"][values_ref.rsplit("/", 1)[-1]]
        for target in self._selected(plan):
            if target.fields is not None:
                prop = values["properties"][target.name]
                ref = prop.get("$ref")
                original = schema["$defs"][ref.rsplit("/", 1)[-1]] if ref else prop
                values["properties"][target.name] = target.project_schema(original)
        schema["properties"]["remove"]["uniqueItems"] = True
        schema["allOf"] = [
            {
                "oneOf": [
                    {
                        "properties": {
                            "values": {"required": [target.name]},
                            "remove": {"not": {"contains": {"const": target.name}}},
                        }
                    },
                    {
                        "required": ["remove"],
                        "properties": {
                            "values": {"not": {"required": [target.name]}},
                            "remove": {"contains": {"const": target.name}},
                        },
                    },
                ]
            }
            for target in self._selected(plan)
        ]
        content = [target.name for target in self._selected(plan) if target.binding.endswith("_files")]
        if content:
            schema["properties"]["remove_paths"]["propertyNames"] = {"enum": content}
        else:
            schema["properties"]["remove_paths"]["maxProperties"] = 0
        if not schema["allOf"]:
            schema.pop("allOf")
        if plan.changes:
            schema["properties"]["remove"]["items"]["enum"] = [target.name for target in self._selected(plan)]
        if not plan.changes:
            schema["properties"]["remove"]["maxItems"] = 0
        return _prune_definitions(schema)

    def _artifact_model(self, plan: Plan) -> type[Artifact]:
        selected = self._selected(plan)
        value_fields: dict[str, Any] = {
            target.name: (target.payload, Field(default_factory=lambda: None)) for target in selected
        }
        values = create_model("SelectedValues", __config__=ConfigDict(extra="forbid"), **value_fields)
        files = Artifact.model_fields["files"]
        file_field = files if selected else Field(default_factory=dict, max_length=0)
        return create_model(
            "SelectedArtifact",
            __base__=Artifact,
            values=(values, Field(description=Artifact.model_fields["values"].description)),
            files=(files.annotation, file_field),
        )

    def accept(self, plan: Plan, value: Any) -> Candidate:
        """Attach host identity after checking selection, native values and supporting paths."""
        plan = self.parse_plan(plan)
        if isinstance(value, Artifact):
            value = value.model_dump()
        if isinstance(value, Mapping):
            misplaced = set(value) & {target.name for target in self._selected(plan)}
            if misplaced:
                locations = ", ".join(f"values[{name!r}]" for name in sorted(misplaced))
                raise ValueError(
                    f"These targets are selected and permitted but are at the wrong artifact level: {sorted(misplaced)}. "
                    f"Put their payloads at {locations}. "
                    f"The artifact's top-level fields are {list(Artifact.model_fields)}."
                )
        parsed = parse_as(self._artifact_model(plan), value)
        normalized = parsed.model_dump(mode="json", exclude_unset=True)
        artifact = Artifact.model_validate(normalized)
        selected = {target.name for target in self._selected(plan)}
        if set(artifact.values) | set(artifact.remove) != selected:
            raise ValueError("supply a value or explicit removal for exactly each selected target")
        for name in artifact.remove_paths:
            if not self.target(name).binding.endswith("_files"):
                raise ValueError("path retirement is available only for content targets")
        for name, value in artifact.values.items():
            self.target(name).parse(value)
        return Candidate(baseline=self.baseline, contract_id=self.contract_id, plan=plan, artifact=artifact)

    def validate(self, candidate: Candidate) -> Candidate:
        if candidate.baseline != self.baseline:
            raise ValueError("candidate baseline does not match the current declaration")
        if candidate.contract_id != self.contract_id:
            raise ValueError("candidate contract does not match the current declaration")
        return self.accept(candidate.plan, candidate.artifact)
