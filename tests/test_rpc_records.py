"""The ``records.*`` and ``sources.*`` RPC surface.

The store and the registry are files under the active agent home, so these
tests point ``RAVEN_HOME`` at a temporary directory and work there. What is
pinned is the contract: a refusal answers with its own code and its own
reason, a record written twice is one record, and no answer claims a thing
the store did not do.

The two libraries are wired together on purpose. A record that keeps a field
its source does not let us keep is refused by the store, and the refusal
names the record and the source so the operator can record the acceptance
and write it again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.errors import (
    InvalidParamsError,
    RecordNotFoundError,
    RecordTransitionRefusedError,
    SourcePolicyRefusedError,
)
from raven.rpc.methods.records import (
    records_get,
    records_grants_set,
    records_list,
    records_transition,
    records_upsert,
)
from raven.rpc.methods.sources import sources_check, sources_list, sources_optin_set

#: A source whose terms forbid keeping its data, and that needs a recorded
#: reason before it may be used at all.
NO_STORE_SOURCE = "linkedin"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the runtime at a temporary agent home."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(root))
    from raven.records.library import set_store_for_tests

    # The store is a process-wide singleton, so it has to be let go between
    # tests or the second one would read the first one's home.
    set_store_for_tests(None)
    yield root
    set_store_for_tests(None)


@pytest.fixture
def company():
    """One company record, written the way a run would write it."""
    return {
        "type": "company",
        "dedupe_key": "acme-co",
        "fields": {"name": "Acme Co", "city": "Kingston"},
        "provenance": [
            {
                "field": "name",
                "source_url": "https://acme.example",
                "source": "",
                "method": "fetch",
                "retrieved_at": "2026-10-20T00:00:00Z",
                "tier": "public",
            }
        ],
    }


# ---------------------------------------------------------------------------
# records.*
# ---------------------------------------------------------------------------


async def test_a_record_written_over_rpc_reads_back_over_rpc(home, company):
    written = await records_upsert(company)
    record = written["record"]
    assert record["dedupe_key"] == "acme-co"
    assert record["type"] == "company"
    assert record["state"] == "new"
    assert record["fields"]["name"] == "Acme Co"

    fetched = await records_get({"id": record["id"]})
    assert fetched["record"]["fields"]["city"] == "Kingston"
    assert fetched["record"]["provenance"][0]["field"] == "name"


async def test_a_record_that_is_not_there_answers_not_found(home):
    with pytest.raises(RecordNotFoundError) as caught:
        await records_get({"id": "no-such-record"})
    assert "no-such-record" in str(caught.value)


async def test_the_same_thing_written_twice_is_one_record(home, company):
    first = await records_upsert(company)
    second = await records_upsert(
        {**company, "fields": {**company["fields"], "phone": "905-555-0100"}}
    )
    assert second["record"]["id"] == first["record"]["id"]
    assert second["record"]["fields"]["name"] == "Acme Co"
    assert second["record"]["fields"]["phone"] == "905-555-0100"

    listed = await records_list({"type": "company"})
    assert listed["total"] == 1


async def test_the_state_only_moves_forward(home, company):
    first = await records_upsert(company)
    await records_transition({"id": first["record"]["id"], "to_state": "discovered"})
    # A second run meeting the same thing brings the state it was in, and a
    # run that found a prospect already pitched does not un-pitch them.
    again = await records_upsert({**company, "state": "new"})
    assert again["record"]["state"] == "discovered"


async def test_a_move_the_chain_allows_is_written_down(home, company):
    written = await records_upsert(company)
    record_id = written["record"]["id"]
    moved = await records_transition(
        {"id": record_id, "to_state": "discovered", "reason": "the site answered"}
    )
    assert moved["record"]["state"] == "discovered"

    from raven.records.library import records_store

    journal = records_store().journal(record_id)
    assert [entry["to_state"] for entry in journal] == ["discovered"]
    assert journal[0]["reason"] == "the site answered"


async def test_a_move_the_chain_refuses_is_refused_with_its_reason(home, company):
    written = await records_upsert(company)
    record_id = written["record"]["id"]
    # A company record may not skip a step, and it has no outcomes of its own.
    with pytest.raises(RecordTransitionRefusedError) as caught:
        await records_transition({"id": record_id, "to_state": "researched"})
    assert "may not move" in str(caught.value)
    # The refused move wrote nothing.
    assert (await records_get({"id": record_id}))["record"]["state"] == "new"


async def test_a_move_of_a_record_that_is_not_there_is_not_found(home):
    with pytest.raises(RecordNotFoundError):
        await records_transition({"id": "no-such-record", "to_state": "discovered"})


async def test_the_list_filters_by_kind_and_by_state(home, company):
    await records_upsert(company)
    await records_upsert({"type": "contact", "dedupe_key": "ada@example.com", "fields": {}})

    assert (await records_list({}))["total"] == 2
    assert (await records_list({"type": "company"}))["total"] == 1
    assert (await records_list({"type": "contact"}))["total"] == 1
    assert (await records_list({"state": "new"}))["total"] == 2
    assert (await records_list({"state": "discovered"}))["total"] == 0


async def test_the_list_is_capped(home, company):
    for index in range(5):
        await records_upsert({**company, "dedupe_key": f"acme-{index}"})
    listed = await records_list({"limit": 2})
    assert len(listed["items"]) == 2
    assert listed["total"] == 5


async def test_a_frame_the_contract_does_not_allow_is_invalid_params(home):
    with pytest.raises(InvalidParamsError):
        await records_upsert({"type": "not-a-kind", "dedupe_key": "x"})
    with pytest.raises(InvalidParamsError):
        await records_get({})


# ---------------------------------------------------------------------------
# The store and the registry, wired together.
# ---------------------------------------------------------------------------


async def test_a_field_the_source_does_not_let_us_keep_is_refused_until_the_acceptance_is_on_record(home, company):
    """The refusal names the record and the source, so the acceptance can be written.

    This is the join between the two libraries: the registry says the terms
    forbid keeping the data, and the store refuses the write until the
    operator has recorded why this deployment may keep it anyway.
    """
    from raven.records.schema import derive_id

    record_id = derive_id("company", "acme-co")
    with pytest.raises(SourcePolicyRefusedError) as caught:
        await records_upsert(
            {
                **company,
                "provenance": [
                    {
                        "field": "profile_data",
                        "source_url": "https://linkedin.example",
                        "source": NO_STORE_SOURCE,
                        "method": "fetch",
                        "retrieved_at": "2026-10-20T00:00:00Z",
                        "tier": "restricted",
                    }
                ],
            }
        )
    message = str(caught.value)
    assert record_id in message
    assert NO_STORE_SOURCE in message

    recorded = await records_grants_set(
        {
            "record_id": record_id,
            "source": NO_STORE_SOURCE,
            "allowed": True,
            "basis": "the operator holds a collection agreement",
        }
    )
    assert recorded["changed"] is True
    assert recorded["grants"][0]["source"] == NO_STORE_SOURCE
    assert recorded["grants"][0]["basis"] == "the operator holds a collection agreement"

    written = await records_upsert(
        {
            **company,
            "provenance": [
                {
                    "field": "profile_data",
                    "source_url": "https://linkedin.example",
                    "source": NO_STORE_SOURCE,
                    "method": "fetch",
                    "retrieved_at": "2026-10-20T00:00:00Z",
                    "tier": "restricted",
                }
            ],
        }
    )
    assert written["record"]["provenance"][0]["source"] == NO_STORE_SOURCE


async def test_an_acceptance_needs_a_basis(home, company):
    written = await records_upsert(company)
    with pytest.raises(InvalidParamsError):
        await records_grants_set(
            {"record_id": written["record"]["id"], "source": NO_STORE_SOURCE, "allowed": True}
        )


async def test_an_acceptance_can_be_withdrawn(home, company):
    written = await records_upsert(company)
    record_id = written["record"]["id"]
    await records_grants_set(
        {
            "record_id": record_id,
            "source": NO_STORE_SOURCE,
            "allowed": True,
            "basis": "a collection agreement",
        }
    )
    withdrawn = await records_grants_set(
        {"record_id": record_id, "source": NO_STORE_SOURCE, "allowed": False}
    )
    assert withdrawn["changed"] is True
    assert withdrawn["grants"] == []


# ---------------------------------------------------------------------------
# sources.*
# ---------------------------------------------------------------------------


async def test_the_registry_lists_every_row(home):
    from raven.sources.policy import SOURCE_POLICIES

    listed = await sources_list({})
    assert listed["total"] == len(SOURCE_POLICIES)
    assert listed["items"], "the registry is not empty"
    row = listed["items"][0]
    assert row["source"]
    assert row["tier"]
    assert row["citation"]
    assert "restrictions" in row


async def test_the_registry_can_be_filtered_by_tier(home):
    from raven.sources.policy import SOURCE_POLICIES

    listed = await sources_list({"tier": "restricted"})
    assert listed["total"] == len(
        [p for p in SOURCE_POLICIES.values() if p.tier.value == "restricted"]
    )
    assert all(item["tier"] == "restricted" for item in listed["items"])


async def test_a_public_source_is_allowed(home):
    checked = await sources_check({"source": "wikidata", "use": "research"})
    assert checked["allowed"] is True
    assert checked["tier"] == "public"
    assert checked["reason"] == ""
    assert checked["citation"]


async def test_a_source_that_is_not_in_the_registry_is_refused_with_a_remedy(home):
    checked = await sources_check({"source": "not-a-source", "use": "research"})
    assert checked["allowed"] is False
    assert "not in the registry" in checked["reason"]
    assert checked["remedy"], "a refusal has to say what would change the answer"
    assert checked["tier"] == ""


async def test_a_restricted_source_is_refused_until_the_acceptance_is_recorded(home):
    checked = await sources_check({"source": NO_STORE_SOURCE, "use": "research"})
    assert checked["allowed"] is False
    assert checked["tier"] == "restricted"
    assert checked["remedy"]

    with_acceptance = await sources_check(
        {"source": NO_STORE_SOURCE, "use": "research", "has_opt_in": True}
    )
    assert with_acceptance["allowed"] is True


async def test_recording_an_acceptance_for_an_unknown_source_is_refused(home):
    with pytest.raises(SourcePolicyRefusedError) as caught:
        await sources_optin_set(
            {
                "source": "not-a-source",
                "allowed": True,
                "basis": "we asked nicely",
            }
        )
    assert "not in the registry" in str(caught.value)


async def test_an_acceptance_needs_a_basis(home):
    with pytest.raises(InvalidParamsError):
        await sources_optin_set({"source": NO_STORE_SOURCE, "allowed": True})


async def test_an_acceptance_can_be_recorded_and_withdrawn(home):
    recorded = await sources_optin_set(
        {
            "source": NO_STORE_SOURCE,
            "allowed": True,
            "basis": "the operator holds an agreement",
            "use": "research",
            "granted_by": "operator",
        }
    )
    assert recorded["changed"] is True
    assert recorded["opt_ins"][0]["source"] == NO_STORE_SOURCE
    assert recorded["opt_ins"][0]["use"] == "research"
    assert recorded["opt_ins"][0]["granted_by"] == "operator"

    # The recorded acceptance is what the check now reads.
    checked = await sources_check({"source": NO_STORE_SOURCE, "use": "research"})
    assert checked["allowed"] is True

    withdrawn = await sources_optin_set(
        {"source": NO_STORE_SOURCE, "allowed": False, "use": "research"}
    )
    assert withdrawn["changed"] is True
    assert withdrawn["opt_ins"] == []

    refused = await sources_check({"source": NO_STORE_SOURCE, "use": "research"})
    assert refused["allowed"] is False


# ---------------------------------------------------------------------------
# The surface itself
# ---------------------------------------------------------------------------


def test_the_wire_models_and_the_domain_enums_agree():
    """The literals on the wire are the domain's own values.

    They are written out rather than imported, so a kind added to the store
    without a change here fails here rather than in a client.
    """
    from typing import get_args

    from raven.records.types import RecordType
    from raven.rpc.models import RecordTypeName, SourceTierName
    from raven.sources.tiers import SourceTier

    assert set(get_args(RecordTypeName)) == {kind.value for kind in RecordType}
    assert set(get_args(SourceTierName)) == {tier.value for tier in SourceTier}


def test_every_new_method_is_registered_on_a_dispatcher():
    from raven.rpc.methods import register_aligned_methods

    dispatcher = Dispatcher()
    register_aligned_methods(dispatcher)
    registered = set(dispatcher.methods())
    for name in (
        "records.get",
        "records.upsert",
        "records.list",
        "records.transition",
        "records.grants.set",
        "sources.list",
        "sources.check",
        "sources.optin.set",
    ):
        assert name in registered, name
