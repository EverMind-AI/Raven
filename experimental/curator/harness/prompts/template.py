"""Load one prompt resource and render validated inputs without recursive expansion."""

import json
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Generic, TypeVar

from pydantic import BaseModel

from ..artifact import relative_path
from ..declaration import schema_for, typed

InputT = TypeVar("InputT", bound=BaseModel)


@dataclass(frozen=True)
class Prompt(Generic[InputT]):
    """Immutable template reference and its one authoritative input model.

    Consumers decide when to render and where to send the text. A Prompt does
    not call a model, alter strategy state or choose a message role.
    """

    path: Path
    inputs: type[InputT]
    template: str

    def __post_init__(self):
        if (
            not isinstance(self.inputs, type)
            or not issubclass(self.inputs, BaseModel)
            or getattr(self.inputs, "__pydantic_root_model__", False)
        ):
            raise TypeError("Prompt inputs must be a named-field Pydantic model")
        template = Template(self.template)
        if not template.is_valid():
            raise ValueError("invalid prompt placeholder")
        unknown = set(template.get_identifiers()) - self.inputs.model_fields.keys()
        if unknown:
            raise ValueError(f"prompt variables lack input fields: {sorted(unknown)}")
        schema_for(self.inputs)

    @classmethod
    def from_file(cls, module_file: str, relative: str, inputs: type[InputT]) -> "Prompt[InputT]":
        """Load UTF-8 text relative to the declaring module, never the process cwd."""
        path = (Path(module_file).resolve().parent / relative_path(relative)).resolve()
        return cls(path, inputs, path.read_text(encoding="utf-8"))

    def render(self, values: InputT | dict) -> str:
        """Strictly revalidate input, JSON-render nonstrings and substitute once.

        Literal dollars in templates use $$. Dollars in input values remain
        literal data; missing or invalid inputs are errors, not empty output.
        """
        checked = typed(self.inputs, values)
        fields = checked.model_dump(mode="json", by_alias=False)
        return Template(self.template).substitute(
            {
                name: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
                for name, value in fields.items()
            }
        )

    def describe(self) -> dict:
        return {
            "path": str(self.path),
            "input_schema": schema_for(self.inputs),
            "variables": Template(self.template).get_identifiers(),
        }
