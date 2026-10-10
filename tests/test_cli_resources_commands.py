"""CLI tests for ``raven resources``.

The command is a seat on the CLI for :mod:`raven.resources`, and the reason it
exists is reach: the wheel carries ``raven/**`` and nothing else, so the
fetcher that used to live under ``scripts/`` was runnable only by a developer
with a checkout. What is pinned here is that reach -- the flags arrive, the
exit status is the fetcher's, and the two runtime errors name a command that a
wheel install can actually run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app

runner = CliRunner()


def test_the_command_is_on_the_cli() -> None:
    result = runner.invoke(app, ["resources", "--help"])

    assert result.exit_code == 0
    assert "--only" in result.output
    assert "--optional" in result.output


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], []),
        (["--optional"], ["--optional"]),
        (["--force"], ["--force"]),
        (["--only", "deepdoc"], ["--only", "deepdoc"]),
        (["--all-layouts"], ["--all-layouts"]),
    ],
)
def test_every_flag_reaches_the_fetcher(monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: list[str]) -> None:
    """Spelled as typer options here, passed as argv there. A flag that stops
    at the CLI is a download that silently does something else."""
    seen: list[list[str]] = []

    def fake_main(passed: list[str] | None = None) -> int:
        seen.append(list(passed or []))
        return 0

    monkeypatch.setattr("raven.resources.main", fake_main)

    result = runner.invoke(app, ["resources", *argv])

    assert result.exit_code == 0
    assert seen == [expected]


def test_a_failed_fetch_is_the_commands_exit_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the shell sees success and an install reports a download
    that did not happen."""
    monkeypatch.setattr("raven.resources.main", lambda passed=None: 1)

    assert runner.invoke(app, ["resources"]).exit_code == 1


def test_the_writer_and_its_readers_agree_on_where_the_files_go() -> None:
    """One derivation, asserted against both readers.

    The fetcher used to resolve its destinations from the repository root,
    which is a directory a wheel install does not have. Deriving them from the
    package is what makes the same call correct for both installs -- and it is
    only correct while it lands where the readers look.
    """
    from raven import resources
    from raven.core import tokenizer
    from raven.knowledge.parser.deepdoc import _onnx

    assert resources.CORE_RES == tokenizer.RES_DIR
    assert resources.KNOWLEDGE_RES == _onnx.RES_DIR
    assert resources.PACKAGE == Path(tokenizer.__file__).resolve().parent.parent


def test_the_dictionary_error_names_a_command_a_wheel_install_can_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`make fetch-resources` and a path under `scripts/` both need a checkout,
    which is exactly what the reader hitting this error does not have."""
    from raven.core import tokenizer

    monkeypatch.setattr(tokenizer, "RES_DIR", tmp_path)

    with pytest.raises(tokenizer.DictionaryMissingError) as caught:
        tokenizer.dictionary_path(required=True)

    assert "raven resources" in str(caught.value)
    assert "scripts/" not in str(caught.value)


def test_the_model_error_names_the_same_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from raven.knowledge.parser.deepdoc import _onnx

    monkeypatch.setattr(_onnx, "RES_DIR", tmp_path)

    with pytest.raises(_onnx.ModelsMissingError) as caught:
        _onnx.model_path("layout", required=True)

    assert "raven resources" in str(caught.value)
    assert "scripts/" not in str(caught.value)
