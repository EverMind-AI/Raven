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
from raven.agent.subagent.presets import BUNDLED_SHIM_PRESETS, THIRD_PARTY_SUBAGENT_PRESETS
from raven.config.agent_names import (
    BUILTIN_AGENT_NAMES,
    BUNDLED_SHIM_PRESET_NAMES,
    GENERIC_AGENT,
    LEGACY_AGENT_ALIASES,
    THIRD_PARTY_PRESET_NAMES,
    canonical_agent_name,
    is_builtin_agent_name,
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


def test_bundled_shim_names_match_the_preset_table() -> None:
    """The rows whose command is this host's interpreter, which config has to
    name for the load-time refresh and for the cross-machine refusal. A preset
    added to the table without its key here would simply stop being refreshed --
    silently, at the next install that moves."""
    assert set(BUNDLED_SHIM_PRESETS) == set(BUNDLED_SHIM_PRESET_NAMES), (
        "the bundled-shim table and the name vocabulary have drifted; a row whose "
        "interpreter is never refreshed points at one that is gone"
    )


def test_the_generic_agent_is_seeded() -> None:
    """A vocabulary that named nothing would satisfy both checks above."""
    assert GENERIC_AGENT in BUILTIN_AGENT_NAMES
    assert len(THIRD_PARTY_PRESET_NAMES) >= 10


def test_the_generic_agent_is_not_named_like_the_host() -> None:
    """The host is Raven: its prompt says so and the UI labels its own approvals
    and its row that way. A roster entry spelled the same made the generic
    sub-agent's approvals indistinguishable from the host's, and put "hand it to
    `Raven`" into the prompt of the agent called Raven."""
    assert GENERIC_AGENT == "Raven-General"


def test_every_earlier_name_of_the_generic_agent_resolves_to_it() -> None:
    """Transcripts, direct chats and stored graphs written under a former name keep
    it, and instance rows keep it until the registry's next write, so each former
    name has to keep resolving."""
    for former in ("raven", "Raven"):
        assert LEGACY_AGENT_ALIASES.get(former) == GENERIC_AGENT
        assert canonical_agent_name(former) == GENERIC_AGENT
        assert is_builtin_agent_name(former)
