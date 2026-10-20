"""CLI tests for ``raven records`` and ``raven sources``.

The refusal text a source gets when its terms do not permit a use names the
command that would change the answer. That wording is a promise, and one of
these tests runs the command the promise names — so the two cannot drift
apart without a test failing.

The store and the opt-in record are files under the active agent home, so
these tests point ``RAVEN_HOME`` at a temporary directory and work there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.sources.policy import SourceRefusedError, consult

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the runtime at a temporary agent home."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(root))
    from raven.records.library import set_store_for_tests

    set_store_for_tests(None)
    yield root
    set_store_for_tests(None)


def _write_record(dedupe_key: str = "acme-co", **fields) -> str:
    """Write one record through the library and return its id."""
    from raven.records.library import records_store
    from raven.records.types import Record, RecordType

    stored = records_store().put(
        Record(type=RecordType.COMPANY, dedupe_key=dedupe_key, fields=fields or {"name": "Acme"})
    )
    return stored.id


# ---------------------------------------------------------------------------
# The command groups exist
# ---------------------------------------------------------------------------


def test_records_help_lists_every_subcommand():
    result = runner.invoke(app, ["records", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "get", "grant", "revoke-grant"):
        assert sub in result.stdout, f"missing subcommand in --help: {sub}"


def test_sources_help_lists_every_subcommand():
    result = runner.invoke(app, ["sources", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "check", "optin", "revoke-optin"):
        assert sub in result.stdout, f"missing subcommand in --help: {sub}"


# ---------------------------------------------------------------------------
# raven records
# ---------------------------------------------------------------------------


def test_records_list_says_so_when_there_are_none(home):
    result = runner.invoke(app, ["records", "list"])
    assert result.exit_code == 0
    assert "No records yet" in result.stdout


def test_records_list_refuses_an_unknown_kind(home):
    result = runner.invoke(app, ["records", "list", "--type", "not-a-kind"])
    assert result.exit_code == 2
    assert "Unknown record kind" in result.stdout


def test_records_list_shows_the_records(home):
    _write_record()
    result = runner.invoke(app, ["records", "list"])
    assert result.exit_code == 0
    assert "acme-co" in result.stdout
    assert "company" in result.stdout


def test_records_get_shows_one_record_with_its_fields(home):
    record_id = _write_record(name="Acme")
    result = runner.invoke(app, ["records", "get", record_id])
    assert result.exit_code == 0
    assert "Acme" in result.stdout
    assert record_id in result.stdout


def test_records_get_says_so_when_the_record_is_not_there(home):
    result = runner.invoke(app, ["records", "get", "no-such-record"])
    assert result.exit_code == 1
    assert "No record carries the id" in result.stdout


def test_records_grant_needs_a_basis(home):
    record_id = _write_record()
    result = runner.invoke(app, ["records", "grant", record_id, "linkedin"])
    assert result.exit_code != 0


def test_records_grant_records_and_revokes(home):
    record_id = _write_record()
    recorded = runner.invoke(
        app, ["records", "grant", record_id, "linkedin", "--basis", "an agreement we hold"]
    )
    assert recorded.exit_code == 0
    assert "Recorded" in recorded.stdout

    from raven.records.grants import read_grants

    assert [grant["source"] for grant in read_grants(record_id)] == ["linkedin"]

    revoked = runner.invoke(app, ["records", "revoke-grant", record_id, "linkedin"])
    assert revoked.exit_code == 0
    assert "Withdrawn" in revoked.stdout
    assert read_grants(record_id) == []


def test_records_revoke_grant_says_so_when_there_is_nothing_to_withdraw(home):
    record_id = _write_record()
    result = runner.invoke(app, ["records", "revoke-grant", record_id, "linkedin"])
    assert result.exit_code == 0
    assert "Nothing to withdraw" in result.stdout


# ---------------------------------------------------------------------------
# raven sources
# ---------------------------------------------------------------------------


def test_sources_list_shows_the_registry(home):
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0
    assert "linkedin" in result.stdout
    assert "restricted" in result.stdout


def test_sources_list_can_be_filtered_by_tier(home):
    result = runner.invoke(app, ["sources", "list", "--tier", "public"])
    assert result.exit_code == 0
    assert "wikidata" in result.stdout
    assert "linkedin" not in result.stdout


def test_sources_list_refuses_an_unknown_tier(home):
    result = runner.invoke(app, ["sources", "list", "--tier", "not-a-tier"])
    assert result.exit_code == 2


def test_sources_check_says_allowed_for_a_public_source(home):
    result = runner.invoke(app, ["sources", "check", "wikidata", "--use", "research"])
    assert result.exit_code == 0
    assert "Allowed" in result.stdout


def _flat(text: str) -> str:
    """The output as one line, so a word-wrapped command still matches.

    The console wraps long lines for reading; the command itself is
    unchanged, and a test that looks for it should not care where the
    terminal put the break.
    """
    return " ".join(text.split())


def test_sources_check_says_why_not_and_how_to_change_it(home):
    result = runner.invoke(app, ["sources", "check", "linkedin", "--use", "research"])
    assert result.exit_code == 1
    assert "refused by source policy" in result.stdout
    assert "raven sources optin linkedin" in _flat(result.stdout)


def test_sources_optin_refuses_a_source_that_is_not_in_the_registry(home):
    result = runner.invoke(
        app, ["sources", "optin", "not-a-source", "--basis", "we asked nicely"]
    )
    assert result.exit_code == 2
    assert "not in the registry" in result.stdout


def test_sources_optin_needs_a_basis(home):
    result = runner.invoke(app, ["sources", "optin", "linkedin"])
    assert result.exit_code != 0


def test_sources_optin_records_and_changes_the_answer(home):
    recorded = runner.invoke(
        app, ["sources", "optin", "linkedin", "--basis", "an agreement we hold", "--use", "research"]
    )
    assert recorded.exit_code == 0
    assert "Recorded" in recorded.stdout

    checked = runner.invoke(app, ["sources", "check", "linkedin", "--use", "research"])
    assert checked.exit_code == 0
    assert "Allowed" in checked.stdout

    revoked = runner.invoke(app, ["sources", "revoke-optin", "linkedin", "--use", "research"])
    assert revoked.exit_code == 0
    assert "Withdrawn" in revoked.stdout

    refused = runner.invoke(app, ["sources", "check", "linkedin", "--use", "research"])
    assert refused.exit_code == 1


# ---------------------------------------------------------------------------
# The promise the refusal text makes
# ---------------------------------------------------------------------------


def test_the_command_a_refusal_names_actually_exists(home):
    """A refusal names the command that would change the answer.

    The wording is written in the registry, and it names a command in the
    CLI. This runs the command the refusal names, so the two cannot drift
    apart without a test failing.
    """
    with pytest.raises(SourceRefusedError) as caught:
        consult("linkedin", use="research", has_opt_in=False)
    remedy = caught.value.remedy

    match = re.search(r"raven sources optin (\S+)", remedy)
    assert match, f"the refusal does not name a command: {remedy!r}"
    source = match.group(1)

    result = runner.invoke(app, ["sources", "optin", source, "--basis", "an agreement we hold"])
    assert result.exit_code == 0, result.stdout
    assert "Recorded" in result.stdout

    # And the command the store's refusal names for a grant.
    from raven.records.store import RecordStore

    store = RecordStore()
    from raven.records.types import Provenance, Record, RecordType

    with pytest.raises(Exception) as refusal:
        store.put(
            Record(
                type=RecordType.COMPANY,
                dedupe_key="acme-co",
                fields={"name": "Acme"},
                provenance=[
                    Provenance(
                        field="profile_data",
                        source_url="https://linkedin.example",
                        source="linkedin",
                    )
                ],
            )
        )
    message = str(refusal.value)
    grant_match = re.search(r"raven records grant (\S+) (\S+)", message)
    assert grant_match, f"the refusal does not name a command: {message!r}"
    record_id, source = grant_match.group(1), grant_match.group(2)

    granted = runner.invoke(
        app, ["records", "grant", record_id, source, "--basis", "an agreement we hold"]
    )
    assert granted.exit_code == 0, granted.stdout
