"""Tests for the ``model.*`` RPC handlers (TUI ``/model`` v1 backend).

The five handlers wrap ``raven.config.update_providers`` write/read helpers
plus the provider registry. Config is sandboxed by redirecting ``Path.home()``
to a tmp dir (same mechanism as ``test_tui_rpc_config`` / ``test_tui_rpc_setup``)
so the real user config is never touched. No network is hit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.providers.common_models import common_models_for
from raven.tui_rpc.errors import ConfigValidationError, NotSupportedInV01Error
from raven.tui_rpc.methods import model as model_module
from raven.tui_rpc.methods.model import (
    model_add_model,
    model_disconnect,
    model_options,
    model_remove_model,
    model_save_key,
)


@pytest.fixture
def fake_home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Clear any process-wide config-path override a prior test left set, so
    # get_config_path() falls back to the patched Path.home (monkeypatch restores it).
    monkeypatch.setattr("raven.config.loader._current_config_path", None)
    # OAuth credentials live under ``~/.raven`` too, so the patched home covers
    # them -- but the two environment overrides jump out of it, and left set they
    # decide whether a provider reports itself authenticated.
    monkeypatch.delenv("GITHUB_COPILOT_TOKEN_DIR", raising=False)
    monkeypatch.delenv("OAUTH_CLI_KIT_TOKEN_PATH", raising=False)
    return tmp_path


def _write_config(home: Path, payload: dict) -> None:
    cfg_dir = home / ".raven"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _entry(result: dict, slug: str) -> dict:
    for entry in result["providers"]:
        if entry["slug"] == slug:
            return entry
    raise AssertionError(f"provider {slug!r} not in options result")


# ----------------------------------------------------------------------------
# model.options
# ----------------------------------------------------------------------------


async def test_options_authed_provider_lists_models(fake_home: Path) -> None:
    _write_config(
        fake_home,
        {
            "agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}},
            "providers": {
                "anthropic": {
                    "apiKey": "sk-ant-xxx",
                    "models": ["claude-opus-4-8", "claude-sonnet-4-5"],
                }
            },
        },
    )
    result = await model_options({})
    entry = _entry(result, "anthropic")
    assert entry["authenticated"] is True
    # Configured models rank first, then the curated shortlist, then LiteLLM's
    # catalogue (deduped). The order is the contract: recommendations stay at the
    # top of a list the catalogue makes long.
    assert entry["models"][:2] == ["claude-opus-4-8", "claude-sonnet-4-5"]
    curated = common_models_for("anthropic")
    assert entry["models"][2 : 2 + len(curated)] == curated
    assert entry["total_models"] > 2 + len(curated), "the catalogue tier added nothing"
    assert entry["auth_type"] == "key"
    assert entry["key_env"] == "ANTHROPIC_API_KEY"


async def test_options_unauthed_provider_marked(fake_home: Path) -> None:
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    result = await model_options({})
    entry = _entry(result, "openai")
    assert entry["authenticated"] is False
    # Curated shortlist is shown regardless of auth (as openrouter always has),
    # so the picker is never empty; the unauthed state is conveyed separately.
    curated = common_models_for("openai")
    assert entry["models"][: len(curated)] == curated
    assert entry["total_models"] > len(curated), "the catalogue tier added nothing"


async def test_options_current_provider_marked(fake_home: Path) -> None:
    _write_config(
        fake_home,
        {
            "agents": {
                "defaults": {
                    "model": "anthropic/claude-sonnet-4-5",
                    "provider": "anthropic",
                }
            }
        },
    )
    result = await model_options({})
    assert result["model"] == "anthropic/claude-sonnet-4-5"
    assert result["provider"] == "anthropic"
    assert _entry(result, "anthropic")["is_current"] is True
    assert _entry(result, "openai")["is_current"] is False


async def test_options_current_provider_derived_from_model(fake_home: Path) -> None:
    _write_config(
        fake_home,
        {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}},
    )
    result = await model_options({})
    assert result["provider"] == "anthropic"
    assert _entry(result, "anthropic")["is_current"] is True


async def test_options_oauth_provider_warning_and_auth_type(fake_home: Path) -> None:
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    result = await model_options({})
    entry = _entry(result, "openai_codex")
    assert entry["auth_type"] == "oauth"
    assert entry["authenticated"] is False
    assert entry["warning"]
    assert "provider login" in entry["warning"]


async def test_options_needs_api_base_flag(fake_home: Path) -> None:
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    result = await model_options({})
    assert _entry(result, "custom")["needs_api_base"] is True
    assert _entry(result, "azure_openai")["needs_api_base"] is True
    assert _entry(result, "anthropic")["needs_api_base"] is False


# ----------------------------------------------------------------------------
# model.save_key
# ----------------------------------------------------------------------------


async def test_save_key_happy_path_writes_key(fake_home: Path) -> None:
    result = await model_save_key({"slug": "anthropic", "api_key": "sk-ant-new"})
    entry = result["provider"]
    assert entry["slug"] == "anthropic"
    assert entry["authenticated"] is True

    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["providers"]["anthropic"]["apiKey"] == "sk-ant-new"


async def test_save_key_custom_accepts_api_base(fake_home: Path) -> None:
    result = await model_save_key(
        {
            "slug": "custom",
            "api_key": "key123",
            "api_base": "https://example.test/v1",
        }
    )
    assert result["provider"]["slug"] == "custom"
    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["providers"]["custom"]["apiBase"] == "https://example.test/v1"


async def test_save_key_oauth_rejected(fake_home: Path) -> None:
    with pytest.raises(NotSupportedInV01Error):
        await model_save_key({"slug": "openai_codex", "api_key": "x"})


async def test_save_key_missing_params_rejected(fake_home: Path) -> None:
    with pytest.raises(ConfigValidationError):
        await model_save_key({"slug": "anthropic"})


# ----------------------------------------------------------------------------
# model.disconnect
# ----------------------------------------------------------------------------


async def test_disconnect_clears_creds(fake_home: Path) -> None:
    await model_save_key({"slug": "anthropic", "api_key": "sk-ant-xxx"})
    result = await model_disconnect({"slug": "anthropic"})
    assert result == {"disconnected": True}

    options = await model_options({})
    assert _entry(options, "anthropic")["authenticated"] is False


# ----------------------------------------------------------------------------
# model.add_model / model.remove_model
# ----------------------------------------------------------------------------


async def test_add_model_reflected_in_options(fake_home: Path) -> None:
    await model_save_key({"slug": "anthropic", "api_key": "sk-ant-xxx"})
    result = await model_add_model({"slug": "anthropic", "model": "claude-opus-4-8"})
    assert "claude-opus-4-8" in result["provider"]["models"]

    options = await model_options({})
    assert "claude-opus-4-8" in _entry(options, "anthropic")["models"]


async def test_remove_model_reflected_in_options(fake_home: Path) -> None:
    await model_save_key({"slug": "anthropic", "api_key": "sk-ant-xxx"})
    await model_add_model({"slug": "anthropic", "model": "claude-opus-4-8"})
    result = await model_remove_model({"slug": "anthropic", "model": "claude-opus-4-8"})
    assert "claude-opus-4-8" not in result["provider"]["models"]

    options = await model_options({})
    assert "claude-opus-4-8" not in _entry(options, "anthropic")["models"]


async def test_add_model_unknown_provider_rejected(fake_home: Path) -> None:
    with pytest.raises(ConfigValidationError):
        await model_add_model({"slug": "no_such_provider", "model": "x"})


# ----------------------------------------------------------------------------
# Dispatcher wiring
# ----------------------------------------------------------------------------


async def test_model_methods_registered_via_helper(fake_home: Path) -> None:
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.methods.model import register_model_methods

    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    d = Dispatcher()
    register_model_methods(d)
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "model.options", "params": {}})
    assert "error" not in resp
    assert resp["result"]["model"] == "anthropic/claude-sonnet-4-5"

    resp = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "model.save_key",
            "params": {"slug": "openai_codex", "api_key": "x"},
        }
    )
    assert resp["error"]["code"] == -32012


# ----------------------------------------------------------------------------
# Regressions (code review)
# ----------------------------------------------------------------------------


async def test_options_accepts_session_id(fake_home: Path) -> None:
    # The picker calls model.options with {session_id: "tui:default"}; the param
    # model must accept it (strict models reject unknown keys otherwise).
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    result = await model_options({"session_id": "tui:default"})
    assert "providers" in result


async def test_save_key_custom_without_api_base_rejected(fake_home: Path) -> None:
    with pytest.raises(ConfigValidationError):
        await model_save_key({"slug": "custom", "api_key": "x"})


# ----------------------------------------------------------------------------
# common-model shortlist (curated defaults shown in the picker)
# ----------------------------------------------------------------------------


async def test_options_openrouter_seeds_common_models(fake_home: Path) -> None:
    # A provider with a key but no explicitly configured models still lists the
    # curated "common" shortlist, so the picker is never empty.
    _write_config(
        fake_home,
        {
            "agents": {"defaults": {"model": "openrouter/anthropic/claude-opus-4.8"}},
            "providers": {"openrouter": {"apiKey": "sk-or-xxx", "models": []}},
        },
    )
    entry = _entry(await model_options({}), "openrouter")
    curated = common_models_for("openrouter")
    assert entry["models"][: len(curated)] == curated
    assert entry["total_models"] > len(curated), "the catalogue tier added nothing"


async def test_options_config_models_rank_before_common_and_dedup(fake_home: Path) -> None:
    # Configured models come first; the common shortlist follows with duplicates
    # removed (a configured id already in the shortlist is not listed twice).
    dup = common_models_for("openrouter")[0]
    _write_config(
        fake_home,
        {
            "agents": {"defaults": {"model": dup}},
            "providers": {"openrouter": {"apiKey": "sk-or-xxx", "models": ["my/custom-model", dup]}},
        },
    )
    models = _entry(await model_options({}), "openrouter")["models"]
    assert models[:2] == ["my/custom-model", dup]
    assert models.count(dup) == 1
    assert set(common_models_for("openrouter")).issubset(set(models))


# Direct providers seeded for issue #100 (keyed provider, empty config models,
# used to show an empty picker). ``prefix`` is the litellm-routing form each id
# must carry so a picked id drops straight into ``agents.defaults.model``.
_SEEDED_DIRECT_PROVIDERS = [
    ("deepseek", "deepseek/"),
    ("openai", "openai/"),
    ("anthropic", "anthropic/"),
    ("gemini", "gemini/"),
    ("zai", "zai/"),
    ("groq", "groq/"),
    ("dashscope", "dashscope/"),
]


@pytest.mark.parametrize("slug, prefix", _SEEDED_DIRECT_PROVIDERS)
def test_common_models_seeded_for_direct_providers(slug: str, prefix: str) -> None:
    models = common_models_for(slug)
    assert models, f"{slug} common-model shortlist is empty"
    assert all(m.startswith(prefix) for m in models), models
    assert len(models) == len(set(models)), "duplicate ids in shortlist"


@pytest.mark.parametrize("slug, prefix", _SEEDED_DIRECT_PROVIDERS)
async def test_options_direct_provider_lists_common_models_when_unconfigured(
    fake_home: Path, slug: str, prefix: str
) -> None:
    _write_config(
        fake_home,
        {
            "agents": {"defaults": {"model": "openrouter/anthropic/claude-opus-4.8"}},
            "providers": {slug: {"apiKey": "sk-test-xxxxxxx", "models": []}},
        },
    )
    entry = _entry(await model_options({}), slug)
    assert entry["total_models"] > 0
    curated = common_models_for(slug)
    assert entry["models"][: len(curated)] == curated, "the curated shortlist must stay at the top"
    # And the tail is this provider's catalogue, not some other provider's:
    # asserting only the prefix let the third tier be wired to a fixed slug.
    from raven.providers.common_models import litellm_models_for

    tail = entry["models"][len(curated) :]
    assert set(tail) <= set(litellm_models_for(slug)), f"{slug}: tail holds models from elsewhere"


async def test_save_key_accepts_a_provider_without_a_spec(fake_home: Path) -> None:
    """The picker lists such a provider, so the key dialog has to serve it.

    Four of the five model.* handlers already accepted one; refusing here left a
    section the picker showed but nothing could finish configuring.
    """
    _write_config(fake_home, {"agents": {"defaults": {"model": "openai/gpt-4o"}}})

    result = await model_save_key({"slug": "mistral", "api_key": "K-MISTRAL"})

    assert result["provider"]["slug"] == "mistral"
    assert result["provider"]["authenticated"] is True


@pytest.mark.parametrize("slug", ["moonshot", "minimax", "volcengine", "ollama_chat", "github_copilot"])
def test_litellm_catalogue_fills_providers_with_no_curated_shortlist(slug: str) -> None:
    """Eleven providers had no shortlist, so the picker offered them nothing.

    Ollama is the case that proves the lookup has to go through every name the
    provider answers to: LiteLLM files its models under "ollama" while the
    section is "ollama_chat", so a lookup by section name alone finds none.
    """
    from raven.providers.common_models import common_models_for, litellm_models_for

    assert not common_models_for(slug), f"{slug} now has a shortlist; pick another provider for this test"
    models = litellm_models_for(slug)
    assert models, f"{slug}: the catalogue tier found nothing"
    assert all("/" in m for m in models), models[:3]


def test_catalogue_ids_are_spelled_the_way_they_route() -> None:
    """The catalogue is inconsistent about prefixes; the picker must not be.

    Moonshot's entries carry their prefix and VolcEngine's do not. An id offered
    bare would be routed by keyword instead of to the provider the user picked.
    """
    from raven.providers.common_models import litellm_models_for
    from raven.providers.registry import find_by_name

    for slug in ("moonshot", "volcengine", "ollama_chat"):
        spec = find_by_name(slug)
        assert spec is not None
        models = litellm_models_for(slug)
        assert models, f"{slug}: nothing to check"
        for model in models:
            # Exactly one prefix, not merely one at the front: `startswith` alone
            # reads "moonshot/moonshot/x" as correct, so it could not tell a
            # re-prefixed id from a right one.
            head, _, rest = model.partition("/")
            assert head == spec.model_prefix, f"{slug}: {model}"
            assert rest, f"{slug}: {model} has no id after the prefix"
            assert not rest.startswith(f"{spec.model_prefix}/"), f"{slug}: double-prefixed {model}"


def test_catalogue_offers_only_chat_models() -> None:
    """Embeddings and speech share the catalogue and fail as a chat default."""
    from raven.providers.common_models import litellm_models_for

    offered = {m for slug in ("minimax", "volcengine") for m in litellm_models_for(slug)}
    assert offered, "nothing to check"
    assert not [m for m in offered if "embedding" in m or "speech" in m], sorted(offered)


def test_the_catalogue_is_not_read_until_the_picker_is_opened() -> None:
    """Reading it imports LiteLLM, which is two seconds Raven must not spend at
    startup. Importing the module that offers it must stay free."""
    import subprocess
    import sys

    probe = (
        "import sys, json\n"
        "import raven.tui_rpc.methods.model  # noqa: F401\n"
        "before = 'litellm' in sys.modules\n"
        "from raven.providers.common_models import litellm_models_for\n"
        "n = len(litellm_models_for('moonshot'))\n"
        "print(json.dumps({'before': before, 'after': 'litellm' in sys.modules, 'n': n}))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["before"] is False, "importing the picker module pulled in litellm"
    assert result["after"] is True, "reading the catalogue did not import litellm; is it still the source?"
    assert result["n"] > 0


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: model_options({}), id="options"),
        pytest.param(lambda: model_save_key({"slug": "deepseek", "api_key": "sk-test-key"}), id="save_key"),
        pytest.param(lambda: model_add_model({"slug": "deepseek", "model": "deepseek-chat"}), id="add_model"),
        pytest.param(lambda: model_remove_model({"slug": "deepseek", "model": "deepseek-chat"}), id="remove_model"),
        pytest.param(lambda: model_options({}), id="options_again"),
    ],
)
async def test_no_handler_reads_the_catalogue_on_the_event_loop(fake_home: Path, monkeypatch, call) -> None:
    """Reading it imports LiteLLM the first time, which takes seconds.

    Only ``model.options`` warmed the cache off the loop; the three write
    handlers built their response row inline. That is the stall the warm-up
    exists to prevent, and it is reachable whenever the cache is cold -- a failed
    read is deliberately not cached, and the client decides the call order.
    """
    import asyncio
    import threading

    _write_config(fake_home, {"providers": {"deepseek": {"api_key": "sk-existing"}}})

    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = model_module.litellm_models_for

    def _spy(slug: str):
        seen.append(threading.get_ident())
        return real(slug)

    monkeypatch.setattr(model_module, "litellm_models_for", _spy)
    await call()

    assert seen, "the handler never consulted the catalogue, so this proves nothing"
    on_loop = [t for t in seen if t == loop_thread]
    assert not on_loop, f"{len(on_loop)}/{len(seen)} catalogue reads ran on the event loop"
    assert asyncio.get_running_loop() is not None


async def test_save_key_configures_a_local_deployment_by_address(fake_home: Path) -> None:
    """It is reached by address and has no key; the handler demanded one.

    `api_key` was a required field, and the picker reported every non-OAuth
    provider as taking one, so a local deployment could not be configured from the
    TUI at all -- and an empty key written into its section would have made it look
    configured while nothing had been set.
    """
    # Seeded with a key it should never have had, so "no key" is asserted as a
    # write rather than as an omission: leaving the field alone kept the old value.
    _write_config(fake_home, {"providers": {"ollama_chat": {"api_key": "sk-leftover"}}})

    result = await model_save_key({"slug": "ollama_chat", "api_base": "http://gpu-box:11434"})

    assert result["provider"]["slug"] == "ollama_chat"
    section = json.loads((fake_home / ".raven" / "config.json").read_text())["providers"]["ollama_chat"]
    assert section.get("apiBase") == "http://gpu-box:11434"
    assert section.get("apiKey") == "", f"a stale key survived: {section}"


async def test_save_key_refuses_a_key_for_a_local_deployment(fake_home: Path) -> None:
    """Said out loud rather than dropped, so it does not look accepted."""
    with pytest.raises(ConfigValidationError) as excinfo:
        await model_save_key({"slug": "ollama_chat", "api_key": "sk-nope", "api_base": "http://x:11434"})
    assert "api_key" in str(excinfo.value)


async def test_save_key_still_requires_a_key_for_a_keyed_provider(fake_home: Path) -> None:
    """Relaxing the field for local deployments must not relax it for the rest."""
    with pytest.raises(ConfigValidationError) as excinfo:
        await model_save_key({"slug": "deepseek", "api_key": ""})
    assert "api_key" in str(excinfo.value)


async def test_save_key_requires_an_address_for_a_local_deployment(fake_home: Path) -> None:
    """Neither field given is not a configured provider."""
    with pytest.raises(ConfigValidationError) as excinfo:
        await model_save_key({"slug": "ollama_chat"})
    assert "api_base" in str(excinfo.value)


async def test_options_lists_the_codex_models_the_account_reports(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the account knows: the registry default is refused by the backend and
    LiteLLM's table carries slugs an account is not entitled to, which is why this
    row used to offer nothing at all."""
    monkeypatch.setattr(
        "raven.providers.codex_catalog.account_models",
        lambda: ("gpt-5.6-sol", "gpt-5.4"),
    )
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})

    entry = _entry(await model_options({}), "openai_codex")

    assert entry["models"] == ["openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.4"]


def test_only_codex_is_asked_of_the_account_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other provider is served by the static tiers, so none of them should
    pay for a network round trip."""
    from raven.tui_rpc.methods.model import _provider_models

    monkeypatch.setattr(
        "raven.providers.codex_catalog.account_models",
        lambda: pytest.fail("the catalogue was asked for a provider it cannot answer for"),
    )

    assert _provider_models("deepseek"), "other providers still list models"
