"""Drill cards with drawn values: every playing meets new facts, yet each card keeps the path it was written for."""

import random

import pytest

from experimental.simulation.cards import draw, split
from experimental.simulation.reference import Rules
from experimental.simulation.scenario import BUNDLED, Scenario
from experimental.simulation.traveller import Traveller
from raven.contracts.llm_provider import LLMResponse

TRAVEL = BUNDLED / "travel_agency"
DRAWS = 300


class Provider:
    def __init__(self):
        self.requests = []

    async def chat_with_retry(self, **kwargs):
        self.requests.append(kwargs)
        return LLMResponse(content="Hello")


def chosen(rules: Rules, trip) -> tuple[str, int]:
    """The product the SOP's selection matrix leads to (S3.1, S3.3): by nightly budget, then down while it does not fit."""
    order = list(rules.products)
    nightly = trip.budget / trip.persons / trip.nights
    tier = 2 if nightly > 2000 else 1 if nightly > 500 else 0
    while tier >= 0:
        product = order[tier]
        expected = rules.expected(product, trip)
        if rules.products[product].base <= trip.nights and expected and expected.total <= trip.budget:
            return product, tier
        tier -= 1
    raise AssertionError(f"no product fits {trip}")


def test_every_card_draws_fresh_facts_but_keeps_its_product_season_and_budget_fit():
    scenario = Scenario.load(TRAVEL)
    rules = Rules.load(scenario)
    tiers = {}
    for persona in scenario.personas:
        played = [persona.play(random.Random(f"7:{persona.name}:{count}")) for count in range(DRAWS)]
        assert all(card.trip and "{" not in card.text and card.trip.phone in card.text for card in played)
        assert all(f"{card.trip.start[0]}" in card.text and str(card.trip.budget) in card.text for card in played)
        assert len({(card.trip.start, card.trip.phone, card.trip.budget) for card in played}) > DRAWS * 0.9
        assert len({rules.season(card.trip.start) for card in played}) == 1
        assert len({card.trip.persons for card in played}) == 1
        tiers[persona.name] = {chosen(rules, card.trip)[1] for card in played}
    assert tiers == {"family": {0}, "premium": {2}, "professional": {1}, "returning": {0}, "student": {0}}


def test_one_seed_draws_the_same_facts_and_a_card_without_values_plays_as_written():
    values, body = split("---\nvalues:\n  who: [Ann, Bo]\n  n: {from: 1, to: 9}\n---\n{who} has {n}.\n")
    first, again = (draw("x", values, body, random.Random("s")) for _ in range(2))
    assert first == again and first.trip is None and "{" not in first.text
    assert split("Plain card.") == ({}, "Plain card.")
    assert draw("plain", {}, "Keep {braces}.", random.Random(1)).text == "Keep {braces}."
    trip = {"start": {"from": "10-30", "to": "10-30"}, "nights": 3}
    with pytest.raises(ValueError, match="phone"):
        draw("x", trip, "", random.Random(1))


async def test_the_traveller_plays_its_card_afresh_each_conversation_and_one_seed_repeats_the_rounds():
    scenario = Scenario.load(TRAVEL)
    student = next(persona for persona in scenario.personas if persona.name == "student")
    traveller = Traveller(student, Provider(), seed=3)
    await traveller.speak([])
    first = traveller.card
    await traveller.speak([])
    assert traveller.card != first and traveller.played == 2
    again = Traveller(student, Provider(), seed=3)
    await again.speak([])
    assert again.card == first
    assert first.trip.end == (first.trip.start[0], first.trip.start[1] + 3)
