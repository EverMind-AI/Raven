"""Unit tests for `raven deep-research` (enable / get / reset) + shared flow.

Key validation is asserted to hit ``GET /v1/models`` (not a chat completion)
and to NOT double the ``/v1`` segment (the MiroThinker default base already
ends in ``/v1``) -- guarding the F1/round-2 fixes.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import typer
from typer.testing import CliRunner

from raven.cli import deep_research_commands as drc
from raven.cli.deep_research_commands import DEFAULT_MODEL, _validate_key, configure_deep_research, deep_research_app
from raven.config import update_tools as ut

runner = CliRunner()


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "config.json"
    monkeypatch.setattr(ut, "get_config_path", lambda: p)
    # CLI enable does a best-effort validation after writing; stub it so tests
    # never touch the network.
    monkeypatch.setattr(
        drc, "_validate_key", lambda *a, **k: {"ok": True, "status": "ok", "model_ids": [], "error": None}
    )
    return p


# ── _validate_key: GET /v1/models, no /v1 doubling ──


def test_validate_key_ok_hits_v1_models_once():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "mirothinker-1-7-deepresearch-mini"}]})

    res = _validate_key("sk-x", "", transport=httpx.MockTransport(handler))
    assert res["ok"] and "mirothinker-1-7-deepresearch-mini" in res["model_ids"]
    assert seen["path"] == "/v1/models"  # default base already has /v1 -> not /v1/v1/models
    assert seen["auth"] == "Bearer sk-x"


def test_validate_key_401_is_not_ok():
    res = _validate_key("bad", "", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"e": "x"})))
    assert not res["ok"] and res["status"] == "http_401"


def test_validate_key_network_error():
    def boom(r):
        raise httpx.ConnectError("down")

    res = _validate_key("x", "", transport=httpx.MockTransport(boom))
    assert not res["ok"] and res["status"] == "network_error"


# ── enable / get / reset ──


def test_enable_flags_write_camelcase(cfg: Path):
    result = runner.invoke(deep_research_app, ["enable", "--key", "sk-abc", "--model", "mirothinker-1-7-deepresearch"])
    assert result.exit_code == 0
    section = json.loads(cfg.read_text())["tools"]["deepResearch"]
    assert section["apiKey"] == "sk-abc" and section["model"] == "mirothinker-1-7-deepresearch"


def test_enable_key_only_defaults_to_mini(cfg: Path):
    assert runner.invoke(deep_research_app, ["enable", "--key", "sk-x"]).exit_code == 0
    assert json.loads(cfg.read_text())["tools"]["deepResearch"]["model"] == DEFAULT_MODEL


def test_get_redacts_key(cfg: Path):
    runner.invoke(deep_research_app, ["enable", "--key", "sk-x"])
    result = runner.invoke(deep_research_app, ["get"])
    assert result.exit_code == 0 and "****set****" in result.stdout


def test_reset_clears_key(cfg: Path):
    runner.invoke(deep_research_app, ["enable", "--key", "sk-x"])
    assert runner.invoke(deep_research_app, ["reset"]).exit_code == 0
    assert json.loads(cfg.read_text())["tools"]["deepResearch"]["apiKey"] == ""


# ── shared configure flow ──


def test_configure_non_interactive_skips_with_warning():
    warnings: list[str] = []
    assert configure_deep_research(non_interactive=True, warnings=warnings) is False
    assert any("deep_research" in w for w in warnings)


class _FakeQuestionary:
    """Minimal questionary stand-in: select().ask() pops scripted answers."""

    def __init__(self, answers: list) -> None:
        self._answers = list(answers)

    def Choice(self, label, value=None):  # noqa: N802 (mirror questionary API)
        return value

    def select(self, *args, **kwargs):
        val = self._answers.pop(0)
        return type("_Ask", (), {"ask": staticmethod(lambda: val)})()


def test_configure_interactive_happy_path(tmp_path: Path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(ut, "get_config_path", lambda: p)
    monkeypatch.setattr(
        drc, "_validate_key", lambda *a, **k: {"ok": True, "status": "ok", "model_ids": [], "error": None}
    )
    # onboard helpers the flow lazy-imports:
    import raven.cli.onboard_commands as ob

    monkeypatch.setattr(
        ob, "_require_questionary", lambda: _FakeQuestionary(["configure", "mirothinker-1-7-deepresearch"])
    )
    monkeypatch.setattr(ob, "_prompt_api_key", lambda *a, **k: "sk-interactive")

    assert configure_deep_research(non_interactive=False, warnings=[]) is True
    section = json.loads(p.read_text())["tools"]["deepResearch"]
    assert section["apiKey"] == "sk-interactive"
    assert section["model"] == "mirothinker-1-7-deepresearch"


def _setup_interactive(monkeypatch, tmp_path: Path, answers: list, *, validate=None) -> Path:
    """Isolate config + stub the onboard helpers the flow lazy-imports."""
    p = tmp_path / "config.json"
    monkeypatch.setattr(ut, "get_config_path", lambda: p)
    monkeypatch.setattr(
        drc, "_validate_key", validate or (lambda *a, **k: {"ok": True, "status": "ok", "model_ids": [], "error": None})
    )
    import raven.cli.onboard_commands as ob

    monkeypatch.setattr(ob, "_require_questionary", lambda: _FakeQuestionary(answers))
    monkeypatch.setattr(ob, "_prompt_api_key", lambda *a, **k: "sk-typed")
    return p


def test_configure_keep_existing_skips_reprompt(tmp_path: Path, monkeypatch):
    p = _setup_interactive(monkeypatch, tmp_path, ["keep"])
    ut.set_deep_research({"api_key": "sk-old", "model": "m"}, config_path=p)
    assert configure_deep_research(non_interactive=False, warnings=[]) is True
    assert json.loads(p.read_text())["tools"]["deepResearch"]["apiKey"] == "sk-old"  # unchanged


def test_configure_reconfigure_existing_writes_new(tmp_path: Path, monkeypatch):
    p = _setup_interactive(monkeypatch, tmp_path, ["reconfigure", "mirothinker-1-7-deepresearch"])
    ut.set_deep_research({"api_key": "sk-old", "model": "m"}, config_path=p)
    assert configure_deep_research(non_interactive=False, warnings=[]) is True
    assert json.loads(p.read_text())["tools"]["deepResearch"]["apiKey"] == "sk-typed"


def test_configure_skip_when_unconfigured_returns_false(tmp_path: Path, monkeypatch):
    p = _setup_interactive(monkeypatch, tmp_path, ["skip"])
    assert configure_deep_research(non_interactive=False, warnings=[]) is False
    assert not p.exists() or "deepResearch" not in json.loads(p.read_text()).get("tools", {})


def test_configure_validation_fail_then_save(tmp_path: Path, monkeypatch):
    fail = {"ok": False, "status": "http_401", "model_ids": None, "error": "bad"}
    p = _setup_interactive(
        monkeypatch, tmp_path, ["configure", "save", "mirothinker-1-7-deepresearch"], validate=lambda *a, **k: fail
    )
    assert configure_deep_research(non_interactive=False, warnings=[]) is True  # saved despite bad key
    assert json.loads(p.read_text())["tools"]["deepResearch"]["apiKey"] == "sk-typed"


def test_configure_validation_fail_then_cancel(tmp_path: Path, monkeypatch):
    fail = {"ok": False, "status": "http_401", "model_ids": None, "error": "bad"}
    p = _setup_interactive(monkeypatch, tmp_path, ["configure", "cancel"], validate=lambda *a, **k: fail)
    assert configure_deep_research(non_interactive=False, warnings=[]) is False  # cancelled, nothing written


def test_configure_validation_retry_then_ok(tmp_path: Path, monkeypatch):
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        return {"ok": calls["n"] > 1, "status": "http_401" if calls["n"] == 1 else "ok", "model_ids": [], "error": None}

    p = _setup_interactive(
        monkeypatch, tmp_path, ["configure", "retry", "mirothinker-1-7-deepresearch"], validate=_flaky
    )
    assert configure_deep_research(non_interactive=False, warnings=[]) is True
    assert calls["n"] == 2  # first validate failed, retry validated ok


def test_enable_flag_bad_key_warns_but_writes(tmp_path: Path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(ut, "get_config_path", lambda: p)
    monkeypatch.setattr(
        drc, "_validate_key", lambda *a, **k: {"ok": False, "status": "http_401", "model_ids": None, "error": "x"}
    )
    result = runner.invoke(deep_research_app, ["enable", "--key", "sk-bad"])
    assert result.exit_code == 0 and "key validation" in result.stdout
    assert json.loads(p.read_text())["tools"]["deepResearch"]["apiKey"] == "sk-bad"  # saved anyway


def test_enable_with_api_base_flag_writes_it(tmp_path: Path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(ut, "get_config_path", lambda: p)
    monkeypatch.setattr(
        drc, "_validate_key", lambda *a, **k: {"ok": True, "status": "ok", "model_ids": [], "error": None}
    )
    runner.invoke(deep_research_app, ["enable", "--key", "sk-x", "--api-base", "https://mirror.example.com/v1"])
    assert json.loads(p.read_text())["tools"]["deepResearch"]["apiBase"] == "https://mirror.example.com/v1"


def test_enable_no_flags_enters_interactive(monkeypatch):
    called: list = []
    monkeypatch.setattr(drc, "configure_deep_research", lambda **k: called.append(k))
    assert runner.invoke(deep_research_app, ["enable"]).exit_code == 0
    assert called and called[0].get("non_interactive") is False


def test_validate_key_custom_base_without_v1_appends_v1_models():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, json={"data": []})

    # base has no /v1 -> the appender adds /v1/models (the other half of the F1 dedup)
    _validate_key("sk-x", "https://custom.example.com", transport=httpx.MockTransport(handler))
    assert seen["path"] == "/v1/models"


def test_configure_ctrl_c_at_menu_exits(tmp_path: Path, monkeypatch):
    # questionary .ask() returns None on Ctrl+C; the flow must raise typer.Exit,
    # matching onboard's convention (not silently treat it as skip/keep).
    _setup_interactive(monkeypatch, tmp_path, [None])
    with pytest.raises(typer.Exit):
        configure_deep_research(non_interactive=False, warnings=[])


def test_configure_ctrl_c_at_keep_menu_exits(tmp_path: Path, monkeypatch):
    p = _setup_interactive(monkeypatch, tmp_path, [None])
    ut.set_deep_research({"api_key": "sk-old", "model": "m"}, config_path=p)  # configured -> keep/reconfigure menu
    with pytest.raises(typer.Exit):
        configure_deep_research(non_interactive=False, warnings=[])


def test_configure_ctrl_c_at_model_pick_exits(tmp_path: Path, monkeypatch):
    # configure -> key -> validate ok -> Ctrl+C at model select
    _setup_interactive(monkeypatch, tmp_path, ["configure", None])
    with pytest.raises(typer.Exit):
        configure_deep_research(non_interactive=False, warnings=[])


def test_enable_refuses_malformed_config_and_preserves_file(tmp_path: Path, monkeypatch):
    # REGRESSION (real-machine data-loss bug): a malformed real config must not
    # be wiped -- enable exits non-zero and leaves the file untouched.
    p = tmp_path / "config.json"
    monkeypatch.setattr(ut, "get_config_path", lambda: p)
    original = '{\n  "providers": {"openai": {"apiKey": "sk-o"}},\n  // comment => invalid JSON\n}\n'
    p.write_text(original, encoding="utf-8")
    result = runner.invoke(
        deep_research_app, ["enable", "--key", "sk-x", "--model", "mirothinker-1-7-deepresearch-mini"]
    )
    assert result.exit_code != 0
    assert p.read_text(encoding="utf-8") == original  # NOT clobbered


def test_get_refuses_malformed_config(tmp_path: Path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(ut, "get_config_path", lambda: p)
    p.write_text("{  // bad\n}", encoding="utf-8")
    assert runner.invoke(deep_research_app, ["get"]).exit_code != 0


def test_reset_refuses_malformed_config_preserves_file(tmp_path: Path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(ut, "get_config_path", lambda: p)
    original = "{  // bad\n}"
    p.write_text(original, encoding="utf-8")
    assert runner.invoke(deep_research_app, ["reset"]).exit_code != 0
    assert p.read_text(encoding="utf-8") == original  # not clobbered


def test_configure_refuses_malformed_config(tmp_path: Path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(ut, "get_config_path", lambda: p)
    p.write_text("{  // bad\n}", encoding="utf-8")
    with pytest.raises(typer.Exit):  # reads config first -> ConfigReadError -> Exit, no questionary
        configure_deep_research(non_interactive=False, warnings=[])


def test_configure_ctrl_c_at_validation_action_menu_exits(tmp_path: Path, monkeypatch):
    fail = {"ok": False, "status": "http_401", "model_ids": None, "error": "bad"}
    # configure -> key -> validate fails -> Ctrl+C at the retry/save/cancel menu
    _setup_interactive(monkeypatch, tmp_path, ["configure", None], validate=lambda *a, **k: fail)
    with pytest.raises(typer.Exit):
        configure_deep_research(non_interactive=False, warnings=[])
