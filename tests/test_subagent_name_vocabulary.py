"""The name vocabulary and the tables it describes say the same thing.

``raven.config.agent_names`` holds which sub-agent names are legal; the tables
holding what those names do stay in ``raven.agent.subagent``. Config validates
declarations at parse time and reads only the vocabulary, which is what keeps
the import pointing one way -- config never reaches up into the agent package.

The cost of that split is two spellings of one fact. A preset added to
``THIRD_PARTY_SUBAGENT_PRESETS`` without its key added to
``THIRD_PARTY_PRESET_NAMES`` would ship and then be refused by the schema that
is supposed to accept it, and nothing else would report the mismatch. These
tests are the pin: the sets must be equal, in both directions.
"""

from __future__ import annotations

from raven.agent.subagent import builtin_agents
from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS
from raven.config.agent_names import (
    BUILTIN_AGENT_NAMES,
    GENERIC_AGENT,
    THIRD_PARTY_PRESET_NAMES,
)


def test_preset_names_match_the_preset_table() -> None:
    assert set(THIRD_PARTY_SUBAGENT_PRESETS) == set(THIRD_PARTY_PRESET_NAMES), (
        "the preset table and the name vocabulary have drifted; a preset the config "
        "schema refuses is a preset that ships but cannot be declared"
    )


def test_builtin_names_match_the_seed_table() -> None:
    seeded = {seed["name"] for seed in builtin_agents._SEEDS}

    assert seeded == set(BUILTIN_AGENT_NAMES), (
        "the seed table and the name vocabulary have drifted; a seed whose name is "
        "not reserved can be claimed by a user row"
    )


def test_the_generic_agent_is_seeded() -> None:
    """A vocabulary that named nothing would satisfy both checks above."""
    assert GENERIC_AGENT in BUILTIN_AGENT_NAMES
    assert len(THIRD_PARTY_PRESET_NAMES) >= 10
