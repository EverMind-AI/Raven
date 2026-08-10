"""Tests for the ``model.*`` RPC handlers (TUI ``/model`` v1 backend).

The eight handlers wrap ``raven.config.update_providers`` write/read helpers
plus the provider registry. Config is sandboxed by redirecting ``Path.home()``
to a tmp dir (same mechanism as ``test_tui_rpc_config`` / ``test_tui_rpc_setup``)
so the real user config is never touched. No network is hit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.providers.common_models import common_models_for
from raven.providers.registry import PROVIDERS
from raven.tui_rpc.errors import ConfigValidationError, NotSupportedInV01Error
from raven.tui_rpc.methods import model as model_module
from raven.tui_rpc.methods.model import (
    model_add_endpoint,
    model_add_model,
    model_disconnect,
    model_endpoints,
    model_options,
    model_remove_endpoint,
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
    # them -- but each family prefers an environment override when one is set, and
    # the suite-wide fixture sets all of them.
    for name in ("CHATGPT_TOKEN_DIR", "GITHUB_COPILOT_TOKEN_DIR", "MINIMAX_OAUTH_TOKEN_DIR"):
        monkeypatch.delenv(name, raising=False)
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

    # The configured entries are written in the pre-contract bare spelling, and
    # the curated list carries the qualified one. They are the same two models,
    # so the shortlist contributes everything except those -- listing a model the
    # user already has, under the other spelling, is the duplicate the picker
    # used to show.
    from raven.providers.wire import merge_key

    configured_keys = {merge_key("anthropic", m) for m in ("claude-opus-4-8", "claude-sonnet-4-5")}
    curated = [m for m in common_models_for("anthropic") if merge_key("anthropic", m) not in configured_keys]
    assert entry["models"][2 : 2 + len(curated)] == curated
    assert len(entry["models"]) == len({merge_key("anthropic", m) for m in entry["models"]}), "a model is listed twice"
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
    # Stored qualified: a bare id is claimed by keyword matching instead.
    assert "anthropic/claude-opus-4-8" in result["provider"]["models"]

    options = await model_options({})
    assert "anthropic/claude-opus-4-8" in _entry(options, "anthropic")["models"]


async def test_a_bare_model_typed_for_codex_is_stored_so_it_finds_codex(fake_home: Path) -> None:
    """Through the handler, not just the helper: the screen takes free text and the
    catalogue offers bare slugs, so this is what a user actually types. Stored bare
    it resolves to OpenAI and the request leaves for a provider that does not serve
    it."""
    from raven.providers.registry import find_by_model

    result = await model_add_model({"slug": "openai_codex", "model": "gpt-5.6-sol"})

    stored = result["provider"]["models"]
    assert "openai-codex/gpt-5.6-sol" in stored, stored
    assert "gpt-5.6-sol" not in stored, "the bare spelling was stored as well"
    resolved = find_by_model("openai-codex/gpt-5.6-sol")
    assert resolved is not None and resolved.name == "openai_codex"


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
# model.endpoints / model.add_endpoint / model.remove_endpoint
# ----------------------------------------------------------------------------


async def test_add_endpoint_answers_with_the_refreshed_list(fake_home: Path) -> None:
    result = await model_add_endpoint(
        {"slug": "deepseek", "label": "eu", "api_key": "sk-eu", "api_base": "https://eu.example.test/v1"}
    )
    assert [ep["label"] for ep in result["endpoints"]] == ["eu"]
    assert result["endpoints"][0]["api_base"] == "https://eu.example.test/v1"

    listed = await model_endpoints({"slug": "deepseek"})
    assert listed == result


async def test_endpoints_never_hand_back_the_key(fake_home: Path) -> None:
    """The picker only ever displays this list, and a key it did not need to see
    is a key a screenshot can leak."""
    await model_add_endpoint({"slug": "deepseek", "label": "eu", "api_key": "sk-eu-secret"})
    await model_add_endpoint({"slug": "deepseek", "label": "keyless"})

    by_label = {ep["label"]: ep["api_key"] for ep in (await model_endpoints({"slug": "deepseek"}))["endpoints"]}

    assert "sk-eu-secret" not in by_label.values()
    assert by_label == {"eu": "****set****", "keyless": "(empty)"}


async def test_add_endpoint_replaces_the_entry_with_the_same_label(fake_home: Path) -> None:
    """``label`` is the idempotency key, so re-adding it is how a rotated key is
    written -- appending a second entry would leave the dead key in rotation."""
    await model_add_endpoint({"slug": "deepseek", "label": "eu", "api_key": "sk-old"})
    result = await model_add_endpoint(
        {"slug": "deepseek", "label": "eu", "api_key": "sk-new", "api_base": "https://eu.example.test/v1"}
    )

    assert [ep["label"] for ep in result["endpoints"]] == ["eu"]
    assert result["endpoints"][0]["api_base"] == "https://eu.example.test/v1"
    section = json.loads((fake_home / ".raven" / "config.json").read_text())["providers"]["deepseek"]
    assert [ep["apiKey"] for ep in section["endpoints"]] == ["sk-new"]


async def test_remove_endpoint_reflected_in_the_list(fake_home: Path) -> None:
    await model_add_endpoint({"slug": "deepseek", "label": "eu", "api_key": "sk-eu"})
    await model_add_endpoint({"slug": "deepseek", "label": "us", "api_key": "sk-us"})

    result = await model_remove_endpoint({"slug": "deepseek", "label": "eu"})

    assert [ep["label"] for ep in result["endpoints"]] == ["us"]
    listed = await model_endpoints({"slug": "deepseek"})
    assert [ep["label"] for ep in listed["endpoints"]] == ["us"]


async def test_removing_an_absent_label_is_a_no_op(fake_home: Path) -> None:
    await model_add_endpoint({"slug": "deepseek", "label": "eu", "api_key": "sk-eu"})

    result = await model_remove_endpoint({"slug": "deepseek", "label": "never-existed"})

    assert [ep["label"] for ep in result["endpoints"]] == ["eu"]


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: model_endpoints({"slug": "no_such_provider"}), id="endpoints"),
        pytest.param(lambda: model_add_endpoint({"slug": "no_such_provider", "label": "eu"}), id="add_endpoint"),
        pytest.param(lambda: model_remove_endpoint({"slug": "no_such_provider", "label": "eu"}), id="remove_endpoint"),
    ],
)
async def test_endpoint_handlers_reject_an_unknown_provider(fake_home: Path, call) -> None:
    with pytest.raises(ConfigValidationError):
        await call()


@pytest.mark.parametrize("slug", ["azure_openai", "github_copilot"])
async def test_add_endpoint_rejects_providers_that_cannot_rotate(slug: str, fake_home: Path) -> None:
    """Mirrors ``make_provider``'s build-time rejection: a provider that
    connects through one dedicated client/account, not several, must be
    rejected here too, with a readable message rather than a bare traceback."""
    with pytest.raises(ConfigValidationError, match="does not support multiple endpoints"):
        await model_add_endpoint({"slug": slug, "label": "x", "api_key": "k"})


async def test_endpoint_handlers_accept_session_id(fake_home: Path) -> None:
    # The picker passes its session down like it does for every other model.*
    # call; a strict param model would reject the key otherwise.
    await model_add_endpoint({"slug": "deepseek", "label": "eu", "session_id": "tui:default"})
    await model_endpoints({"slug": "deepseek", "session_id": "tui:default"})
    result = await model_remove_endpoint({"slug": "deepseek", "label": "eu", "session_id": "tui:default"})

    assert result["endpoints"] == []


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

    resp = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "model.endpoints",
            "params": {"slug": "deepseek"},
        }
    )
    assert "error" not in resp
    assert resp["result"] == {"endpoints": []}


# ----------------------------------------------------------------------------
# Regressions (code review)
# ----------------------------------------------------------------------------


async def test_options_accepts_session_id(fake_home: Path) -> None:
    # The picker calls model.options with {session_id: "tui:default"}; the param
    # model must accept it (strict models reject unknown keys otherwise).
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    result = await model_options({"session_id": "tui:default"})
    assert "providers" in result


async def test_save_key_custom_key_only_accepted(fake_home: Path) -> None:
    """The spec ships a default address, so a bare key is a runnable submission --
    the same answer `credential_status` gives; the picker must not refuse what
    `raven provider set custom --api-key` accepts."""
    result = await model_save_key({"slug": "custom", "api_key": "x"})
    assert result["provider"]["authenticated"] is True


async def test_save_key_azure_key_only_still_rejected(fake_home: Path) -> None:
    """No spec default to fall back on: the address stays mandatory."""
    with pytest.raises(ConfigValidationError) as excinfo:
        await model_save_key({"slug": "azure_openai", "api_key": "x"})
    assert excinfo.value.data["field"] == "api_base"


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
    from raven.providers.registry import find_by_model, find_by_name

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
            assert rest, f"{slug}: {model} has no id after the prefix"
            # The outcome, not one spelling of it: a candidate has to resolve
            # back to the provider it was offered for. Asserting the wire prefix
            # instead tied this to how the id happens to be spelled, which is
            # `stored_model_id`'s business and differs from the routing prefix
            # for every underscore-named provider.
            assert find_by_model(model) is spec, f"{slug}: {model} resolves elsewhere"
            assert not rest.startswith(f"{head}/"), f"{slug}: double-prefixed {model}"


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
    assert excinfo.value.data["field"] == "api_key"


async def test_save_key_requires_an_address_for_a_local_deployment(fake_home: Path) -> None:
    """Neither field given is not a configured provider."""
    with pytest.raises(ConfigValidationError) as excinfo:
        await model_save_key({"slug": "ollama_chat"})
    assert excinfo.value.data["field"] == "api_base"


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
    # Asked only of an account there is one for, so the row has to be signed in
    # before the catalogue is reached at all.
    auth = fake_home / ".raven" / "oauth" / "chatgpt"
    auth.mkdir(parents=True, exist_ok=True)
    (auth / "auth.json").write_text('{"access_token": "live"}', encoding="utf-8")
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})

    entry = _entry(await model_options({}), "openai_codex")

    assert entry["authenticated"] is True, "the credential this test wrote was not seen"
    assert entry["models"] == ["openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.4"]


@pytest.mark.parametrize(
    ("slug", "configured"),
    [
        pytest.param("deepseek", True, id="another-provider-the-static-tiers-serve"),
        pytest.param("openai_codex", False, id="codex-with-nobody-signed-in"),
    ],
)
def test_the_account_catalogue_is_asked_only_when_it_can_answer(
    slug: str,
    configured: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network round trip that can only fail is still cached as a failure, which
    is what left codex empty for half a minute after signing in to it."""
    from raven.tui_rpc.methods.model import _provider_models

    monkeypatch.setattr(
        "raven.providers.codex_catalog.account_models",
        lambda: pytest.fail("the catalogue was asked when it had no account to answer for"),
    )

    models = _provider_models(slug, configured=configured)

    if slug == "deepseek":
        assert models, "other providers still list models"


