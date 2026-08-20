"""The built-in agent rows the package ships, and how config overrides them.

These are the rows a user does not have to write. They are seeds rather than
defaults-in-a-field: a row here exists whether or not config mentions it, and a
config row of the same name is a *field-level* override -- writing
``{"name": "research-raven", "skills": [...]}`` retunes that agent's skill menu
and leaves its description alone. There is deliberately no way to delete one,
because "not written" already means "use the package's row"; ``enabled: false``
is how a row leaves the roster.

The descriptions are the model's only account of what each agent is for, so they
are charters and boundaries rather than personas, and they are the same strings
the playbook generator used to read out of ``roles_default.yaml`` -- moved here
because the agent registry is what resolves an agent name now, and having the
agent layer import its own roster out of :mod:`raven.playbook` would invert that
dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from raven.config.schema import BuiltinAgentConfig

# The generic in-process sub-agent. Also the historical identity: direct-chat
# records under ``subagents/direct/raven/<handle>/`` and instance-registry rows
# with agent ``"raven"`` were written before there was a table, and this row is
# what they resolve against now -- which is why the name is reserved rather than
# tidied into ``general-raven``.
GENERIC_AGENT = "raven"

_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "name": "research-raven",
        "description": (
            "Deep retrieval and fact-checking: multi-source search, source-credibility judgment, "
            "adjudication when retrieved results conflict, research conclusions delivered with "
            "citations. No claims without a source."
        ),
    },
    {
        "name": "code-raven",
        "description": (
            "Repository-level code work: understand the existing code structure, locate where a "
            "change lands, implement with unit tests, hands-on terminal work. Follow the repo's "
            "existing abstractions; never build a parallel system."
        ),
    },
    {
        "name": "data-raven",
        "description": (
            "The whole data chain: data discovery, SQL workflows, statistics and causal judgment, "
            "quantified impact. Conclusions must be verifiable by an executable comparison or a "
            "programmatic assertion."
        ),
    },
    {
        "name": "content-raven",
        "description": (
            "Text and presentation deliverables: writing, rewriting, removing the AI flavor, "
            "producing final copy under format and tone constraints, self-review. Conclusions "
            "first; hard constraints (word count, structure) outrank style."
        ),
    },
    {
        "name": GENERIC_AGENT,
        "description": "General-purpose sub-agent with no capability bias; the whole tool set, no skill narrowing.",
    },
)

BUILTIN_AGENT_NAMES: frozenset[str] = frozenset(seed["name"] for seed in _SEEDS)
"""Names the package seeds. Used to tell an override from an addition, and by the
write paths that must not let a caller change a seed row's transport."""


def builtin_agent_seeds() -> list["BuiltinAgentConfig"]:
    """The package's built-in rows, freshly validated.

    Built per call rather than as a module constant: the rows are mutable pydantic
    models and the registry hands them out, so one shared instance would let a
    caller's ``model_copy(update=...)`` target leak into the next materialization.
    """
    from raven.config.schema import BuiltinAgentConfig

    return [BuiltinAgentConfig.model_validate(seed) for seed in _SEEDS]


def _overrides(cfg: Any) -> dict[str, Any]:
    """The fields of one config row that actually say something.

    Fields still holding the schema default are dropped, rather than trusting
    ``model_fields_set``. A row does not stay in the shape it was written in: the
    write path validates and dumps the whole model, so a stored override of one
    field comes back with *every* field present -- and applied literally, an
    override that only meant to flip ``enabled`` would also reset the seed's
    description to ``""`` and blank the agent out of the roster the model reads.

    The cost is that a value equal to the default cannot be written *as* an
    override. For the fields here that reads correctly: ``description: ""`` is not
    an edit anyone means, and every other default is "inherit".
    """
    from raven.config.schema import BuiltinAgentConfig

    fields = BuiltinAgentConfig.model_fields
    out: dict[str, Any] = {}
    for name in cfg.model_fields_set:
        field = fields.get(name)
        value = getattr(cfg, name)
        if field is not None and value == field.default:
            continue
        out[name] = value
    return out


def merge_builtin_seeds(configs: list[Any] | None) -> list[Any]:
    """Config rows over the package seeds: the table the registry materializes.

    Seeds keep their declared order and stay in place when overridden, so the
    roster the model reads does not reshuffle because a user retuned one agent.
    Rows naming something the package does not ship are appended in config order.

    A config row overrides a seed field-by-field, using only the fields the row
    actually set (``model_fields_set``) -- a row that mentions ``skills`` must not
    silently reset ``description`` to the schema default and blank the agent out of
    the roster.

    A non-builtin row that claims a seed's name takes the slot, with a warning:
    two rows of one name is the ambiguity ``SubagentsConfig._dedupe_names`` exists
    to remove, and between the two the one the user wrote by hand is the one they
    meant. The seed is not recoverable except by renaming that row, which the
    warning says.
    """
    by_name: dict[str, Any] = {}
    order: list[str] = []
    for seed in builtin_agent_seeds():
        by_name[seed.name] = seed
        order.append(seed.name)

    for cfg in configs or []:
        name = getattr(cfg, "name", None)
        if not name:
            continue
        seed = by_name.get(name)
        if seed is None:
            by_name[name] = cfg
            order.append(name)
            continue
        if getattr(cfg, "kind", None) != "builtin":
            logger.warning(
                "sub-agent {!r} is declared as kind {!r} but that name is a built-in agent; "
                "the configured row wins and the built-in one is unavailable -- rename it to keep both",
                name,
                getattr(cfg, "kind", None),
            )
            by_name[name] = cfg
            continue
        by_name[name] = seed.model_copy(update=_overrides(cfg))

    return [by_name[name] for name in order]


__all__ = ["BUILTIN_AGENT_NAMES", "GENERIC_AGENT", "builtin_agent_seeds", "merge_builtin_seeds"]
