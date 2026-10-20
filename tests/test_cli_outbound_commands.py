"""CLI tests for ``raven outbound``.

The list is where an operator sees a send whose outcome is unknown, and
``resolve`` is where they decide what happened to it. What matters is that
the decision is the operator's and the record follows it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app

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


def _prepare(subject: str = "acme-co"):
    from raven.outbound import prepare_send
    from raven.records.library import records_store

    return prepare_send(
        records_store(),
        subject=subject,
        subject_type="company",
        channel="email",
        body="Hello there",
    )


def test_outbound_help_lists_every_subcommand():
    result = runner.invoke(app, ["outbound", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "ambiguous", "resolve"):
        assert sub in result.stdout, f"missing subcommand in --help: {sub}"


def test_outbound_list_says_so_when_nothing_was_sent(home):
    result = runner.invoke(app, ["outbound", "list"])
    assert result.exit_code == 0
    assert "No outbound sends recorded" in result.stdout


def test_outbound_list_shows_a_prepared_send(home):
    prepared = _prepare()
    result = runner.invoke(app, ["outbound", "list"])
    assert result.exit_code == 0
    assert prepared.token in result.stdout
    assert "acme-co" in result.stdout
    assert "outcome unknown" in result.stdout


def test_ambiguous_lists_the_unresolved_sends(home):
    prepared = _prepare()
    result = runner.invoke(app, ["outbound", "ambiguous"])
    assert result.exit_code == 0
    assert prepared.token in result.stdout
    assert "acme-co" in result.stdout


def test_ambiguous_says_so_when_nothing_is_waiting(home):
    result = runner.invoke(app, ["outbound", "ambiguous"])
    assert result.exit_code == 0
    assert "No send is waiting on a decision" in result.stdout


def test_resolve_needs_to_say_what_happened(home):
    prepared = _prepare()
    result = runner.invoke(app, ["outbound", "resolve", prepared.token])
    assert result.exit_code == 2
    assert "--sent" in result.stdout


def test_resolving_as_sent_moves_the_record(home):
    from raven.outbound import find_by_token
    from raven.records.library import records_store

    prepared = _prepare()
    result = runner.invoke(app, ["outbound", "resolve", prepared.token, "--sent"])
    assert result.exit_code == 0
    assert "as sent" in result.stdout
    assert find_by_token(records_store(), prepared.token).state == "sent"


def test_resolving_as_not_sent_removes_the_record(home):
    from raven.outbound import find_by_token
    from raven.records.library import records_store

    prepared = _prepare()
    result = runner.invoke(app, ["outbound", "resolve", prepared.token, "--not-sent"])
    assert result.exit_code == 0
    assert "did not go out" in result.stdout
    # A record of a message that never left does not belong in the store.
    assert find_by_token(records_store(), prepared.token) is None


def test_resolving_a_token_that_is_not_there(home):
    result = runner.invoke(app, ["outbound", "resolve", "rv-0000000000", "--sent"])
    assert result.exit_code == 1
    assert "No send carries the token" in result.stdout


def test_resolving_a_send_that_is_already_settled(home):
    prepared = _prepare()
    runner.invoke(app, ["outbound", "resolve", prepared.token, "--sent"])
    again = runner.invoke(app, ["outbound", "resolve", prepared.token, "--sent"])
    assert again.exit_code == 0
    assert "nothing to decide" in again.stdout
