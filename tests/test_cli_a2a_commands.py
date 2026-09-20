"""The inbound face refuses to start in a sub-agent process, on both hostings."""

import json

import pytest
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


def test_the_gateway_facade_builds_nothing_when_the_face_is_off(monkeypatch):
    """A disabled face must not assemble a handler, which is what keeps the
    default-OFF promise free: building one imports a2a-sdk."""
    from raven.a2a.gate import mount_gateway_face

    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)
    app = web.Application()
    called = []
    assert mount_gateway_face(app, A2aConfig(), agent_loop_factory=lambda: called.append(1)) is None
    assert list(app.router.routes()) == []
    assert called == []


def test_the_gateway_facade_mounts_a_real_handler_when_enabled(monkeypatch):
    from raven.a2a.gate import mount_gateway_face

    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)
    app = web.Application()
    cfg = A2aConfig.model_validate({"server": {"enabled": True, "token": "t0ken"}})
    handler = mount_gateway_face(app, cfg, agent_loop_factory=lambda: None)
    assert handler is not None
    assert hasattr(handler, "on_message_send")
    assert list(app.router.routes()) != []


def test_the_gateway_facade_is_refused_in_a_subagent_process(monkeypatch):
    from raven.a2a.gate import mount_gateway_face

    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    app = web.Application()
    cfg = A2aConfig.model_validate({"server": {"enabled": True, "token": "t0ken"}})
    assert mount_gateway_face(app, cfg, agent_loop_factory=lambda: None) is None
    assert list(app.router.routes()) == []


# ---------------------------------------------------------------------------
# `raven a2a enable` / `raven a2a disable`
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg_at(tmp_path, monkeypatch):
    """Point the config writes this module drives at a throwaway file."""
    path = tmp_path / "config.json"
    monkeypatch.setattr("raven.config.update.get_config_path", lambda: path)
    return path


def _server(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["a2a"]["server"]


def test_enable_opens_the_face_from_a_fresh_install(cfg_at, monkeypatch):
    """The install ships the face off, so this command is the whole opt-in."""
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)

    result = runner.invoke(a2a_app, ["enable"])

    assert result.exit_code == 0, result.output
    server = _server(cfg_at)
    assert server["enabled"] is True and server["token"]


def test_enable_never_prints_the_token(cfg_at, monkeypatch):
    """It is the credential: it belongs in the file, not in a terminal buffer."""
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)

    result = runner.invoke(a2a_app, ["enable"])

    assert _server(cfg_at)["token"] not in result.output


def test_disable_closes_the_face(cfg_at, monkeypatch):
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)
    runner.invoke(a2a_app, ["enable"])

    result = runner.invoke(a2a_app, ["disable"])

    assert result.exit_code == 0, result.output
    assert _server(cfg_at)["enabled"] is False


def test_a_disabled_face_does_not_mount(cfg_at, monkeypatch):
    """The command's write is what `may_mount` reads, not a separate switch."""
    from raven.a2a.gate import may_mount

    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)
    runner.invoke(a2a_app, ["enable"])
    assert may_mount(A2aConfig.model_validate({"server": _server(cfg_at)})) is True

    runner.invoke(a2a_app, ["disable"])
    assert may_mount(A2aConfig.model_validate({"server": _server(cfg_at)})) is False
