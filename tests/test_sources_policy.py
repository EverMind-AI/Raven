"""What each source permits, and what the registry does about it.

The registry is the thing that stands between a deployment and a notice from
a source whose terms it did not read. So the rules are pinned here: which
sources are closed, which uses are closed, that a source nobody has looked at
is closed, and that a refusal says which rule was hit and what would change
the answer -- because the person who can change it is the operator.
"""

from __future__ import annotations

import pytest

from raven.sources import (
    SOURCE_POLICIES,
    SourcePolicyError,
    SourceRefusedError,
    SourceTier,
    consult,
    delete_optin,
    has_opt_in,
    optins_path,
    policy_for,
    read_optins,
    write_optin,
)


def test_every_source_carries_the_terms_it_is_judged_against() -> None:
    for source, policy in SOURCE_POLICIES.items():
        assert policy.citation.startswith("http"), source
        assert policy.label, source


def test_the_closed_sources_carry_the_terms_that_close_them() -> None:
    closed = [p for p in SOURCE_POLICIES.values() if p.tier is SourceTier.RESTRICTED]
    assert closed, "the registry is supposed to close something"
    for policy in closed:
        assert policy.citation.startswith("http"), policy.source
        assert policy.restrictions, f"{policy.source} is closed without saying which use is closed"


def test_a_source_nobody_has_looked_at_is_refused() -> None:
    assert policy_for("a_service_that_does_not_exist") is None

    with pytest.raises(SourceRefusedError) as raised:
        consult("a_service_that_does_not_exist", use="call")

    assert "not in the registry" in str(raised.value)
    assert "raven/sources/policy.py" in raised.value.remedy


def test_a_closed_source_is_refused_until_the_operator_says_why() -> None:
    with pytest.raises(SourceRefusedError) as raised:
        consult("linkedin", use="call")

    assert "do not permit" in str(raised.value)
    assert "optin" in raised.value.remedy


def test_a_source_the_operator_has_agreed_to_may_be_used() -> None:
    write_optin("linkedin", basis="a contract we hold", use="call")
    try:
        assert has_opt_in("linkedin", "call") is True
        policy = consult("linkedin", use="call", has_opt_in=True)
        assert policy.source == "linkedin"
    finally:
        delete_optin("linkedin", "call")


def test_an_opt_in_covers_the_use_it_names_and_no_other() -> None:
    write_optin("linkedin", basis="a contract we hold", use="call")
    try:
        assert has_opt_in("linkedin", "call") is True
        assert has_opt_in("linkedin", "profile_data") is False
    finally:
        delete_optin("linkedin", "call")


def test_a_use_a_source_does_not_permit_is_refused_on_its_own() -> None:
    with pytest.raises(SourceRefusedError) as raised:
        consult("google_places", use="derivation")

    assert "forbid creating content" in str(raised.value)

    with pytest.raises(SourceRefusedError):
        consult("cloudflare_radar", use="commercial")


def test_the_places_reading_is_allowed_where_the_derivation_is_not() -> None:
    policy = consult("google_places", use="call")
    assert policy.store_allowed is False
    assert policy.derivation_allowed is False
    assert policy.attribution_required is True


def test_the_places_terms_forbid_keeping_anything_but_the_place_id() -> None:
    policy = policy_for("google_places")
    assert "store" in dict(policy.restrictions)
    assert "place id" in dict(policy.restrictions)["store"]


def test_the_nominatim_clause_names_the_platforms_it_refuses() -> None:
    policy = policy_for("nominatim")
    reason = dict(policy.restrictions)["platform"]
    assert "vibe-coding" in reason
    assert policy.tier is SourceTier.RESTRICTED


def test_overpass_is_the_supported_way_to_read_the_same_data() -> None:
    overpass = policy_for("overpass")
    nominatim = policy_for("nominatim")
    assert overpass.tier is SourceTier.PUBLIC
    assert nominatim.tier is SourceTier.RESTRICTED


def test_the_closed_company_data_stays_closed_for_a_generative_pipeline() -> None:
    policy = policy_for("crunchbase")
    assert "train models" in dict(policy.restrictions)["generative_pipeline"]
    assert policy.tier is SourceTier.LICENSED