@pytest.mark.parametrize(
    ("slug", "typed", "stored"),
    [
        pytest.param("openai_codex", "gpt-5.6-sol", "openai-codex/gpt-5.6-sol", id="codex-typed-bare"),
        pytest.param("openai_codex", "openai-codex/gpt-5.4", "openai-codex/gpt-5.4", id="codex-typed-prefixed"),
        pytest.param("minimax_global", "MiniMax-M2", "minimax-global/MiniMax-M2", id="minimax-typed-bare"),
        pytest.param("deepseek", "deepseek-chat", "deepseek/deepseek-chat", id="a-provider-that-routes-on-it"),
        pytest.param("azure_openai", "my-deployment", "azure-openai/my-deployment", id="azure-names-its-provider-too"),
        pytest.param("zai", "zhipu/glm-4.6", "zai/glm-4.6", id="a-former-name-is-canonicalized"),
        pytest.param("zai", "openrouter/z-ai/glm-4.6", "openrouter/z-ai/glm-4.6", id="a-declared-skip-prefix-is-left"),
    ],
)
def test_a_typed_model_is_stored_the_way_it_resolves_back(slug: str, typed: str, stored: str) -> None:
    """The add-model screen takes free text, and a bare id is claimed by keyword
    matching rather than by the provider it was entered under: "gpt-5.6-sol"
    resolves to OpenAI.

    Every provider now stores a qualified id, not the three whose own client
    strips the prefix back off. Azure included: its deployment comes off again in
    the URL builder, which is where that belongs -- storing it bare was the one
    thing that made Azure ids shaped unlike everyone else's.
    """
    from raven.tui_rpc.methods.model import _stored_spelling

    assert _stored_spelling(slug, typed) == stored


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_every_provider_stores_a_model_id_that_finds_it_again(spec) -> None:
    """A sweep rather than a list of the cases we thought of: the id a provider
    stores has to resolve back to that provider, or the request leaves for
    whoever else claims the bare name. Providers that route on the prefix or use
    the id verbatim are covered by resolving as themselves.
    """
    from raven.providers.registry import find_by_model
    from raven.tui_rpc.methods.model import _stored_spelling

    stored = _stored_spelling(spec.name, "some-model")
    resolved = find_by_model(stored)

    assert resolved is not None and resolved.name == spec.name, f"{stored} resolves to {resolved and resolved.name}"


async def test_a_user_written_overlay_reaches_the_picker(fake_home: Path) -> None:
    """A model the catalogues cannot describe still arrives with a name.

    The list already let a model be added; naming one is what was missing, so a
    self-hosted deployment reached the picker as a bare id with no description
    line at all -- `_model_labels` skips every row nothing describes.
    """
    _write_config(
        fake_home,
        {
            "agents": {"defaults": {"model": "hosted-vllm/my-finetune-v3"}},
            "providers": {
                "hosted_vllm": {
                    "apiBase": "http://localhost:8000/v1",
                    "models": ["hosted-vllm/my-finetune-v3"],
                    "modelOverlay": {"my-finetune-v3": {"label": "Our finetune", "description": "tuned on tickets"}},
                }
            },
        },
    )
    entry = _entry(await model_options({}), "hosted_vllm")
    label = (entry.get("model_labels") or {}).get("hosted-vllm/my-finetune-v3")
    assert label == {"label": "Our finetune", "description": "tuned on tickets"}
