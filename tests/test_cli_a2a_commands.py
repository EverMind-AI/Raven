"""The inbound face refuses to start in a sub-agent process, on both hostings."""

from aiohttp import web
from typer.testing import CliRunner

from raven.a2a.gate import refuse_if_subagent
from raven.cli.a2a_commands import a2a_app
from raven.config.schema import A2aConfig

runner = CliRunner()


def test_the_gate_is_open_on_a_host(monkeypatch):
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)
    assert refuse_if_subagent() is None


def test_the_gate_closes_in_a_subagent_process(monkeypatch):
    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    reason = refuse_if_subagent()
    assert reason is not None
    assert "sub-agent" in reason


def test_serve_exits_nonzero_in_a_subagent_process(monkeypatch):
    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    result = runner.invoke(a2a_app, ["serve"])
    assert result.exit_code != 0
    assert "sub-agent" in result.output


def test_the_gateway_mount_is_skipped_in_a_subagent_process(monkeypatch):
    from raven.a2a.gate import mount_if_allowed

    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    app = web.Application()
    cfg = A2aConfig.model_validate({"server": {"enabled": True, "token": "t0ken"}})
    assert mount_if_allowed(app, cfg, handler=object()) is False
    assert [r.resource.canonical for r in app.router.routes()] == []


def test_the_mount_is_skipped_when_disabled(monkeypatch):
    from raven.a2a.gate import mount_if_allowed

    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)
    app = web.Application()
    assert mount_if_allowed(app, A2aConfig(), handler=object()) is False