def test_an_opt_in_without_a_reason_is_not_accepted() -> None:
    with pytest.raises(Exception):
        write_optin("linkedin", basis="")


def test_the_opt_in_record_is_readable_and_can_be_withdrawn() -> None:
    write_optin("linkedin", basis="a contract we hold", use="call")
    assert "linkedin" in read_optins()

    assert delete_optin("linkedin", "call") is True
    assert "linkedin" not in read_optins()


def test_a_missing_opt_in_record_reads_as_no_opt_ins() -> None:
    assert read_optins() == {}
    assert has_opt_in("linkedin", "call") is False
    assert delete_optin("linkedin", "call") is False


def test_an_opt_in_record_that_cannot_be_read_is_reported() -> None:
    path = optins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json", encoding="utf-8")

    with pytest.raises(SourcePolicyError):
        read_optins()
    with pytest.raises(SourcePolicyError):
        write_optin("linkedin", basis="a contract we hold", use="call")
    with pytest.raises(SourcePolicyError):
        delete_optin("linkedin", "call")


def test_an_opt_in_record_that_is_not_a_mapping_is_reported() -> None:
    path = optins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(SourcePolicyError):
        read_optins()
    assert delete_optin("linkedin", "call") is False


def test_an_opt_in_must_name_a_source() -> None:
    with pytest.raises(SourcePolicyError):
        write_optin("", basis="a contract we hold", use="call")


def test_a_withdrawal_takes_the_whole_source_when_no_use_is_named() -> None:
    write_optin("linkedin", basis="a contract we hold", use="call")
    write_optin("linkedin", basis="a contract we hold, for the record too", use="store")

    assert delete_optin("linkedin") is True
    assert "linkedin" not in read_optins()


def test_a_withdrawal_leaves_the_other_uses_alone() -> None:
    write_optin("linkedin", basis="a contract we hold", use="call")
    write_optin("linkedin", basis="a contract we hold, for the record too", use="store")

    assert delete_optin("linkedin", "call") is True
    remaining = read_optins()["linkedin"]
    assert [grant["use"] for grant in remaining] == ["store"]


def test_a_withdrawal_of_a_use_that_was_never_given_removes_nothing() -> None:
    write_optin("linkedin", basis="a contract we hold", use="call")

    assert delete_optin("linkedin", "store") is False
    assert "linkedin" in read_optins()


def test_every_offered_search_vendor_has_a_policy_row():
    """A vendor the tools offer may not be one the policy has never heard of.

    The policy is what decides whether a source's terms permit a use, and it
    decides by the registry. A vendor in the tool's own provider table with no
    registry row is a vendor the guard refuses on every request - so the
    offering and the registry have to agree, and this is where they are made
    to agree.
    """
    from raven.agent.tools.web import SEARCH_PROVIDERS
    from raven.sources.policy import SOURCE_POLICIES

    missing = sorted(set(SEARCH_PROVIDERS) - set(SOURCE_POLICIES))
    assert not missing, f"no policy row for: {', '.join(missing)}"


def test_every_vendor_the_research_flow_offers_has_a_policy_row() -> None:
    """The flow's own vendor tables have to agree with the registry too.

    The flow offers its search and fetch vendors separately from the host's
    tools, and a vendor in either table with no registry row is a vendor the
    guard refuses on every request - which is how TinyFish ended up offered,
    selectable, and unusable at the same time.
    """
    import sys
    from pathlib import Path

    flow_dir = Path(__file__).resolve().parents[1] / "agents" / "raven-research" / "plugins" / "research-flow"
    sys.path.insert(0, str(flow_dir))
    try:
        from research_flow.tools.web import FETCH_PROVIDERS, SEARCH_PROVIDERS
    finally:
        sys.path.remove(str(flow_dir))

    missing = sorted((set(SEARCH_PROVIDERS) | set(FETCH_PROVIDERS)) - set(SOURCE_POLICIES))
    assert not missing, f"no policy row for: {', '.join(missing)}"
