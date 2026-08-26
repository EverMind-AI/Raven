"""The routes, and the promises that hold across all of them.

Two of these tests exist because the predecessor broke exactly here. One route
became unreachable under every configuration -- a predicate contradicted itself
-- and the suite had frozen the unreachability as expected behaviour, so 1500
lines of tooling sat dead for months. The other is the hard invariant about
physical quantities, which lived in prose and therefore could not fail.
"""

from __future__ import annotations

import pytest

from raven.ppt.contracts import Profile
from raven.ppt.contracts.findings import Severity
from raven.ppt.profiles import registry
from raven.ppt.services.gates.registry import DISPATCH


def test_every_registered_route_is_reachable_by_name() -> None:
    assert registry.names(), "no routes registered"
    for name in registry.names():
        assert isinstance(registry.get(name), Profile)
    assert registry.DEFAULT in registry.names()


def test_an_unknown_route_names_the_ones_that_exist() -> None:
    with pytest.raises(ValueError, match="Registered:"):
        registry.get("no_such_route")


def test_no_route_lets_the_model_write_a_physical_quantity() -> None:
    for name in registry.names():
        caps = registry.get(name).capabilities
        assert caps.physical_geometry is False, f"{name} opened physical geometry"
        assert caps.font_size is False, f"{name} opened font size"


def test_no_route_refuses_a_deck_over_something_the_gate_only_reports() -> None:
    """A route's fatal list may not contradict the severity the check itself gives.

    Publication refuses on `severity is BLOCKING or kind in blocking_kinds`, so the
    two say the same thing in different places and nothing made them agree. The band
    gate was downgraded to a warning and stayed fatal on two routes for exactly as
    long as it took someone to try publishing: the finding came back marked
    `warning`, the deck was refused, and the reply told the model to remove the bars.

    A kind the checks do not declare at all is a route's own -- `unmapped_page` and
    `overlap` build their findings inside the route that cares -- so this only holds
    the kinds both places name.
    """
    for name in registry.names():
        contradicted = {
            kind
            for kind in registry.get(name).blocking_kinds
            if kind in DISPATCH and DISPATCH[kind] is not Severity.BLOCKING
        }
        assert not contradicted, f"{name} refuses on {sorted(contradicted)}, which the gate only reports"


# The stages that settle what was agreed. `prepare` reads the request and records
# the part of the brief it already states, asking the user for the rest; `brief` is
# the same settlement without the reading. A route may open with either, and with
# nothing else.
_SETTLES_THE_BRIEF = {"brief", "prepare"}


def test_every_route_agrees_the_brief_first_and_publishes_last() -> None:
    """The brief is settled first on every route because everything after it is
    checked against it: the page count, the language of the copy, and the room the
    design is judged for. A route that ingested first could read the materials and
    then be told the deck is for a different audience in a different language."""
    for name in registry.names():
        stages = [s.name for s in registry.get(name).stages]
        assert stages[0] in _SETTLES_THE_BRIEF, f"{name} starts with {stages[0]}"
        assert stages[-1] == "publish", f"{name} ends with {stages[-1]}"
        # Ingest comes before anything is planned or drawn, whatever optional
        # gathering sits between it and the brief.
        assert "ingest" in stages, name
        planning = [s for s in ("plan", "build", "background") if s in stages]
        assert all(stages.index("ingest") < stages.index(step) for step in planning), name


def test_what_was_agreed_refuses_a_deck_on_every_route() -> None:
    """Asking a user to decide something and then ignoring it is worse than not
    asking. The length and the language are checked against the finished file."""
    for name in registry.names():
        assert {"page_budget", "language"} <= registry.get(name).blocking_kinds, name


def test_provenance_refuses_publication_on_every_route() -> None:
    """A page crediting the wrong figure is fatal whichever way the deck was made."""
    for name in registry.names():
        blocking = registry.get(name).blocking_kinds
        assert {"citation"} <= blocking, f"{name} would publish a miscredited figure"


def test_the_routes_differ_in_what_they_let_the_model_emit() -> None:
    """Otherwise there is one route wearing three names."""
    emissions = {
        name: (
            registry.get(name).capabilities.raw_script,
            registry.get(name).capabilities.normalized_regions,
            registry.get(name).capabilities.background_prompt,
        )
        for name in registry.names()
    }
    assert len(set(emissions.values())) == len(emissions), emissions


def test_the_routes_do_not_share_a_backend() -> None:
    backends = [registry.get(n).backend for n in registry.names()]
    assert len(set(backends)) == len(backends), backends


def test_availability_names_the_missing_tools_instead_of_guessing() -> None:
    profile = registry.get("script_author")
    ok, missing = registry.available(profile.name, set(profile.tools))
    assert ok and missing == ()
    ok, missing = registry.available(profile.name, {"ppt_ingest"})
    assert not ok
    assert "ppt_build" in missing


def test_the_image_route_keeps_the_words_out_of_the_picture() -> None:
    """Type baked into a generated image is neither editable nor checkable.

    It cannot be retranslated or corrected, and it is a raster no check can read
    -- so a deck could state anything at all and pass every check.
    Hence two stages rather than one, and a fail-closed kind for the case.
    """
    profile = registry.get("image_text")
    stages = [s.name for s in profile.stages]
    assert stages.index("background") < stages.index("place_text")
    assert "text_in_background" in profile.blocking_kinds


def test_the_script_route_refuses_a_deck_that_dropped_a_planned_figure() -> None:
    """Three fetched pictures, two planned onto pages, none in the deck."""
    from raven.ppt.profiles import registry

    assert "unplaced_figure" in registry.get("script_author").blocking_kinds
