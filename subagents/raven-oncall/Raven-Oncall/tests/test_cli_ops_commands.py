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
    # Named, not implied. `ops` collapsed to `tune` only while `tune` was its one
    # command -- a typer convenience, not a shape anyone chose -- and `connection`
    # ends it. Nothing outside this line ever used the collapsed form: the two
    # places the trunk spells this call, ops.py's module docstring and
    # runner.py's, both already write `raven ops tune`.
    result = runner.invoke(ops_app, ["tune", "--host", "1.2.3.4", "--adaptive"])
    assert result.exit_code == 1
    assert "requires --llm-base-url and --llm-model" in result.output
