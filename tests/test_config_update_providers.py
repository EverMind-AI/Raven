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
    add_provider_endpoint,
    add_provider_model,
    get_provider_config,
    list_provider_endpoints,
    list_providers,
    provider_field_specs,
    remove_provider_endpoint,
    remove_provider_model,
    reset_provider,
    set_provider_fields,
)
from raven.config.update_providers import test_provider as probe_provider
from raven.providers.registry import PROVIDERS as _PROVIDERS


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
        {"api_key": "g-key", "api_key_list": "k1,k2,k3"},
        config_path=cfg_path,
    )

    section = _read(cfg_path)["providers"]["gemini"]
    assert section["apiKey"] == "g-key"
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


def test_get_redacts_api_key_nested_inside_endpoints(cfg_path: Path) -> None:
    add_provider_endpoint("openrouter", label="a", api_key="sk-SUPER-SECRET-A", config_path=cfg_path)
    add_provider_endpoint("openrouter", label="b", api_key="sk-SUPER-SECRET-B", config_path=cfg_path)

    cfg = get_provider_config("openrouter", config_path=cfg_path)

    assert [ep.api_key for ep in cfg["endpoints"]] == ["****set****", "****set****"]
    assert "sk-SUPER-SECRET-A" not in repr(cfg)
    assert "sk-SUPER-SECRET-B" not in repr(cfg)
    # Non-secret fields on the same endpoint pass through untouched.
    assert [ep.label for ep in cfg["endpoints"]] == ["a", "b"]


def test_get_endpoints_plaintext_with_redact_false(cfg_path: Path) -> None:
    add_provider_endpoint("openrouter", label="a", api_key="sk-SUPER-SECRET-A", config_path=cfg_path)

    cfg = get_provider_config("openrouter", redact_secrets=False, config_path=cfg_path)

    assert cfg["endpoints"][0].api_key == "sk-SUPER-SECRET-A"


def test_get_endpoints_empty_key_renders_as_empty(cfg_path: Path) -> None:
    # hosted_vllm rather than openrouter: a key-based provider now refuses to
    # persist a keyless endpoint (see the write-time tests in the endpoints
    # section below) -- a local deployment is the shape that legitimately has
    # none, and the redaction rule under test does not depend on which.
    add_provider_endpoint("hosted_vllm", label="a", api_base="http://localhost:8000/v1", config_path=cfg_path)

    cfg = get_provider_config("hosted_vllm", config_path=cfg_path)

    assert cfg["endpoints"][0].api_key == "(empty)"


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


def test_list_reports_endpoints_only_provider_key_state_consistently(cfg_path: Path) -> None:
    """No flat ``api_key`` set, only ``endpoints`` -- the key column must not say
    ``(empty)`` while the same row's ``configured`` says the credential is present."""
    add_provider_endpoint("openrouter", label="a", api_key="k1", config_path=cfg_path)
    add_provider_endpoint("openrouter", label="b", api_key="k2", config_path=cfg_path)

    row = {p["name"]: p for p in list_providers(config_path=cfg_path)}["openrouter"]

    assert row["configured"] is True
    assert row["api_key_redacted"] == "****set**** (2 endpoints)"


def test_list_does_not_call_keyless_endpoints_set(cfg_path: Path) -> None:
    """The mirror direction of the consistency rule: endpoints whose keys are
    all empty hold no credential, so the key column must not say set while
    credential_status says the section is unconfigured.

    Blanked out by hand after the write rather than passed to
    ``add_provider_endpoint`` directly: that function now refuses to persist a
    keyless endpoint for a key-based provider like openrouter, so a section
    shaped like this can only exist from a config written before that rule, or
    hand-edited -- ``list_providers`` still has to describe it accurately.
    """
    add_provider_endpoint("openrouter", label="a", api_key="k1", api_base="https://a.example/v1", config_path=cfg_path)
    data = json.loads(cfg_path.read_text())
    data["providers"]["openrouter"]["endpoints"][0]["apiKey"] = ""
    cfg_path.write_text(json.dumps(data))

    row = {p["name"]: p for p in list_providers(config_path=cfg_path)}["openrouter"]

    assert row["configured"] is False
    assert row["api_key_redacted"] == "(empty) (1 endpoints)"


