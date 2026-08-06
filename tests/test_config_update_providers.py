"""Unit tests for ``raven.config.update_providers`` — the provider write path."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from raven.config.update_providers import (
    _copilot_token_dir,
    _oauth_token_path,
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
        "ollama_chat",
        {"api_base": "http://localhost:11434"},
        config_path=cfg_path,
    )

    section = _read(cfg_path)["providers"]["ollama_chat"]
    assert section["apiBase"] == "http://localhost:11434"


def test_setting_a_provider_by_its_former_name_consolidates_on_the_current_one(cfg_path: Path) -> None:
    """A saved config keeps working, and does not end up with two sections.

    Reading folds the spellings together, so leaving the old key behind would
    make the retired section's fields reappear on the next read.
    """
    set_provider_fields("ollama", {"api_base": "http://localhost:11434"}, config_path=cfg_path)

    providers = _read(cfg_path)["providers"]
    assert "ollama" not in providers
    assert providers["ollama_chat"]["apiBase"] == "http://localhost:11434"
    assert get_provider_config("ollama", config_path=cfg_path)["api_base"] == "http://localhost:11434"


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
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "chatgpt"))
    token_file = tmp_path / "chatgpt" / "auth.json"
    token_file.parent.mkdir()
    token_file.write_text('{"access_token":"X","refresh_token":"R"}')

    reset_provider("openai_codex", config_path=cfg_path)

    assert not token_file.exists()


def test_reset_oauth_idempotent_when_no_token_file(
    cfg_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "chatgpt"))
    reset_provider("openai_codex", config_path=cfg_path)


@pytest.fixture
def oauth_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every OAuth credential lookup inside tmp_path.

    Each family reads its directory from an environment variable when one is set,
    which jumps out of the patched home -- and the suite-wide fixture sets all of
    them. Dropping them here is what puts the patched home back in charge.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("GITHUB_COPILOT_TOKEN_DIR", raising=False)
    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    monkeypatch.delenv("MINIMAX_OAUTH_TOKEN_DIR", raising=False)
    return tmp_path


def _configured(cfg_path: Path, slug: str) -> bool:
    return bool({p["name"]: p for p in list_providers(config_path=cfg_path)}[slug]["configured"])


def _write_credential(path: Path, slug: str) -> None:
    """Write whatever shape this family's reader accepts at ``path``.

    MiniMax parses and validates its token file (the resource URL has to be one
    the region actually serves), so a placeholder JSON would read as no token at
    all and the test would pass for the wrong reason.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if slug.startswith("minimax_"):
        from raven.providers.minimax_oauth import oauth_config

        config = oauth_config("global" if slug == "minimax_global" else "cn")
        payload = {
            "access": "X",
            "refresh": "R",
            "expires": 9_999_999_999_000,
            "resource_url": config.default_resource_url,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        return

    if slug == "openai_codex":
        # LiteLLM's driver names these fields, not the kit that used to.
        path.write_text('{"access_token":"X","refresh_token":"R"}', encoding="utf-8")

        return

    path.write_text('{"access":"X","refresh":"R","expires":9999999999000}', encoding="utf-8")


@pytest.mark.parametrize(
    "slug",
    ["openai_codex", "github_copilot", "minimax_global", "minimax_cn"],
)
def test_oauth_credential_is_read_from_ravens_own_directory(
    cfg_path: Path,
    oauth_home: Path,
    slug: str,
) -> None:
    """Whatever writes the credential, one directory answers for all four."""
    assert _configured(cfg_path, slug) is False

    _write_credential(_oauth_token_path(slug), slug)

    assert _configured(cfg_path, slug) is True


@pytest.mark.parametrize(
    "slug",
    ["openai_codex", "github_copilot", "minimax_global", "minimax_cn"],
)
def test_a_file_that_is_not_a_credential_is_not_reported_as_one(
    cfg_path: Path,
    oauth_home: Path,
    slug: str,
) -> None:
    """A truncated write or a hand-edit passes ``exists()`` and then fails on the
    first request, having told the picker and the startup gate it was ready."""
    path = _oauth_token_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("   ", encoding="utf-8")

    assert _configured(cfg_path, slug) is False

    _write_credential(path, slug)

    assert _configured(cfg_path, slug) is True


def test_reset_clears_copilots_api_key_too(cfg_path: Path, oauth_home: Path) -> None:
    """The API key outlives the access token it came from, and LiteLLM keeps
    using it -- a disconnect that leaves it behind does not disconnect."""
    token_dir = _copilot_token_dir()
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "access-token").write_text("ghu_abc")
    (token_dir / "api-key.json").write_text('{"token":"k","expires_at":9999999999}')

    reset_provider("github_copilot", config_path=cfg_path)

    assert list(token_dir.glob("*")) == []


def test_codex_detection_asks_the_module_that_speaks_for_the_driver(oauth_home: Path) -> None:
    """Two derivations of one path is the bug this directory exists to prevent."""
    from raven.providers.chatgpt_token import auth_file

    assert _oauth_token_path("openai_codex") == auth_file()


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
    assert by_name["ollama_chat"]["is_local"] is True
    assert by_name["ollama_chat"]["api_key_redacted"] == "(not needed for local)"

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


