"""Name resolution across the agent table.

The generic built-in row was renamed ``raven`` -> ``Raven``, and the names written
into instance-registry rows, direct-chat records and stored dag nodes before that
were not rewritten. Every one of those references still has to resolve, which is
what :data:`LEGACY_AGENT_ALIASES` is for -- and it has to resolve *without* the
roster ever offering the old spelling back to the model, or the table would
advertise two names for one agent.
"""

from __future__ import annotations

from typing import Any

from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.agent.subagent.registry import AgentRegistry
from raven.config.schema import ThirdPartyCliSubagentConfig


def _registry() -> AgentRegistry:
    """A table with the package seeds and nothing else."""
    registry = AgentRegistry(build_builtin=lambda row, narrowed: ("backend", row.name))
    registry.apply([])
    return registry


class TestTheGenericRowIsOnTheTableUnderItsCurrentName:
    def test_the_enum_offers_the_current_name(self) -> None:
        assert GENERIC_AGENT in _registry().names()

    def test_and_never_the_legacy_one(self) -> None:
        # The enum is what the model picks from. An alias there would read as a
        # second agent, and the two would be indistinguishable on the roster.
        assert "raven" not in _registry().names()


class TestAStoredReferenceUnderTheLegacyNameStillResolves:
    def test_lookup_finds_the_row(self) -> None:
        row = _registry().get("raven")

        assert row is not None
        assert row.name == GENERIC_AGENT

    def test_dispatch_finds_the_backend(self) -> None:
        # `backend` is its own lookup, not a wrapper around `get` -- a resume that
        # got its row would still fail here if only one of the two resolved.
        assert _registry().backend("raven") is not None

    def test_validation_accepts_a_playbook_that_names_it(self) -> None:
        # `all_names` is what a stored playbook's agent reference is checked
        # against, and a playbook written before the rename is not malformed.
        assert "raven" in _registry().all_names()

    def test_a_name_on_no_row_still_resolves_to_nothing(self) -> None:
        assert _registry().get("Nonexistent") is None
        assert _registry().backend("Nonexistent") is None


class TestTheAliasCannotBeUsedToDisplaceTheSeed:
    """Resolution tries the literal name first and the aliases only after.

    That ordering used to be observable as "an agent genuinely holding an alias's
    spelling wins its own name". It no longer is, and deliberately: a config row
    spelled like a seed -- under either the current name or a former one -- is
    dropped from the table rather than allowed to take the slot, because the seed
    is mandatory. So the property left to pin is the one that matters: neither
    spelling reaches anything but the built-in row.
    """

    def _external(self, name: str) -> Any:
        return ThirdPartyCliSubagentConfig.model_validate(
            {"name": name, "kind": "cli", "enabled": True, "command": "true {prompt}", "description": "external"}
        )

    def test_a_row_under_the_current_name_does_not_take_the_slot(self) -> None:
        registry = AgentRegistry(build_builtin=lambda row, narrowed: ("backend", row.name))
        registry.apply([self._external(GENERIC_AGENT)])

        row = registry.get(GENERIC_AGENT)
        assert row is not None
        assert row.kind == "builtin"

    def test_and_the_legacy_spelling_still_reaches_the_built_in_one(self) -> None:
        # The sharper half: without the seed winning, this lookup would resolve
        # through the alias into the external row, so every record written under
        # the old name would dispatch to a third-party backend.
        registry = AgentRegistry(build_builtin=lambda row, narrowed: ("backend", row.name))
        registry.apply([self._external(GENERIC_AGENT)])

        row = registry.get("raven")
        assert row is not None
        assert row.kind == "builtin"


class TestTheGenericRowsLimitReachesTheModel:
    """``roster_text`` is what ``format_agent_listing`` renders into the spawn tool's
    description and the dag tool's node guidance, so it is where the dispatching
    model learns what a row can be given.

    Asserted here rather than only on the seed: the seed is the source of the
    sentence, and this is the surface that decides whether the sentence has any
    effect. The two drifted once already -- the row advertised "the whole tool set"
    while its backend registered seven tools -- and nothing failed.
    """

    def test_the_roster_says_it_cannot_call_sub_agents(self) -> None:
        assert "cannot call sub-agents" in _registry().roster_text()