def test_list_flat_key_residue_does_not_paper_over_keyless_endpoints(cfg_path: Path) -> None:
    """A stale flat ``api_key`` left behind by an ``endpoints`` migration must
    not display as set while ``configured`` -- decided off the endpoints list,
    same as every other gate -- says the section is not usable.
    """
    add_provider_endpoint("openrouter", label="a", api_key="k1", api_base="https://a.example/v1", config_path=cfg_path)
    data = json.loads(cfg_path.read_text())
    data["providers"]["openrouter"]["apiKey"] = "stale-flat-key"
    data["providers"]["openrouter"]["endpoints"][0]["apiKey"] = ""
    cfg_path.write_text(json.dumps(data))

    row = {p["name"]: p for p in list_providers(config_path=cfg_path)}["openrouter"]

    assert row["configured"] is False
    assert row["api_key_redacted"] == "(empty) (1 endpoints)"


def test_endpoint_ops_refuse_an_invalid_section_instead_of_wiping_it(cfg_path: Path) -> None:
    """A section that no longer validates must stop the endpoint commands loudly.

    Swallowing the error made them see an empty list and write it back: a
    hand-edited duplicate label -- which already stops Raven from starting --
    plus one `provider endpoint add` erased every real endpoint in the
    section, on exactly the command a user would reach for to fix things.
    """
    import json

    from pydantic import ValidationError

    add_provider_endpoint("openrouter", label="keep-1", api_key="k1", config_path=cfg_path)
    add_provider_endpoint("openrouter", label="keep-2", api_key="k2", config_path=cfg_path)
    data = json.loads(cfg_path.read_text())
    data["providers"]["openrouter"]["endpoints"].append({"label": "keep-1", "apiKey": "dup"})
    cfg_path.write_text(json.dumps(data))

    with pytest.raises(ValidationError):
        add_provider_endpoint("openrouter", label="new", api_key="k3", config_path=cfg_path)

    survivors = json.loads(cfg_path.read_text())["providers"]["openrouter"]["endpoints"]
    assert [ep["label"] for ep in survivors] == ["keep-1", "keep-2", "keep-1"]


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


def test_test_provider_endpoints_only_section_probes_the_endpoints_key(cfg_path: Path) -> None:
    """No flat ``api_key`` at all -- the credential lives only in ``endpoints``.

    The probe used to read ``cfg.get("api_key")`` directly, which is empty for
    an endpoints-only section, and reported ``not_configured`` on a section the
    runtime could already serve. It must read the same resolved list
    (``provider_endpoints``) that a real request does -- including that
    request's own ``api_base``, not the vendor default.
    """
    add_provider_endpoint(
        "openrouter",
        label="primary",
        api_key="sk-or-endpoint-test",
        api_base="https://example-endpoint.test/v1",
        config_path=cfg_path,
    )

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    result = probe_provider("openrouter", config_path=cfg_path, transport=_mock_transport(handler))

    assert result["ok"] is True
    assert result["status"] == "valid"
    assert seen["auth"] == "Bearer sk-or-endpoint-test"
    assert seen["url"].startswith("https://example-endpoint.test/v1")


def test_test_provider_gemini_api_key_list_section_probes_the_first_key(cfg_path: Path) -> None:
    """Same gap, Gemini's shape: a plural ``api_key_list`` and no flat ``api_key``."""
    set_provider_fields(
        "gemini",
        {"api_key_list": "AIzaTEST1,AIzaTEST2", "api_base": "https://example-gemini.test/v1"},
        config_path=cfg_path,
    )

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": []})

    result = probe_provider("gemini", config_path=cfg_path, transport=_mock_transport(handler))

    assert result["ok"] is True
    assert result["status"] == "valid"
    assert seen["auth"] == "Bearer AIzaTEST1"


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
    # Stored under the name that resolves back to openrouter: the bare vendor id
    # resolves to Anthropic, so a gateway's list said the vendor served it.
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


