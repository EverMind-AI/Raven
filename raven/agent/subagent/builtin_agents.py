"""The built-in agent row the package ships, and how config overrides it.

There is exactly one: the in-process raven loop, on the same table as the
external agents so that ``spawn`` and a DAG node pick from one roster -- a
built-in agent only reachable by omitting the ``subagent`` argument is an agent
the model cannot be told about.

It is a seed rather than a default-in-a-field: the row exists whether or not
config mentions it, and a config row of the same name is a *field-level*
override -- writing ``{"name": "raven", "skills": [...]}`` retunes its skill menu
and leaves its description alone. There is deliberately no way to delete it,
because "not written" already means "use the package's row"; ``enabled: false``
is how a row leaves the roster.

**Four more rows used to be here** -- ``research-raven``, ``code-raven``,
``data-raven``, ``content-raven`` -- inherited from the playbook role pool that
predated this table. They were removed because they were not agents. The backend
factory reads four fields off a row (``model``, ``restrict_to_workspace``,
``tools``, ``skills``) and all four were unset on all four rows, so every one of
them dispatched the same provider, the same model, the same tool set and the same
system prompt; the sub-agent was never even told which name it was running under.
What they did have was five distinct descriptions on the roster, which is why the
dispatching model wrote ``research-raven`` on a node -- it was picking off a menu,
not from the work. The per-step differentiation that mattered was always in the
node's ``promptTemplate``, and that is untouched by their removal. Re-adding a
specialised row is a matter of writing one with a real narrowing on it; a row
whose only content is a description is a label the table lends false authority to.
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
        "name": GENERIC_AGENT,
        "description": (
            "Raven's own in-process sub-agent: the whole tool set and the whole skill catalogue, "
            "no capability bias. Put what this step must accomplish in its prompt -- that is the "
            "only thing that shapes the run."
        ),
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

    A config row overrides a seed field-by-field, using only the fields that say
    something (see :func:`_overrides` -- not ``model_fields_set``, which a stored
    row arrives with fully populated) -- a row that mentions ``skills`` must not
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
