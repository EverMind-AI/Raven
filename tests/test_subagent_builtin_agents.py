"""Ownership across the built-in seed/override merge.

``merge_builtin_seeds`` applies a config row over a package seed field by field,
keeping only the fields that say something -- so the three states ``owns`` has
(undeclared, declared, explicitly nothing) have to survive a round trip through a
stored row, which arrives with every field populated. Undeclared has to inherit,
because a row written to retune ``skills`` must not strip a seed's ownership on
its way past; explicitly empty has to stick, because that is how an agent is
opted out.
"""

from __future__ import annotations

from typing import Any

from raven.agent.subagent import builtin_agents
from raven.agent.subagent.backends import agent_meta
from raven.agent.subagent.builtin_agents import (
    GENERIC_AGENT,
    builtin_agent_seeds,
    is_builtin_agent_name,
    merge_builtin_seeds,
)
from raven.config.schema import BuiltinAgentConfig

OWNS = "owns decks. Do not build the deck yourself."


def _row(**fields: Any) -> BuiltinAgentConfig:
    """A row as the write path leaves it: validated, then dumped and reloaded."""
    row = BuiltinAgentConfig.model_validate({"name": "Scribe", "kind": "builtin", **fields})
    return BuiltinAgentConfig.model_validate(row.model_dump())


def _seeded(monkeypatch, **seed_fields: Any) -> None:
    """Replace the package seeds with one Scribe seed, so "inherit" is provable.

    The shipped table is a single generic row that declares no ownership, against
    which an inherit test cannot tell a working inherit from a dropped field.
    """
    seed = BuiltinAgentConfig.model_validate({"name": "Scribe", "kind": "builtin", **seed_fields})
    monkeypatch.setattr(builtin_agents, "builtin_agent_seeds", lambda: [seed.model_copy()])


class TestOwnershipIsDeclarableOnABuiltinRow:
    def test_the_field_survives_validation(self) -> None:
        assert _row(owns=OWNS).owns == OWNS

    def test_and_reaches_the_advertised_capabilities(self) -> None:
        assert agent_meta(_row(owns=OWNS)).owns == OWNS

    def test_undeclared_reads_as_no_claim(self) -> None:
        assert _row().owns is None
        assert agent_meta(_row()).owns == ""


class TestOwnershipThroughTheSeedOverride:
    def test_a_row_that_says_nothing_inherits_the_seed(self, monkeypatch) -> None:
        _seeded(monkeypatch, owns=OWNS)
        merged = merge_builtin_seeds([_row(skills=["x"])])[0]
        assert merged.owns == OWNS
        assert merged.skills == ["x"]

    def test_an_explicit_empty_string_opts_the_agent_out(self, monkeypatch) -> None:
        _seeded(monkeypatch, owns=OWNS)
        assert merge_builtin_seeds([_row(owns="")])[0].owns == ""

    def test_a_declared_value_overrides_the_seed(self, monkeypatch) -> None:
        _seeded(monkeypatch, owns="owns nothing much.")
        assert merge_builtin_seeds([_row(owns=OWNS)])[0].owns == OWNS

    def test_a_row_the_package_does_not_ship_keeps_its_own(self) -> None:
        merged = {row.name: row for row in merge_builtin_seeds([_row(owns=OWNS)])}
        assert merged["Scribe"].owns == OWNS
        assert merged[GENERIC_AGENT].owns is None


class TestTheSwitchIsNotOverridableOnABuiltinRow:
    """``enabled`` is the one field a config row may not say anything about.

    An unnamed ``spawn`` and a DAG node with no ``subagent`` both normalize to the
    generic built-in row, so a roster without it is a roster with a hole where the
    default lands. The seed's switch therefore wins over a stored one -- including
    a row hand-written into config, which no UI guard can reach.
    """

    def test_a_stored_switch_off_does_not_reach_the_merged_row(self, monkeypatch) -> None:
        _seeded(monkeypatch)
        assert merge_builtin_seeds([_row(enabled=False)])[0].enabled is True

    def test_and_the_rest_of_that_row_still_overrides(self, monkeypatch) -> None:
        _seeded(monkeypatch)
        merged = merge_builtin_seeds([_row(enabled=False, skills=["x"])])[0]
        assert merged.enabled is True
        assert merged.skills == ["x"]


class TestTheGenericAgentCarriesACapitalisedName:
    """It is the product's own name, and every other row on the table is capitalised."""

    def test_the_seed_is_the_capitalised_name(self) -> None:
        assert GENERIC_AGENT == "Raven"
        assert [seed.name for seed in builtin_agent_seeds()] == ["Raven"]


