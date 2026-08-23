"""The built-in agent row the package ships, and how config overrides it.

There is exactly one: the in-process raven loop, on the same table as the
external agents so that ``spawn`` and a DAG node pick from one roster -- a
built-in agent only reachable by omitting the ``subagent`` argument is an agent
the model cannot be told about.

It is a seed rather than a default-in-a-field: the row exists whether or not
config mentions it, and a config row of the same name is a *field-level*
override -- writing ``{"name": "raven", "skills": [...]}`` retunes its skill menu
and leaves its description alone. There is deliberately no way to take it off the
roster: not writing a row already means "use the package's row", and ``enabled``
is the one field an override may not speak to.

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
# records under ``subagents/direct/<agent>/<handle>/`` and instance-registry rows
# were written before there was a table, and this row is what they resolve against
# now -- which is why the name is reserved rather than tidied into
# ``general-raven``. Those records spell it lowercase, hence ``LEGACY_AGENT_ALIASES``.
GENERIC_AGENT = "Raven"

_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "name": GENERIC_AGENT,
        # Says what the row cannot do, because nothing else tells the dispatching
        # model: ``RavenLoopBackend`` builds its own registry (files, shell, web)
        # and registers neither ``spawn`` nor ``run_subagent_dag``, so a step that
        # has to delegate cannot be done here and cannot be handed on. "The skills
        # those tools support" is the same honesty about the menu -- the sub-agent
        # prompt withholds any skill whose ``requires.tools`` this row lacks.
        "description": (
            "Raven's own in-process sub-agent: files, shell and web, plus the skills those tools "
            "support, with no capability bias. Put what this step must accomplish in its prompt -- "
            "that is the only thing that shapes the run. IMPORTANT: it cannot call sub-agents, so "
            "give it only work it can finish on its own."
        ),
    },
)

BUILTIN_AGENT_NAMES: frozenset[str] = frozenset(seed["name"] for seed in _SEEDS)
"""Names the package seeds. Used to tell an override from an addition, and by the
write paths that must not let a caller change a seed row's transport.

Match against it with :func:`is_builtin_agent_name`, never with ``in`` directly:
a stored name may be a legacy alias, and a guard that misses one lets a config row
be written under a seed's old name -- which lands on the table as a second agent
rather than as the override it was meant to be."""

LEGACY_AGENT_ALIASES: dict[str, str] = {"raven": GENERIC_AGENT}
"""Names a seed used to answer to, mapped to the one it answers to now.

The generic row was lowercase until it was capitalised to match the rest of the
table, and by then thousands of instance-registry rows, direct-chat records and
stored dag nodes had its old name written into them. Resolution consults this only
*after* an exact match fails, so no other agent's name changes meaning, and the
roster never advertises an alias -- it is for reading old data, not for the model
to pick from."""


def canonical_agent_name(name: str) -> str:
    """The seed name an agent reference resolves to, or the name unchanged.

    Answers "which seed, if any", from the package's own names alone -- it reads no
    table, so it cannot tell that some *other* agent holds the name. Exactness at
    the row level belongs to the caller holding the rows, which tries the literal
    name before this one (see :meth:`AgentRegistry.get`); an agent that genuinely
    holds an alias's spelling therefore still wins its own name.

    A seed's current name maps to itself even if the alias table also lists it, so
    a mistake there cannot make the row unreachable under the name it advertises.
    """
    if name in BUILTIN_AGENT_NAMES:
        return name
    return LEGACY_AGENT_ALIASES.get(name, name)


def is_builtin_agent_name(name: str) -> bool:
    """Whether a name refers to a package seed, under any name it has had."""
    return canonical_agent_name(name) in BUILTIN_AGENT_NAMES


def builtin_agent_seeds() -> list["BuiltinAgentConfig"]:
    """The package's built-in rows, freshly validated.

    Built per call rather than as a module constant: the rows are mutable pydantic
    models and the registry hands them out, so one shared instance would let a
    caller's ``model_copy(update=...)`` target leak into the next materialization.
    """
    from raven.config.schema import BuiltinAgentConfig

    return [BuiltinAgentConfig.model_validate(seed) for seed in _SEEDS]


_NOT_OVERRIDABLE = frozenset({"enabled", "name"})
"""Seed fields a config row cannot speak to. See :func:`_overrides`."""


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

    ``enabled`` is dropped whatever it says: a seed row's switch is the package's,
    not config's. See :func:`merge_builtin_seeds` for why.

    ``name`` is dropped for the same shape of reason: it is what identified the seed
    to override in the first place, so it can only say what is already true -- or,
    for a row written under a legacy alias, rename the merged row back to the name
    the alias exists to move away from.
    """
    from raven.config.schema import BuiltinAgentConfig

    fields = BuiltinAgentConfig.model_fields
    out: dict[str, Any] = {}
    for name in cfg.model_fields_set:
        if name in _NOT_OVERRIDABLE:
            continue
        field = fields.get(name)
        value = getattr(cfg, name)
        if field is not None and value == field.default:
            continue
        out[name] = value
    return out


