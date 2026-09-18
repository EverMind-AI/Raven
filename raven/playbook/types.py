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

from pydantic import Field, model_validator

from raven.agent.subagent.dag_graph import DagNodeSpec
from raven.config.schema import MCPServerConfig
from raven.playbook.base import CamelBase
from raven.playbook.stint_spec import MemoryEntry, RoleEntry, StopSpec, VerifyEntry
from raven.utils.paths import mint_slug

SPEC_VERSION = 1

NAME_RE = r"^[a-z0-9][a-z0-9-]*$"
"""The one field the whole on-disk layout hangs off: a playbook's name is its
directory name, so everything that writes must hold values to this shape --
the schema alone cannot (``model_copy`` skips validators, and the tool
argument validator has no ``pattern`` support)."""
_NAME_RE = NAME_RE
MCP_SERVER_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
"""A carried server's name is a segment of its credential path
(``<credentials>/playbooks/<playbook>/mcp/<server>.json``), so it is admitted
only as a plain filename: no separators, not dot-only, not leading with a dot."""
_NODE_ID_RE = r"^[A-Za-z0-9_-]+$"

NodeSpec = DagNodeSpec
"""One step of the graph -- the DAG's own node model, not a second definition.

``NodeSpec`` is ``DagNodeSpec``, so a playbook's node fields are a subset of a
graph's by construction rather than a rule someone
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
    slug = mint_slug(name)
    return slug or "playbook"


class Triggers(CamelBase):
    """Retrieval vocabulary: normalized substring matching; words and phrases
    share one list and affect visibility, never automatic execution."""

    keywords: list[str] = Field(min_length=1)


class ParamSpec(CamelBase):
    """One runtime input, keyed by its name in ``PlaybookSpec.params``.

    ``description`` is required because it guides the model's extraction,
    supplies the missing-param follow-up wording and documents the stored
    procedure.

    ``type: secret`` is the one type that constrains where the *value* may go
    rather than what it looks like: it may be referenced only from an
    ``mcpServers`` ``env`` / ``headers`` entry, it carries no default, and it is
    never rendered back to a caller. A playbook is a distribution unit, so a
    credential that reached the file, a reply or a dispatched prompt would
    travel with it.
    """

    type: Literal["string", "integer", "number", "boolean", "enum", "path", "secret"] = "string"
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

    @model_validator(mode="after")
    def _secret_carries_no_value(self) -> "ParamSpec":
        if self.type == "secret" and self.default is not None:
            raise ValueError("type 'secret' must not carry a default: the value would live in the playbook file")
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
    mode: Literal["dag", "prompt", "stint"]
    confirm: bool = True
    triggers: Triggers
    params: dict[str, ParamSpec] = Field(default_factory=dict)

    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    """MCP servers this playbook brings with it, keyed by the name a node's
    ``mcps`` entry uses. ``dag`` mode only -- a prompt-mode graph is composed by
    the caller in a later turn, which cannot be handed these definitions, so the
    combination is refused in validation rather than silently dropped.

    Without this, ``mcps: [deepwiki]`` in a distributed playbook works only
    where the receiving machine happens to have a server of that name -- the
    field is a local short name resolved against the host's own
    ``tools.mcpServers``. A definition here travels with the file and is
    resolved first, the host's config second.

    The value shape is the host's own :class:`~raven.config.schema.MCPServerConfig`
    rather than a second definition of the same thing, so the definition that
    travels is the definition the host can connect. Secrets do not travel with
    it: an ``env`` / ``headers`` value may reference a ``secret`` param
    (``{{ params.PG_PASSWORD }}``) and the value arrives at run time."""

    nodes: list[NodeSpec] | None = None
    prompts: str | None = None
    """prompt mode: how to assemble the graph (layers, agents, per-node
    skills/mcps, dependencies) — never runtime decision rules; a composed
    graph is fixed once assembled."""

    roles: list[RoleEntry] | None = None
    """rounds mode: who plays each part, every round. The round's graph is
    compiled from this, one node per role, so a role is a node's worth of
    description plus what carries between rounds."""

    memory: list[MemoryEntry] | None = None
    """rounds mode: the files that carry between rounds. Every round is a fresh
    conversation for every role, so nothing carries that is not written down."""

    verify: list[VerifyEntry] | None = None
    """rounds mode: real commands a role's work is measured by. The one signal
    in a round no model produced, which is why it is worth its cost."""

    stop: StopSpec | None = None
    """rounds mode: when to stop opening rounds. Absent is the default budget,
    never "forever"."""

    setup: Literal["stint"] | None = None
    """rounds mode: the project layout this playbook is written against, laid
    out before the first round when the project does not have it.

    A name from a closed set rather than a list of files or a script. A playbook
    is a file that travels, so a setup section that could say *what* to write
    would be a way to make the host write anything; a name can only select a
    recipe that shipped with the host, and the approval can say which one and
    what it lays down. Absent is a playbook that works a project as it finds
    it -- most of them."""

    STINT_SECTIONS: ClassVar[tuple[str, ...]] = ("roles", "memory", "verify", "stop", "setup")

    @model_validator(mode="after")
    def _mode_section_pairing(self) -> "PlaybookSpec":
        if self.mode == "stint":
            if not self.roles:
                raise ValueError("mode 'stint' requires non-empty roles")
            if self.nodes or self.prompts:
                raise ValueError("mode 'stint' carries roles, not nodes or prompts")
            return self
        # Said by name rather than as "the other sections": a playbook that
        # carried `verify` under `mode: dag` would look like it runs commands
        # and would not, and a section that is silently ignored is worse than
        # one that is refused.
        carried = [name for name in self.STINT_SECTIONS if getattr(self, name) is not None]
        if carried:
            raise ValueError(f"mode {self.mode!r} must not carry {', '.join(carried)} -- those are rounds mode's")
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
    def _the_round_is_an_order_of_roles(self) -> "PlaybookSpec":
        """Unique labels, resolvable dependencies, no cycle.

        The same three rules the graph itself enforces, asked here so a role
        table that could not compile is refused when the file loads rather than
        when somebody runs it.
        """
        roles = self.roles or []
        labels = [role.label for role in roles]
        duplicated = sorted({label for label in labels if labels.count(label) > 1})
        if duplicated:
            raise ValueError(f"two roles share the label {', '.join(duplicated)}; a label names one part")
        known = set(labels)
        for role in roles:
            unknown = [dep for dep in role.depends_on if dep not in known]
            if unknown:
                raise ValueError(f"role {role.label!r} depends on {', '.join(unknown)}, which no role answers to")
            if role.label in role.depends_on:
                raise ValueError(f"role {role.label!r} depends on itself")
        remaining = {role.label: set(role.depends_on) for role in roles}
        while remaining:
            ready = sorted(label for label, deps in remaining.items() if not deps)
            if not ready:
                raise ValueError(f"the roles depend on each other in a circle: {', '.join(sorted(remaining))}")
            for label in ready:
                remaining.pop(label)
            for deps in remaining.values():
                deps.difference_update(ready)
        return self

    @model_validator(mode="after")
    def _a_role_is_measured_by_a_check_that_exists(self) -> "PlaybookSpec":
        declared = {entry.name for entry in (self.verify or [])}
        for role in self.roles or []:
            unknown = [name for name in role.verify_after if name not in declared]
            if unknown:
                raise ValueError(
                    f"role {role.label!r} verifies after {', '.join(unknown)}, which no verify entry names"
                )
        return self

    @model_validator(mode="after")
    def _running_commands_is_approved_once_or_not_at_all(self) -> "PlaybookSpec":
        """A playbook is a file that travels; `verify` is the one part of it
        that executes. The single approval a plan gets has to be the approval
        for those commands, so a stint playbook carrying them may not waive it.
        """
        if self.verify and not self.confirm:
            raise ValueError("a playbook carrying verify commands must keep confirm: true -- they run as shell")
        return self

    @model_validator(mode="after")
    def _param_names_are_identifiers(self) -> "PlaybookSpec":
        for pname in self.params:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", pname):
                raise ValueError(f"param name {pname!r} must be an identifier")
        return self

    @model_validator(mode="after")
    def _mcp_server_names_are_path_safe(self) -> "PlaybookSpec":
        for sname in self.mcp_servers:
            if not re.match(MCP_SERVER_NAME_RE, sname):
                raise ValueError(f"mcpServers name {sname!r} must match {MCP_SERVER_NAME_RE}")
        return self

    def block_dump(self) -> dict[str, Any]:
        """The ``yaml playbook-spec`` block content: machine fields only,
        camelCase; frontmatter fields excluded."""
        data = self.model_dump(by_alias=True, exclude_none=True)
        for key in self.FRONTMATTER_FIELDS:
            data.pop(key, None)
        # An MCP server config carries a full default set (transport, oauth,
        # timeout), and ``exclude_none`` keeps every one of them -- so a
        # three-line definition round-tripped as twenty. Dumped by what the
        # author wrote instead; a dropped default reloads as the same default.
        if self.mcp_servers:
            data["mcpServers"] = {
                name: cfg.model_dump(by_alias=True, exclude_defaults=True) for name, cfg in self.mcp_servers.items()
            }
        else:
            data.pop("mcpServers", None)
        return data
