"""The playbook contract, per the agreed field definition (playbook.md, three
regions: a two-field frontmatter, a human-readable body, one fenced
``yaml playbook-spec`` block holding every machine field).

Naming: python attributes are snake_case; the on-disk block is camelCase
(``promptTemplate`` / ``dependsOn``), handled by field aliases — dump with
``by_alias=True`` to get the wire shape.

Every field is a contract field: exactly what the field definition allows
inside ``playbook.md``. There is no lifecycle state here — a generator's
assumptions and open questions go into the human body (the
``## Open questions`` section), and whether a playbook is matchable on this
machine lives in config (``playbooks.disabled``), because the file is the
distribution unit and local state must not travel with it.

The two modes differ only in where the graph comes from: ``dag`` ships it in
``nodes``; ``prompt`` ships assembly guidance in ``prompts`` and a model
composes a graph at run time, which must pass the same validation before the
same execution chain runs it.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from raven.agent.subagent.dag_graph import DagNodeSpec

SPEC_VERSION = 1

NAME_RE = r"^[a-z0-9][a-z0-9-]*$"
"""The one field the whole on-disk layout hangs off: a playbook's name is its
directory name, so everything that writes must hold values to this shape --
the schema alone cannot (``model_copy`` skips validators, and the tool
argument validator has no ``pattern`` support)."""
_NAME_RE = NAME_RE
_NODE_ID_RE = r"^[A-Za-z0-9_-]+$"
_SLUG_RE = re.compile(r"[^a-z0-9]+")

NodeSpec = DagNodeSpec
"""One step of the graph -- the DAG's own node model, not a second definition.

A playbook's ``nodes[]`` used to be declared here, and the two drifted: neither
was a superset (this one had ``skills`` / ``mcps`` / ``confirm``, the DAG's had
``inputs``), the step's target field was spelled ``agent`` here and ``subagent``
there,
and a field a playbook could write but no graph could carry was silently dropped
at dispatch. The subset discipline the two are supposed to have -- a playbook's
fields are a subset of a graph's -- is now structural rather than a rule someone
has to remember.

The camelCase wire spelling is unchanged: ``DagNodeSpec`` carries the camel alias
generator, so ``promptTemplate`` in a playbook file and ``prompt_template`` in the
model-facing schema both validate.

Node-level ``confirm`` is gone with the merge, and deliberately: the field's only
effect was to be rejected by validation, because honouring it needs a runner that
can pause mid-graph. The gate is graph-level (``PlaybookSpec.confirm``, dispatched
as ``SubAgentDagSpec.confirm``), where "approve this" means the whole graph.
"""


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or "playbook"


class CamelBase(BaseModel):
    """Wire shape is camelCase; python stays snake_case."""

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class Triggers(CamelBase):
    """Retrieval vocabulary: normalized substring matching; words and phrases
    share one list and affect visibility, never automatic execution."""

    keywords: list[str] = Field(min_length=1)


class ParamSpec(CamelBase):
    """One runtime input, keyed by its name in ``PlaybookSpec.params``.

    ``description`` is required because it guides the model's extraction,
    supplies the missing-param follow-up wording and documents the stored
    procedure."""

    type: Literal["string", "integer", "number", "boolean", "enum", "path"] = "string"
    required: bool = False
    default: Any = None
    enum: list[str] | None = None
    description: str

    @model_validator(mode="after")
    def _enum_needs_values(self) -> "ParamSpec":
        if self.type == "enum" and not self.enum:
            raise ValueError("type 'enum' requires a non-empty 'enum' list")
        if self.type != "enum" and self.enum:
            raise ValueError(f"'enum' given but type is {self.type!r}")
        return self


class PlaybookSpec(CamelBase):
    """One whole playbook. ``name``/``description`` live in the frontmatter,
    the rest in the fenced block (see :meth:`block_dump` / the store)."""

    FRONTMATTER_FIELDS: ClassVar[tuple[str, ...]] = ("name", "description")

    name: str = Field(pattern=_NAME_RE)
    """Unique id; equals the directory name."""

    description: str = Field(min_length=1, max_length=200)
    """One-line intent, written for retrieval ("when to use me")."""

    task_summary: str = Field(
        min_length=1,
        description=(
            "A short title for what running this playbook dispatches -- the length of a "
            "chat title, under ten words, not a sentence and not a summary. Distinct from "
            "`description`, which is matched against to decide whether to run the "
            "playbook at all."
        ),
    )
    """A short title, for the user, naming what running this playbook dispatches.
    Distinct from ``description``, which is matched against to decide whether to
    run it at all."""

    version: int = SPEC_VERSION
    mode: Literal["dag", "prompt"]
    confirm: bool = True
    triggers: Triggers
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    nodes: list[NodeSpec] | None = None
    prompts: str | None = None
    """prompt mode: how to assemble the graph (layers, agents, per-node
    skills/mcps, dependencies) — never runtime decision rules; a composed
    graph is fixed once assembled."""

    @model_validator(mode="after")
    def _mode_section_pairing(self) -> "PlaybookSpec":
        if self.mode == "dag":
            if not self.nodes:
                raise ValueError("mode 'dag' requires non-empty nodes")
            if self.prompts:
                raise ValueError("mode 'dag' must not carry prompts")
        else:
            if not (self.prompts or "").strip():
                raise ValueError("mode 'prompt' requires non-empty prompts")
            if self.nodes:
                raise ValueError("mode 'prompt' must not carry nodes")
        return self

    @model_validator(mode="after")
    def _param_names_are_identifiers(self) -> "PlaybookSpec":
        for pname in self.params:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", pname):
                raise ValueError(f"param name {pname!r} must be an identifier")
        return self

    def block_dump(self) -> dict[str, Any]:
        """The ``yaml playbook-spec`` block content: machine fields only,
        camelCase; frontmatter fields excluded."""
        data = self.model_dump(by_alias=True, exclude_none=True)
        for key in self.FRONTMATTER_FIELDS:
            data.pop(key, None)
        return data
