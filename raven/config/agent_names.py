"""The names a sub-agent may be declared under, and what they resolve to.

Config validates declarations against these names at parse time -- a hand-written
row naming a preset that does not exist, or claiming a name a package seed owns,
is refused while it is still a dict. That check needs the vocabulary, not the
behaviour: which names are legal, never what those names run.

So the vocabulary is seated here rather than beside the tables it describes.
``raven.agent.subagent`` holds what a name *does* (the command, the transport,
the ready timeout) and imports these names downward; the schema and the write
path read them without reaching up into the agent package. Before this split the
two directions were both real, and both sides deferred their import into a
function body to keep module init off the cycle.

The preset names are spelled out rather than derived from
``THIRD_PARTY_SUBAGENT_PRESETS``: deriving them is the import this module exists
to remove. Two spellings of one fact is a drift risk, so the equality is pinned
by tests/test_subagent_name_vocabulary.py -- add a preset without adding its key
here and that test fails, rather than config quietly rejecting a preset that
does in fact ship.
"""

from __future__ import annotations

GENERIC_AGENT = "Raven"
"""The generic in-process sub-agent, and the historical identity.

Direct-chat records under ``subagents/direct/<agent>/<handle>/`` and
instance-registry rows were written before there was a table, and this row is
what they resolve against now -- which is why the name is reserved rather than
tidied into ``general-raven``. Those records spell it lowercase, hence
``LEGACY_AGENT_ALIASES``."""

BUILTIN_AGENT_NAMES: frozenset[str] = frozenset({GENERIC_AGENT})
"""Names the package seeds. Used to tell an override from an addition, and by the
write paths that guard a seed row's transport: a seed may be redeclared as
``acp`` (the host's own raven over ACP), but a cli / openai row may not claim one
of these names.

Match against it with :func:`is_builtin_agent_name`, never with ``in`` directly:
a stored name may be a legacy alias, and a guard that misses one lets a config row
be written under a seed's old name -- which lands on the table as a second agent
rather than as the override it was meant to be.

Kept equal to the seed table's own names by
tests/test_subagent_name_vocabulary.py."""

LEGACY_AGENT_ALIASES: dict[str, str] = {"raven": GENERIC_AGENT}
"""Names a seed used to answer to, mapped to the one it answers to now.

The generic row was lowercase until it was capitalised to match the rest of the
table, and by then thousands of instance-registry rows, direct-chat records and
stored dag nodes had its old name written into them. Resolution consults this only
*after* an exact match fails, so no other agent's name changes meaning, and the
roster never advertises an alias -- it is for reading old data, not for the model
to pick from."""

THIRD_PARTY_PRESET_NAMES: frozenset[str] = frozenset(
    {
        "claude_code",
        "codebuddy",
        "codex",
        "github_copilot",
        "grok",
        "hermes",
        "kimi_code",
        "mirothinker",
        "openclaw",
        "opencode",
        "pi",
        "qoder",
        "qwen_code",
    }
)
"""Keys of the built-in third-party presets.

The provenance field on a stored row names one of these. An explicit value that
names none is refused at parse time: a hand-edited ``preset: "hermes"`` on an
unrelated entry would otherwise both dodge the reserved-name guard and hide the
real Hermes preset from the Presets group.

Kept equal to ``THIRD_PARTY_SUBAGENT_PRESETS``'s keys by
tests/test_subagent_name_vocabulary.py."""


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


__all__ = [
    "BUILTIN_AGENT_NAMES",
    "GENERIC_AGENT",
    "LEGACY_AGENT_ALIASES",
    "THIRD_PARTY_PRESET_NAMES",
    "canonical_agent_name",
    "is_builtin_agent_name",
]