# ---------------------------------------------------------------------------
# Endpoints (add_provider_endpoint / remove_provider_endpoint / list_provider_endpoints)
# ---------------------------------------------------------------------------


def test_add_provider_endpoint_appends(cfg_path: Path) -> None:
    endpoints = add_provider_endpoint("openrouter", label="primary", api_key="k1", config_path=cfg_path)

    assert [e.label for e in endpoints] == ["primary"]
    section = _read(cfg_path)["providers"]["openrouter"]
    assert section["endpoints"] == [{"label": "primary", "apiKey": "k1", "apiBase": None, "extraHeaders": None}]


def test_add_provider_endpoint_appends_a_second_label(cfg_path: Path) -> None:
    add_provider_endpoint("openrouter", label="primary", api_key="k1", config_path=cfg_path)
    endpoints = add_provider_endpoint("openrouter", label="backup", api_key="k2", config_path=cfg_path)

    assert [e.label for e in endpoints] == ["primary", "backup"]


def test_add_provider_endpoint_same_label_replaces_wholesale(cfg_path: Path) -> None:
    add_provider_endpoint(
        "openrouter",
        label="primary",
        api_key="k1",
        api_base="https://old.example.com",
        extra_headers={"X-Old": "1"},
        config_path=cfg_path,
    )
    endpoints = add_provider_endpoint("openrouter", label="primary", api_key="k2", config_path=cfg_path)

    # A field omitted on the replacement is gone, not carried over from the old
    # entry: this is a replace, not a merge.
    assert len(endpoints) == 1
    assert endpoints[0].api_key == "k2"
    assert endpoints[0].api_base is None
    assert endpoints[0].extra_headers is None


def test_add_provider_endpoint_with_api_base_and_headers(cfg_path: Path) -> None:
    endpoints = add_provider_endpoint(
        "openrouter",
        label="eu",
        api_key="k1",
        api_base="https://eu.example.com",
        extra_headers={"X-Region": "eu"},
        config_path=cfg_path,
    )

    assert endpoints[0].api_base == "https://eu.example.com"
    assert endpoints[0].extra_headers == {"X-Region": "eu"}


def test_add_provider_endpoint_unknown_provider_raises(cfg_path: Path) -> None:
    with pytest.raises(KeyError):
        add_provider_endpoint("nonexistent_provider", label="x", api_key="k", config_path=cfg_path)


def test_add_provider_endpoint_refuses_an_empty_key_for_a_key_based_provider(cfg_path: Path) -> None:
    """The write-time half of the rule: an endpoint with no key is a request
    that will 401, so a key-based provider refuses to persist one -- the same
    check the picker and the CLI both need, decided once in the ops layer.
    """
    with pytest.raises(RuntimeError, match="api_key"):
        add_provider_endpoint("openrouter", label="a", api_base="https://a.example/v1", config_path=cfg_path)


def test_add_provider_endpoint_allows_an_empty_key_for_a_local_deployment(cfg_path: Path) -> None:
    """Derived from the registry's credential shape (``credential_kind``), not
    a hardcoded vendor list: a local deployment has no key to give."""
    endpoints = add_provider_endpoint(
        "hosted_vllm", label="a", api_base="http://10.0.0.5:8000/v1", config_path=cfg_path
    )

    assert endpoints[0].api_key == ""
    assert endpoints[0].api_base == "http://10.0.0.5:8000/v1"


@pytest.mark.parametrize("provider", ["azure_openai", "github_copilot"])
def test_add_provider_endpoint_rejects_providers_that_cannot_rotate(provider: str, cfg_path: Path) -> None:
    """Azure connects through a dedicated client, github_copilot through OAuth --
    neither takes an ``endpoints`` list. ``make_provider`` already refused this
    at build time; the write path must refuse it before ever touching disk,
    not accept a section that starts up broken."""
    with pytest.raises(RuntimeError, match="does not support multiple endpoints"):
        add_provider_endpoint(provider, label="x", api_key="k", config_path=cfg_path)

    assert not cfg_path.exists()