def _without_shadowed_aliases(configs: list[Any]) -> list[Any]:
    """Config rows with any legacy-named row dropped whose current name is also written.

    Two spellings of one seed are the ambiguity ``SubagentsConfig._dedupe_names``
    removes for two rows of one name, and it cannot see this pair because the
    strings differ. Resolved here rather than by order of appearance, so which
    override wins does not depend on where in the file the rows sit.
    """
    written = {getattr(cfg, "name", None) for cfg in configs}
    kept: list[Any] = []
    for cfg in configs:
        name = getattr(cfg, "name", None)
        canonical = canonical_agent_name(name) if name else None
        if canonical is not None and canonical != name and canonical in written:
            logger.warning(
                "sub-agent {!r} is the former name of built-in agent {!r}, and config declares both; "
                "the entry under {!r} wins and this one is ignored -- delete it to silence this",
                name,
                canonical,
                canonical,
            )
            continue
        kept.append(cfg)
    return kept


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

    ``enabled`` is not among the fields an override can set. An unnamed ``spawn``
    and a DAG node with no ``subagent`` both normalize to the generic seed, so a
    roster without it is a roster with a hole where the default lands. The seed's
    switch therefore wins over a stored one -- including one hand-written into
    config, which is the case no UI guard can reach.

    A row naming a seed the way it used to be named is that seed's override too
    (see :data:`LEGACY_AGENT_ALIASES`); if config declares both spellings, the
    current one wins and the legacy row is dropped with a warning.

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

    for cfg in _without_shadowed_aliases(configs or []):
        name = getattr(cfg, "name", None)
        if not name:
            continue
        # A stored row may spell a seed's name the way that seed used to be
        # spelled; it is still that seed's override, and keying it under the
        # current name is what keeps it from landing beside the seed as a second,
        # phantom agent.
        name = canonical_agent_name(name)
        seed = by_name.get(name)
        if seed is None:
            by_name[name] = cfg
            order.append(name)
            continue
        if getattr(cfg, "kind", None) != "builtin":
            # The seed wins, and the configured row is dropped from the table. It
            # used to be the other way round, on the reading that a hand-written row
            # is the one the user meant -- but a seed row is mandatory now, and an
            # unnamed ``spawn`` and a dag node with no ``subagent`` both resolve to
            # this one. Letting config displace it puts an external backend behind
            # every such dispatch, silently, which is worse than losing one row.
            #
            # Reachable without anyone doing anything wrong: ``Raven`` was a legal
            # third-party name until the generic row was renamed to it, because the
            # write guard reserved only the lowercase spelling. Nothing is lost --
            # config is untouched, so renaming the entry brings it straight back,
            # which is what this says.
            logger.warning(
                "sub-agent {!r} is declared as kind {!r} but that name belongs to a built-in agent; "
                "the built-in row wins and this entry is ignored -- rename it in config to keep both",
                name,
                getattr(cfg, "kind", None),
            )
            continue
        by_name[name] = seed.model_copy(update=_overrides(cfg))

    return [by_name[name] for name in order]


__all__ = [
    "BUILTIN_AGENT_NAMES",
    "GENERIC_AGENT",
    "LEGACY_AGENT_ALIASES",
    "builtin_agent_seeds",
    "canonical_agent_name",
    "is_builtin_agent_name",
    "merge_builtin_seeds",
]
