"""Tests for the ``raven ops`` CLI commands."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from raven.cli.ops_commands import ops_app

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _help_text() -> str:
    """Rich styles option names with escape codes between the dashes and the name,
    so an assertion on the raw output can miss "--host". Strip the codes."""
    result = runner.invoke(ops_app, ["tune", "--help"])
    assert result.exit_code == 0
    return _ANSI.sub("", result.output)


def test_tune_help_lists_key_options() -> None:
    text = _help_text()
    for opt in ("--host", "--k1", "--b", "--metric", "--remote-dir"):
        assert opt in text


def test_tune_help_lists_adaptive_options() -> None:
    text = _help_text()
    for opt in ("--adaptive", "--objective", "--max-rounds", "--llm-base-url", "--llm-model"):
        assert opt in text


def test_adaptive_requires_llm_endpoint() -> None:
    result = runner.invoke(ops_app, ["--host", "1.2.3.4", "--adaptive"])
    assert result.exit_code == 1
    assert "requires --llm-base-url and --llm-model" in result.output