def test_add_provider_endpoint_still_accepts_a_plain_api_key_provider(cfg_path: Path) -> None:
    """openrouter is a plain API-key vendor reached through litellm -- the one
    shape ``endpoints`` is meaningful for -- and must be unaffected by the
    guard above."""
    endpoints = add_provider_endpoint("openrouter", label="primary", api_key="k1", config_path=cfg_path)
    assert [e.label for e in endpoints] == ["primary"]


def test_remove_provider_endpoint(cfg_path: Path) -> None:
    add_provider_endpoint("openrouter", label="primary", api_key="k1", config_path=cfg_path)
    add_provider_endpoint("openrouter", label="backup", api_key="k2", config_path=cfg_path)

    endpoints = remove_provider_endpoint("openrouter", "primary", config_path=cfg_path)

    assert [e.label for e in endpoints] == ["backup"]
    section = _read(cfg_path)["providers"]["openrouter"]
    assert [e["label"] for e in section["endpoints"]] == ["backup"]


def test_remove_absent_endpoint_is_noop(cfg_path: Path) -> None:
    add_provider_endpoint("openrouter", label="primary", api_key="k1", config_path=cfg_path)

    endpoints = remove_provider_endpoint("openrouter", "not-there", config_path=cfg_path)

    assert [e.label for e in endpoints] == ["primary"]


def test_remove_provider_endpoint_unknown_provider_raises(cfg_path: Path) -> None:
    with pytest.raises(KeyError):
        remove_provider_endpoint("nonexistent_provider", "x", config_path=cfg_path)


def test_list_provider_endpoints_redacts_api_key(cfg_path: Path) -> None:
    add_provider_endpoint(
        "openrouter",
        label="primary",
        api_key="k1",
        api_base="https://example.com",
        config_path=cfg_path,
    )

    out = list_provider_endpoints("openrouter", config_path=cfg_path)

    assert out == [
        {"label": "primary", "api_key": "****set****", "api_base": "https://example.com", "extra_headers": None}
    ]


def test_list_provider_endpoints_reports_empty_key(cfg_path: Path) -> None:
    add_provider_endpoint("openrouter", label="primary", api_key="", config_path=cfg_path)

    out = list_provider_endpoints("openrouter", config_path=cfg_path)

    assert out[0]["api_key"] == "(empty)"


def test_list_provider_endpoints_default_when_none_configured(cfg_path: Path) -> None:
    assert list_provider_endpoints("openrouter", config_path=cfg_path) == []


def test_list_provider_endpoints_unknown_provider_raises(cfg_path: Path) -> None:
    with pytest.raises(KeyError):
        list_provider_endpoints("nonexistent_provider", config_path=cfg_path)


def test_malformed_config_refuses_write_and_preserves_file(cfg_path: Path) -> None:
    # REGRESSION: a present-but-unparseable config must NOT be clobbered.
    from raven.config.loader import ConfigReadError

    original = '{\n  "providers": {"openai": {"apiKey": "sk-o"}},\n  // comment => invalid JSON\n}\n'
    cfg_path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigReadError):
        set_provider_fields("openai", {"api_key": "sk-x"}, config_path=cfg_path)
    assert cfg_path.read_text(encoding="utf-8") == original  # untouched


def _codex_credential(payload: str = '{"access_token": "live"}') -> None:
    """The probe checks the file before it asks the account: the two failures are
    not fixed by the same thing, so they must not read the same."""
    from raven.config.paths import get_oauth_dir

    codex_dir = get_oauth_dir() / "chatgpt"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "auth.json").write_text(payload, encoding="utf-8")