def test_test_provider_oauth_sends_the_stored_token(
    cfg_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MiniMax's OAuth families go through the generic probe, with the token the
    login stored as the bearer -- read, never re-acquired, so a report cannot open
    a device flow behind the user."""
    monkeypatch.setattr(
        "raven.providers.minimax_oauth.get_token",
        lambda region: SimpleNamespace(access="oauth-token-xyz", resource_url="https://api.minimax.io/anthropic/v1"),
    )

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["x_api_key"] = request.headers.get("x-api-key")

        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    result = probe_provider(
        "minimax_global",
        config_path=cfg_path,
        transport=_mock_transport(handler),
    )

    assert result["ok"] is True
    assert seen["auth"] == "Bearer oauth-token-xyz"
    assert seen["x_api_key"] == "oauth-token-xyz"


def test_test_provider_oauth_missing_token_returns_oauth_token_missing(
    cfg_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_token(region: str):
        raise RuntimeError("no token stored")

    monkeypatch.setattr("raven.providers.minimax_oauth.get_token", no_token)

    result = probe_provider("minimax_global", config_path=cfg_path)

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


def test_probing_codex_asks_the_endpoint_that_backend_serves(
    cfg_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generic probe requests ``{api_base}/v1/models``, which this backend does
    not serve -- a valid OAuth credential came back refused, reading as a bad key.
    Its catalogue is both the credential check and the list of usable models."""
    monkeypatch.setattr(
        "raven.providers.codex_catalog.account_models",
        lambda timeout=5.0: ("gpt-5.6-sol", "gpt-5.4"),
    )

    result = probe_provider("openai_codex", config_path=cfg_path)

    assert result["ok"] is True
    assert result["status"] == "valid"
    assert result["models_count"] == 2
    assert result["model_ids"] == ["gpt-5.6-sol", "gpt-5.4"]


def test_probing_codex_without_a_usable_credential_says_so(
    cfg_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "raven.providers.chatgpt_token.access_token_and_account",
        lambda: ("token", "acct"),
    )
    monkeypatch.setattr("raven.providers.codex_catalog.account_models", lambda timeout=5.0: ())

    result = probe_provider("openai_codex", config_path=cfg_path)

    assert result["ok"] is False
    assert result["status"] == "oauth_token_missing"
    assert "provider login" in result["error"]


def test_probing_codex_tells_a_missing_credential_from_an_unreachable_one(
    cfg_path: Path,
    oauth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalogue answers an empty list whether nobody is signed in or the
    request never landed, and "sign in" is a different instruction from "you
    appear to be offline". Told apart by what is on disk, which is free."""
    from raven.config.paths import get_oauth_dir

    codex_dir = get_oauth_dir() / "chatgpt"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "auth.json").write_text('{"refresh_token": "stored"}', encoding="utf-8")
    monkeypatch.setattr("raven.providers.codex_catalog.account_models", lambda timeout=5.0: ())

    result = probe_provider("openai_codex", config_path=cfg_path)

    assert result["status"] == "network_error", result
    assert "revoked" in result["error"]
    assert "provider login" not in result["error"], "told to sign in while signed in"


def test_probing_copilot_with_no_credential_does_not_start_a_device_flow(
    cfg_path: Path,
    oauth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLM's copilot authenticator logs in when it finds no token -- a device
    code printed underneath ``provider test`` and a wait for someone to paste it.
    Nothing may reach it before the credential on disk has been seen."""

    def never(self):  # pragma: no cover - reached only if the guard is gone
        raise AssertionError("the authenticator was asked for a key with no credential")

    monkeypatch.setattr(
        "litellm.llms.github_copilot.authenticator.Authenticator.get_api_key",
        never,
    )

    result = probe_provider("github_copilot", config_path=cfg_path)

    assert result["ok"] is False
    assert result["status"] == "oauth_token_missing"
    assert "provider login github-copilot" in result["error"]


def test_probing_copilot_reads_its_own_credential_not_another_providers(
    cfg_path: Path,
    oauth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both providers store a credential under the same OAuth directory, and the
    one the probe reports on has to be the one it was asked about."""
    from raven.config.paths import get_oauth_dir

    assert oauth_home in get_oauth_dir().parents
    codex_dir = get_oauth_dir() / "chatgpt"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "auth.json").write_text('{"access_token": "codex-token"}', encoding="utf-8")

    monkeypatch.setattr(
        "litellm.llms.github_copilot.authenticator.Authenticator.get_api_key",
        lambda self: (_ for _ in ()).throw(AssertionError("asked without a copilot credential")),
    )

    result = probe_provider("github_copilot", config_path=cfg_path)

    assert result["status"] == "oauth_token_missing"
    assert "github-copilot" in result["error"]


@pytest.mark.parametrize(
    "slug",
    [p.name for p in __import__("raven.providers.registry", fromlist=["PROVIDERS"]).PROVIDERS if p.is_oauth],
)
def test_every_oauth_family_has_its_own_credential_check(
    slug: str,
    cfg_path: Path,
    oauth_home: Path,
) -> None:
    """Each family stores its credential its own way, so each needs its own check.
    Defaulting to another family's reads the wrong file and can start that
    provider's device flow behind a probe -- which is what happened when copilot
    shared codex's. A family added without a check of its own lands here."""
    result = probe_provider(slug, config_path=cfg_path, timeout_s=3)

    assert result["status"] == "oauth_token_missing", result
    assert "has no credential check" not in (result["error"] or ""), (
        f"{slug} falls through to another family's credential check"
    )
    assert "provider login" in (result["error"] or "").lower(), result["error"]
