"""Unit tests for ``raven.config.update_providers`` — the provider write path."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from raven.config.update_providers import (
    add_provider_model,
    get_provider_config,
    list_providers,
    provider_field_specs,
    remove_provider_model,
    reset_provider,
    set_provider_fields,
)
from raven.config.update_providers import test_provider as probe_provider


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    """Sandboxed config path; the real ``~/.raven/config.json`` is never touched."""
    return tmp_path / "config.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# set_provider_fields
# ---------------------------------------------------------------------------


def test_set_api_key_for_simple_provider(cfg_path: Path) -> None:
    set_provider_fields("openrouter", {"api_key": "sk-or-v1-abc"}, config_path=cfg_path)

    section = _read(cfg_path)["providers"]["openrouter"]
    assert section["apiKey"] == "sk-or-v1-abc"


def test_set_api_base_for_local_provider(cfg_path: Path) -> None:
    set_provider_fields(
        "ollama",
        {"api_base": "http://localhost:11434"},
        config_path=cfg_path,
    )

    section = _read(cfg_path)["providers"]["ollama"]
    assert section["apiBase"] == "http://localhost:11434"


def test_set_complex_provider_azure(cfg_path: Path) -> None:
    set_provider_fields(
        "azure_openai",
        {"api_key": "X", "api_base": "https://x.openai.azure.com"},
        config_path=cfg_path,
    )

    section = _read(cfg_path)["providers"]["azure_openai"]
    assert section["apiKey"] == "X"
    assert section["apiBase"] == "https://x.openai.azure.com"


def test_set_gemini_extra_fields(cfg_path: Path) -> None:
    set_provider_fields(
        "gemini",
        {"api_key": "g-key", "vertex": "true", "api_key_list": "k1,k2,k3"},
        config_path=cfg_path,
    )

    section = _read(cfg_path)["providers"]["gemini"]
    assert section["apiKey"] == "g-key"
    assert section["vertex"] is True
    assert section["apiKeyList"] == ["k1", "k2", "k3"]


def test_set_api_key_for_oauth_provider_raises(cfg_path: Path) -> None:
    with pytest.raises(RuntimeError, match="OAuth"):
        set_provider_fields(
            "github_copilot",
            {"api_key": "ghu_abc"},
            config_path=cfg_path,
        )


def test_set_unknown_provider_raises_with_helpful_message(cfg_path: Path) -> None:
    with pytest.raises(KeyError, match="Unknown provider 'foo'"):
        set_provider_fields("foo", {"api_key": "X"}, config_path=cfg_path)


def test_set_unknown_field_raises_with_helpful_message(cfg_path: Path) -> None:
    with pytest.raises(KeyError, match="Unknown field"):
        set_provider_fields(
            "openrouter",
            {"not_a_field": "X"},
            config_path=cfg_path,
        )


def test_set_empty_fields_returns_empty_dict(cfg_path: Path) -> None:
    assert set_provider_fields("openrouter", {}, config_path=cfg_path) == {}
    assert not cfg_path.exists()


def test_set_returns_previous_values(cfg_path: Path) -> None:
    set_provider_fields("openrouter", {"api_key": "old"}, config_path=cfg_path)
    prev = set_provider_fields("openrouter", {"api_key": "new"}, config_path=cfg_path)
    assert prev == {"api_key": "old"}


def test_set_camelcase_round_trip(cfg_path: Path) -> None:
    set_provider_fields("openrouter", {"api_key": "K", "api_base": "https://x"}, config_path=cfg_path)
    section = _read(cfg_path)["providers"]["openrouter"]
    assert "apiKey" in section and "apiBase" in section
    assert "api_key" not in section and "api_base" not in section


# ---------------------------------------------------------------------------
# get_provider_config
# ---------------------------------------------------------------------------


def test_get_redacts_api_key(cfg_path: Path) -> None:
    set_provider_fields("openrouter", {"api_key": "secret"}, config_path=cfg_path)
    cfg = get_provider_config("openrouter", config_path=cfg_path)
    assert cfg["api_key"] == "****set****"
    assert "secret" not in repr(cfg)


def test_get_with_redact_false_returns_plaintext(cfg_path: Path) -> None:
    set_provider_fields("openrouter", {"api_key": "secret"}, config_path=cfg_path)
    cfg = get_provider_config("openrouter", redact_secrets=False, config_path=cfg_path)
    assert cfg["api_key"] == "secret"


def test_get_unknown_provider_raises(cfg_path: Path) -> None:
    with pytest.raises(KeyError):
        get_provider_config("not-real", config_path=cfg_path)


def test_get_empty_api_key_renders_as_empty(cfg_path: Path) -> None:
    cfg = get_provider_config("openrouter", config_path=cfg_path)
    assert cfg["api_key"] == "(empty)"


def test_gemini_api_key_list_redacted_in_get(cfg_path: Path) -> None:
    set_provider_fields(
        "gemini",
        {"api_key_list": "k1,k2"},
        config_path=cfg_path,
    )
    cfg = get_provider_config("gemini", config_path=cfg_path)
    assert cfg["api_key_list"] == ["****set****", "****set****"]
    assert "k1" not in repr(cfg) and "k2" not in repr(cfg)


def test_gemini_api_key_list_plaintext_with_redact_false(cfg_path: Path) -> None:
    set_provider_fields(
        "gemini",
        {"api_key_list": "k1,k2"},
        config_path=cfg_path,
    )
    cfg = get_provider_config("gemini", redact_secrets=False, config_path=cfg_path)
    assert cfg["api_key_list"] == ["k1", "k2"]


# ---------------------------------------------------------------------------
# reset_provider
# ---------------------------------------------------------------------------


def test_reset_clears_all_fields(cfg_path: Path) -> None:
    set_provider_fields(
        "openrouter",
        {"api_key": "X", "api_base": "https://example.com"},
        config_path=cfg_path,
    )
    reset_provider("openrouter", config_path=cfg_path)

    section = _read(cfg_path)["providers"]["openrouter"]
    assert section["apiKey"] == ""
    assert section.get("apiBase") in (None, "")


def test_reset_clears_oauth_token_file(
    cfg_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "codex.json"
    token_file.write_text('{"access":"X","refresh":"R","expires":0}')
    monkeypatch.setenv("OAUTH_CLI_KIT_TOKEN_PATH", str(token_file))

    reset_provider("openai_codex", config_path=cfg_path)

    assert not token_file.exists()


def test_reset_oauth_idempotent_when_no_token_file(
    cfg_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OAUTH_CLI_KIT_TOKEN_PATH", str(tmp_path / "nonexistent.json"))
    reset_provider("openai_codex", config_path=cfg_path)


# ---------------------------------------------------------------------------
# list_providers
# ---------------------------------------------------------------------------


def test_list_reports_every_provider_with_correct_status(cfg_path: Path) -> None:
    set_provider_fields("openrouter", {"api_key": "X"}, config_path=cfg_path)

    rows = list_providers(config_path=cfg_path)
    by_name = {p["name"]: p for p in rows}

    assert "openrouter" in by_name
    assert by_name["openrouter"]["configured"] is True
    assert by_name["openrouter"]["api_key_redacted"] == "****set****"

    assert by_name["anthropic"]["configured"] is False
    assert by_name["github_copilot"]["is_oauth"] is True
    assert by_name["ollama"]["is_local"] is True
    assert by_name["ollama"]["api_key_redacted"] == "(not needed for local)"

    assert len(rows) >= 18


# ---------------------------------------------------------------------------
# provider_field_specs
# ---------------------------------------------------------------------------


def test_field_specs_includes_is_secret_flag() -> None:
    specs = provider_field_specs("openrouter")
    assert specs["api_key"]["is_secret"] is True
    assert specs["api_base"]["is_secret"] is False


def test_gemini_api_key_list_is_secret_via_workaround() -> None:
    specs = provider_field_specs("gemini")
    assert specs["api_key_list"]["is_secret"] is True


# ---------------------------------------------------------------------------
# test_provider — httpx.MockTransport (no real network)
# ---------------------------------------------------------------------------


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _seed_key(cfg_path: Path, name: str = "openrouter", key: str = "sk-test") -> None:
    set_provider_fields(name, {"api_key": key}, config_path=cfg_path)


def test_test_provider_200_returns_ok_with_models_count(cfg_path: Path) -> None:
    _seed_key(cfg_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer sk-test"
        assert request.url.path.endswith("/v1/models")
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]})

    result = probe_provider("openrouter", config_path=cfg_path, transport=_mock_transport(handler))
    assert result["ok"] is True
    assert result["status"] == "valid"
    assert result["models_count"] == 3
    assert result["http_status"] == 200


def test_test_provider_200_extracts_model_ids(cfg_path: Path) -> None:
    _seed_key(cfg_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "claude-haiku-4-5"},
                    {"id": "claude-sonnet-4-5"},
                    {"id": "openai/gpt-4o"},
                ]
            },
        )

    result = probe_provider("openrouter", config_path=cfg_path, transport=_mock_transport(handler))
    assert result["model_ids"] == [
        "claude-haiku-4-5",
        "claude-sonnet-4-5",
        "openai/gpt-4o",
    ]


def test_test_provider_200_empty_data_returns_empty_model_ids(cfg_path: Path) -> None:
    _seed_key(cfg_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    result = probe_provider("openrouter", config_path=cfg_path, transport=_mock_transport(handler))
    assert result["ok"] is True
    assert result["models_count"] == 0
    assert result["model_ids"] == []


def test_test_provider_200_falls_back_to_name_field(cfg_path: Path) -> None:
    _seed_key(cfg_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "with-id"}, {"name": "name-only"}, {}]},
        )

    result = probe_provider("openrouter", config_path=cfg_path, transport=_mock_transport(handler))
    assert result["model_ids"] == ["with-id", "name-only"]


def test_test_provider_failure_paths_have_none_model_ids(cfg_path: Path) -> None:
    _seed_key(cfg_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    result = probe_provider("openrouter", config_path=cfg_path, transport=_mock_transport(handler))
    assert result["ok"] is False
    assert result["model_ids"] is None


def test_test_provider_network_error_has_none_model_ids(cfg_path: Path) -> None:
    _seed_key(cfg_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    result = probe_provider("openrouter", config_path=cfg_path, transport=_mock_transport(handler))
    assert result["status"] == "network_error"
    assert result["model_ids"] is None


def test_test_provider_401_returns_invalid_key(cfg_path: Path) -> None:
    _seed_key(cfg_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    result = probe_provider("openrouter", config_path=cfg_path, transport=_mock_transport(handler))
    assert result["ok"] is False
    assert result["status"] == "invalid_key"


def test_test_provider_402_returns_no_credits(cfg_path: Path) -> None:
    _seed_key(cfg_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": "no credit"})

    result = probe_provider("openrouter", config_path=cfg_path, transport=_mock_transport(handler))
    assert result["status"] == "no_credits"


def test_test_provider_429_returns_rate_limited(cfg_path: Path) -> None:
    _seed_key(cfg_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    result = probe_provider("openrouter", config_path=cfg_path, transport=_mock_transport(handler))
    assert result["status"] == "rate_limited"


def test_test_provider_network_error_returns_network_error(cfg_path: Path) -> None:
    _seed_key(cfg_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    result = probe_provider("openrouter", config_path=cfg_path, transport=_mock_transport(handler))
    assert result["ok"] is False
    assert result["status"] == "network_error"
    assert "nope" in (result["error"] or "")


def test_test_provider_not_configured_when_api_key_empty(cfg_path: Path) -> None:
    result = probe_provider("openrouter", config_path=cfg_path)
    assert result["ok"] is False
    assert result["status"] == "not_configured"


def test_test_provider_oauth_reads_token_from_oauth_cli_kit(
    cfg_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    fake_token = SimpleNamespace(access="oauth-token-xyz", account_id="me@x")
    fake_module = SimpleNamespace(get_token=lambda: fake_token)
    monkeypatch.setitem(sys.modules, "oauth_cli_kit", fake_module)

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    # openai_codex has default_api_base set; github_copilot doesn't — pick
    # the former so the request can resolve a URL without extra setup.
    result = probe_provider(
        "openai_codex",
        config_path=cfg_path,
        transport=_mock_transport(handler),
    )
    assert result["ok"] is True
    assert seen["auth"] == "Bearer oauth-token-xyz"


def test_test_provider_oauth_missing_token_returns_oauth_token_missing(
    cfg_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    fake_module = SimpleNamespace(get_token=lambda: SimpleNamespace(access=None, account_id=None))
    monkeypatch.setitem(sys.modules, "oauth_cli_kit", fake_module)

    result = probe_provider("openai_codex", config_path=cfg_path)
    assert result["status"] == "oauth_token_missing"


# ---------------------------------------------------------------------------
# Manual model catalog (add_provider_model / remove_provider_model)
# ---------------------------------------------------------------------------


def test_provider_config_models_round_trips(cfg_path: Path) -> None:
    set_provider_fields("openrouter", {"api_key": "sk-or-v1-x"}, config_path=cfg_path)
    add_provider_model("openrouter", "anthropic/claude-sonnet-4-5", config_path=cfg_path)

    section = _read(cfg_path)["providers"]["openrouter"]
    assert section["models"] == ["anthropic/claude-sonnet-4-5"]
    assert "anthropic/claude-sonnet-4-5" in get_provider_config("openrouter", config_path=cfg_path).get("models", [])


def test_add_provider_model_is_idempotent(cfg_path: Path) -> None:
    add_provider_model("openai", "gpt-4o", config_path=cfg_path)
    models = add_provider_model("openai", "gpt-4o", config_path=cfg_path)

    assert models == ["gpt-4o"]
    assert _read(cfg_path)["providers"]["openai"]["models"] == ["gpt-4o"]


def test_add_provider_model_appends_in_order(cfg_path: Path) -> None:
    add_provider_model("openai", "gpt-4o", config_path=cfg_path)
    models = add_provider_model("openai", "gpt-4o-mini", config_path=cfg_path)

    assert models == ["gpt-4o", "gpt-4o-mini"]


def test_remove_provider_model(cfg_path: Path) -> None:
    add_provider_model("openai", "gpt-4o", config_path=cfg_path)
    add_provider_model("openai", "gpt-4o-mini", config_path=cfg_path)

    models = remove_provider_model("openai", "gpt-4o", config_path=cfg_path)

    assert models == ["gpt-4o-mini"]
    assert _read(cfg_path)["providers"]["openai"]["models"] == ["gpt-4o-mini"]


def test_remove_absent_model_is_noop(cfg_path: Path) -> None:
    add_provider_model("openai", "gpt-4o", config_path=cfg_path)
    models = remove_provider_model("openai", "not-there", config_path=cfg_path)

    assert models == ["gpt-4o"]


def test_add_provider_model_unknown_provider_raises(cfg_path: Path) -> None:
    with pytest.raises(KeyError):
        add_provider_model("nonexistent_provider", "x", config_path=cfg_path)


def test_malformed_config_refuses_write_and_preserves_file(cfg_path: Path) -> None:
    # REGRESSION: a present-but-unparseable config must NOT be clobbered.
    from raven.config.loader import ConfigReadError

    original = '{\n  "providers": {"openai": {"apiKey": "sk-o"}},\n  // comment => invalid JSON\n}\n'
    cfg_path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigReadError):
        set_provider_fields("openai", {"api_key": "sk-x"}, config_path=cfg_path)
    assert cfg_path.read_text(encoding="utf-8") == original  # untouched
