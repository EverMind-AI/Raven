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
from raven.agent.subagent.builtin_agents import GENERIC_AGENT, merge_builtin_seeds
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
