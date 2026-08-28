"""Command-surface tests for ``raven acp``."""

from pathlib import Path

from typer.testing import CliRunner

from raven.cli import acp_commands
from raven.cli.commands import app
from raven.config.loader import get_config_path, set_config_path

runner = CliRunner()


def test_acp_uses_the_explicit_config_before_serving(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "agent.json"
    observed: list[Path] = []

    async def serve() -> None:
        observed.append(get_config_path())

    monkeypatch.setattr(acp_commands, "_serve", serve)
    try:
        result = runner.invoke(app, ["acp", "--config", str(config)])
    finally:
        set_config_path(None)  # type: ignore[arg-type]

    assert result.exit_code == 0
    assert result.exception is None
    assert observed == [config]


def test_acp_reports_a_bounded_error_without_a_traceback(monkeypatch) -> None:
    async def serve() -> None:
        raise RuntimeError("provider failed")

    monkeypatch.setattr(acp_commands, "_serve", serve)
    monkeypatch.setattr(acp_commands.logger, "exception", lambda *args, **kwargs: None)

    result = runner.invoke(app, ["acp"])

    assert result.exit_code == 1
    assert "raven acp failed: provider failed" in result.output
    assert "Traceback" not in result.output


def test_acp_log_level_defaults_to_info_and_accepts_an_override(monkeypatch) -> None:
    monkeypatch.delenv("RAVEN_ACP_LOG_LEVEL", raising=False)
    assert acp_commands._file_log_level() == "INFO"

    monkeypatch.setenv("RAVEN_ACP_LOG_LEVEL", " debug ")
    assert acp_commands._file_log_level() == "DEBUG"