class TestTheLegacyLowercaseNameStillNamesTheSeed:
    """Thousands of instance-registry rows and every stored dag node spell it
    ``raven``, from before the row was capitalised.

    ``merge_builtin_seeds`` matches a config row to a seed by name, so without the
    alias a stored ``{"name": "raven", ...}`` override stops overriding and lands on
    the table as a second, phantom agent -- and the write-path guards, which read
    ``BUILTIN_AGENT_NAMES``, stop recognising the name they exist to protect.
    """

    def test_the_membership_helper_accepts_it(self) -> None:
        assert is_builtin_agent_name("raven")
        assert is_builtin_agent_name(GENERIC_AGENT)
        assert not is_builtin_agent_name("Coder")

    def test_a_row_written_under_it_overrides_the_seed(self) -> None:
        stored = BuiltinAgentConfig.model_validate({"name": "raven", "kind": "builtin", "skills": ["x"]})
        merged = merge_builtin_seeds([BuiltinAgentConfig.model_validate(stored.model_dump())])

        assert [row.name for row in merged] == [GENERIC_AGENT]
        assert merged[0].skills == ["x"]
        # And it is an override, not a replacement: the seed's description is the
        # only line the model reads about this agent.
        assert "in-process sub-agent" in merged[0].description

    def test_the_capitalised_row_wins_when_config_holds_both(self) -> None:
        legacy = BuiltinAgentConfig.model_validate({"name": "raven", "kind": "builtin", "skills": ["old"]})
        current = BuiltinAgentConfig.model_validate({"name": GENERIC_AGENT, "kind": "builtin", "skills": ["new"]})
        merged = merge_builtin_seeds([legacy, current])

        assert [row.name for row in merged] == [GENERIC_AGENT]
        assert merged[0].skills == ["new"]


class TestTheDescriptionStatesWhatTheAgentCannotDo:
    """The roster line is the model's whole basis for picking this row.

    It has no ``spawn`` and no ``run_subagent_dag`` -- ``RavenLoopBackend`` builds
    its own registry with files, shell and web and nothing else -- so a roster that
    does not say so invites a step that has to delegate to be routed here, where it
    cannot be done and cannot be handed on.
    """

    def test_it_says_sub_agents_are_out_of_reach(self) -> None:
        assert "cannot call sub-agents" in builtin_agent_seeds()[0].description

    def test_and_no_longer_claims_the_whole_tool_set(self) -> None:
        # The claim that produced the mismatch: `spawn` is part of "the whole tool
        # set", so a line saying both says nothing a reader can act on.
        assert "whole tool set" not in builtin_agent_seeds()[0].description


class TestANonBuiltinRowCannotTakeASeedsName:
    """A seed row is mandatory, so nothing in config may displace it.

    `Raven` was a legal third-party name before the generic row was renamed to it:
    the write guard reserved only the lowercase spelling, so `set_agents` accepted
    a cli/acp/openai row spelled this way and the two coexisted. A config written
    then still holds one -- and letting it win the name now would take the seed off
    the table, which is the one thing that cannot happen: an unnamed `spawn` and a
    dag node with no `subagent` both resolve to it.
    """

    def _external(self, name: str) -> Any:
        from raven.config.schema import ThirdPartyCliSubagentConfig

        return ThirdPartyCliSubagentConfig.model_validate(
            {"name": name, "kind": "cli", "enabled": True, "command": "true {prompt}", "description": "external"}
        )

    def test_the_seed_keeps_its_name_and_its_kind(self) -> None:
        merged = merge_builtin_seeds([self._external(GENERIC_AGENT)])

        assert [(row.name, row.kind) for row in merged] == [(GENERIC_AGENT, "builtin")]

    def test_the_colliding_row_is_dropped_rather_than_renamed_over_the_seed(self) -> None:
        # Two rows of one name is the ambiguity the merge exists to remove, and the
        # seed is the half that cannot be dropped. Config is untouched, so the entry
        # comes back the moment it is renamed -- which is what the warning says.
        merged = merge_builtin_seeds([self._external(GENERIC_AGENT), self._external("Coder")])

        assert [row.name for row in merged] == [GENERIC_AGENT, "Coder"]
        assert next(r for r in merged if r.name == GENERIC_AGENT).kind == "builtin"

    def test_a_row_under_the_legacy_spelling_does_not_displace_it_either(self) -> None:
        # The alias resolves the old name to the seed, so a cli row spelled that way
        # would otherwise reach the seed's slot through the same door.
        merged = merge_builtin_seeds([self._external("raven")])

        assert [(row.name, row.kind) for row in merged] == [(GENERIC_AGENT, "builtin")]