def test_probing_codex_asks_the_endpoint_that_backend_serves(
    cfg_path: Path,
    oauth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generic probe requests ``{api_base}/v1/models``, which this backend does
    not serve -- a valid OAuth credential came back refused, reading as a bad key.
    Its catalogue is both the credential check and the list of usable models."""
    _codex_credential()
    monkeypatch.setattr(
        "raven.providers.codex_catalog.account_models",
        lambda timeout=5.0, strict=False: ("gpt-5.6-sol", "gpt-5.4"),
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


def test_probing_codex_reports_an_unreachable_catalogue_as_a_network_problem(
    cfg_path: Path,
    oauth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forgiving reader answers an empty list for every failure, which reads as
    "this account has nothing". Only one of those two is fixed by signing in, and
    the recovery menus branch on this status to decide what to offer."""
    _codex_credential()

    def unreachable(timeout=5.0, strict=False):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("raven.providers.codex_catalog.account_models", unreachable)

    result = probe_provider("openai_codex", config_path=cfg_path)

    assert result["status"] == "network_error", result
    assert "provider login" not in result["error"], "sent to sign in over a network fault"


def test_probing_codex_keeps_a_revoked_credential_on_the_path_that_can_fix_it(
    cfg_path: Path,
    oauth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential the account no longer honours reaches the catalogue and comes
    back empty. Reporting that as a network fault put it in the recovery branch
    that offers only Retry -- signing in again, the one thing that fixes it, is
    offered by the other branch."""
    _codex_credential('{"refresh_token": "revoked"}')
    monkeypatch.setattr(
        "raven.providers.codex_catalog.account_models",
        lambda timeout=5.0, strict=False: (),
    )

    result = probe_provider("openai_codex", config_path=cfg_path)

    assert result["status"] == "oauth_token_missing", result
    assert "provider login openai-codex" in result["error"]


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


def _seed_copilot_credential(monkeypatch: pytest.MonkeyPatch, *, api_base: str | None) -> None:
    """A signed-in seat: a device token on disk, plus what the driver reads from
    the API key it exchanges it for."""
    from raven.config.update_providers import _copilot_token_dir

    token_dir = _copilot_token_dir()
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "access-token").write_text("gho_device_token", encoding="utf-8")
    monkeypatch.setattr(
        "litellm.llms.github_copilot.authenticator.Authenticator.get_api_key",
        lambda self: "tid=copilot-api-key",
    )
    monkeypatch.setattr(
        "litellm.llms.github_copilot.authenticator.Authenticator.get_api_base",
        lambda self: api_base,
    )


def test_probing_copilot_asks_the_endpoint_the_credential_names(
    cfg_path: Path,
    oauth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed-in seat has to be able to come back ok.

    The endpoint is not in the registry -- it arrives with the credential -- and
    the backend refuses a request carrying only an ``Authorization`` header, so
    the generic probe could reach neither: it answered "api_base is empty" before
    asking, and asking its way would have read a working seat as a bad key.
    """
    _seed_copilot_credential(monkeypatch, api_base="https://api.githubcopilot.com")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)

        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}, {"id": "claude-sonnet-4.5"}]})

    result = probe_provider("github_copilot", config_path=cfg_path, transport=_mock_transport(handler))

    assert result["ok"] is True, result
    assert result["models_count"] == 2
    assert result["model_ids"] == ["gpt-4o", "claude-sonnet-4.5"]
    assert str(seen[0].url) == "https://api.githubcopilot.com/models", "asked the generic /v1/models shape"
    assert seen[0].headers["authorization"] == "Bearer tid=copilot-api-key"
    # The headers that tell the backend an editor is asking. Without them a valid
    # seat is refused, which the probe would report as a bad credential.
    assert seen[0].headers["copilot-integration-id"] == "vscode-chat"
    assert seen[0].headers["editor-version"].startswith("vscode/")


def test_a_copilot_credential_without_an_endpoint_is_a_credential_problem(
    cfg_path: Path,
    oauth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint lives in the API key file, so its absence is that file being
    unusable -- not a provider missing configuration the user could supply."""
    _seed_copilot_credential(monkeypatch, api_base=None)

    def never(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("a request went out with no endpoint")

    result = probe_provider("github_copilot", config_path=cfg_path, transport=_mock_transport(never))

    assert result["status"] == "oauth_token_missing"
    assert "provider login github-copilot" in result["error"]


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


def test_probing_codex_offers_a_sign_in_when_the_credential_is_the_problem(
    cfg_path: Path,
    oauth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token that cannot be produced any more is fixed by signing in again, and
    the recovery menu offers that on this status. Reported as a network fault it
    landed on the branch that offers only Retry -- which cannot fix it."""
    _codex_credential('{"refresh_token": "revoked"}')

    def cannot_produce_a_token(timeout=5.0, strict=False):
        raise RuntimeError("could not renew the ChatGPT credential")

    monkeypatch.setattr("raven.providers.codex_catalog.account_models", cannot_produce_a_token)

    result = probe_provider("openai_codex", config_path=cfg_path)

    assert result["status"] == "oauth_token_missing", result
    assert "could not renew" in result["error"]


def test_a_model_stored_one_way_is_removed_by_the_other(cfg_path: Path) -> None:
    """Deletion matches the model, not the spelling.

    The two write paths disagreed for most providers, so a list could hold one
    model as both `glm-4.6` and `zai/glm-4.6`. Removing either string left the
    other behind, and the call reported success.
    """
    from raven.config.update_providers import add_provider_model, remove_provider_model

    add_provider_model("zai", "glm-4.6", config_path=cfg_path)
    remaining = remove_provider_model("zai", "zai/glm-4.6", config_path=cfg_path)
    assert remaining == []


def test_the_same_model_in_two_spellings_is_added_once(cfg_path: Path) -> None:
    from raven.config.update_providers import add_provider_model

    add_provider_model("zai", "zai/glm-4.6", config_path=cfg_path)
    assert add_provider_model("zai", "glm-4.6", config_path=cfg_path) == ["zai/glm-4.6"]


def test_the_cli_writes_a_model_id_the_way_every_other_path_does(cfg_path: Path) -> None:
    """`provider set --models` is the third write path and skipped the contract.

    It stored a bare id while the picker and the wizard stored a qualified one.
    Identity still matched so nothing visibly broke -- which is how the two
    spellings coexisted the last time, right up until a delete matched neither.
    """
    from raven.providers.wire import stored_model_id

    set_provider_fields("anthropic", {"models": "claude-opus-4-8,anthropic/claude-sonnet-5"}, config_path=cfg_path)

    stored = _read(cfg_path)["providers"]["anthropic"]["models"]
    assert stored == ["anthropic/claude-opus-4-8", "anthropic/claude-sonnet-5"]
    assert stored[0] == stored_model_id("anthropic", "claude-opus-4-8")


# ---------------------------------------------------------------------------
# Providers that ship no default address: the probe used to call them unconfigured
# ---------------------------------------------------------------------------


def test_a_vendor_litellm_knows_the_address_of_is_actually_probed(cfg_path: Path) -> None:
    """Ten providers carry no ``default_api_base``, and this returned
    ``not_configured`` for every one of them -- telling a correctly configured
    install to set the key it had already set. LiteLLM knows where four of them
    live, because it is the thing that sends their requests.
    """
    _seed_key(cfg_path, "groq", "sk-groq")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(401, json={"error": "bad key"})

    result = probe_provider("groq", config_path=cfg_path, transport=_mock_transport(handler))

    assert seen, "the probe never left the building"
    assert "groq.com" in seen[0], seen
    assert result["status"] == "invalid_key", "a bad key is now distinguishable from an unconfigured one"


def test_a_vendor_with_no_catalogue_endpoint_is_reported_as_unprobed_not_unconfigured(cfg_path: Path) -> None:
    """Anthropic, OpenAI and Gemini compile the address into their SDKs, so
    there is no ``/models`` to ping and nothing the user could supply. The key is
    there; this probe simply cannot reach the vendor. Saying so is the honest
    answer, and it is not a failure."""
    _seed_key(cfg_path, "anthropic", "sk-ant")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        raise AssertionError(f"nothing should have been sent: {request.url}")

    result = probe_provider("anthropic", config_path=cfg_path, transport=_mock_transport(handler))

    assert result["status"] == "no_probe_endpoint"
    assert result["ok"] is False
    assert "credential present" in result["error"]


def test_a_provider_that_genuinely_needs_an_address_still_says_so(cfg_path: Path) -> None:
    """A self-hosted deployment and an Azure resource are addresses only the user
    knows, so ``not_configured`` is the truth for those two -- the change must not
    turn a real gap into a shrug."""
    for name in ("hosted_vllm", "azure_openai"):
        _seed_key(cfg_path, name, "sk-x")
        result = probe_provider(name, config_path=cfg_path, transport=_mock_transport(lambda r: httpx.Response(200)))
        assert result["status"] == "not_configured", name


def test_a_404_from_an_address_we_guessed_is_not_reported_as_a_broken_key(cfg_path: Path) -> None:
    """DeepSeek's completions endpoint is ``/beta``, which has no ``/models``.

    A 404 never says anything about a credential, so surfacing it as a failure
    would be the original lie in a new spelling. A 404 from an address the *user*
    supplied is different -- that is a typo they need to see -- so this only
    applies where the address was derived.
    """
    _seed_key(cfg_path, "deepseek", "sk-deepseek")

    derived = probe_provider(
        "deepseek",
        config_path=cfg_path,
        transport=_mock_transport(lambda r: httpx.Response(404, json={"error": "not found"})),
    )
    assert derived["status"] == "no_probe_endpoint"

    set_provider_fields("deepseek", {"api_base": "https://typo.example.com/v1"}, config_path=cfg_path)
    typed = probe_provider(
        "deepseek",
        config_path=cfg_path,
        transport=_mock_transport(lambda r: httpx.Response(404, json={"error": "not found"})),
    )
    assert typed["status"] == "http_404", "a user's own wrong address must still surface"


def test_probing_a_login_prompting_provider_never_asks_litellm_to_resolve_it(cfg_path: Path) -> None:
    """Resolving a Copilot id resolves its credentials on the way.

    With no token file that prints a device code to stdout and blocks; deriving
    the address before the branch that handles Copilot separately hung this one
    probe on that login. Recorded rather than raised, because the
    derivation swallows exceptions to fall through -- a probe that raises is
    caught and proves nothing.
    """
    import litellm

    asked: list[str] = []

    def _record(*args, **kwargs):
        asked.append(str(kwargs.get("model") or (args[0] if args else "?")))
        raise Exception("unmapped")

    original = litellm.get_llm_provider
    litellm.get_llm_provider = _record
    try:
        probe_provider("github_copilot", config_path=cfg_path, transport=_mock_transport(lambda r: httpx.Response(200)))
    finally:
        litellm.get_llm_provider = original

    assert not asked, f"a login-prompting id was handed to LiteLLM: {asked}"


@pytest.mark.parametrize("spec", [s for s in _PROVIDERS if s.is_oauth], ids=lambda s: s.name)
def test_an_oauth_provider_is_never_resolved_through_litellm_for_its_address(spec, cfg_path: Path) -> None:
    """Resolving one of these resolves its credentials on the way, which prints a
    device code and blocks.

    Asserted per OAuth provider rather than for the one that broke: the guard
    used to be asked about the *wire* form of the id, and `wire_model` strips the
    provider name outright for the codex and azure shapes -- so it was handed a
    bare "probe-model" and saw nothing to object to. Today that is masked by
    those providers having an address already; it would come back the moment one
    did not.
    """
    import litellm

    from raven.config.update_providers import _litellm_api_base

    asked: list[str] = []

    def _record(*args, **kwargs):
        asked.append(str(kwargs.get("model") or (args[0] if args else "?")))
        raise Exception("unmapped")

    original = litellm.get_llm_provider
    litellm.get_llm_provider = _record
    try:
        assert _litellm_api_base(spec) == ""
    finally:
        litellm.get_llm_provider = original

    assert not asked, f"{spec.name}: handed to LiteLLM anyway ({asked})"
