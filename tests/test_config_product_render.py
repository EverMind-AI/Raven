"""The launcher library: the product-agnostic machines behind agents/ launchers.

``tests/test_agents_research_launcher.py`` stays the behavioral pin for the
composed render; these pin each machine alone, with product-neutral tables,
so the next product inherits tested parts rather than a copy of run.py.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from raven.config import product_render as render


def test_env_wins_over_the_env_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\n\nKEY=from-file\nOTHER = spaced \n", encoding="utf-8")
    monkeypatch.setenv("KEY", "from-env")
    assert render.env_value("KEY", env_file=f) == "from-env"
    monkeypatch.delenv("KEY")
    assert render.env_value("KEY", env_file=f) == "from-file"
    assert render.env_value("OTHER", env_file=f) == "spaced"
    assert render.env_value("ABSENT", env_file=f) is None
    assert render.env_value("KEY") is None, "no file, no env, no value"


def test_dig_and_put_roundtrip():
    data: dict = {}
    render.put(data, ("a", "b", "c"), "v")
    assert data == {"a": {"b": {"c": "v"}}}
    assert render.dig(data, ("a", "b", "c")) == "v"
    assert render.dig(data, ("a", "missing", "c")) == ""
    assert render.dig({"a": "leaf"}, ("a", "b")) == "", "a non-dict on the way is not an error"


def test_secret_slots_fall_back_to_the_host_except_required():
    slots = {"P_KEY": ("providers", "x", "apiKey"), "P_EXTRA": ("tools", "extra")}
    host = {"providers": {"x": {"apiKey": "host-key"}}, "tools": {"extra": "host-extra"}}

    config: dict = {}
    render.apply_secret_slots(
        config, host, slots=slots, required=("P_KEY",), lookup={"P_KEY": None, "P_EXTRA": None}.get
    )
    assert render.dig(config, ("tools", "extra")) == "host-extra"
    assert render.dig(config, ("providers", "x", "apiKey")) == "", "a required secret never inherits per-slot"

    config = {}
    render.apply_secret_slots(
        config, host, slots=slots, required=("P_KEY",), lookup={"P_KEY": "own", "P_EXTRA": "mine"}.get
    )
    assert render.dig(config, ("providers", "x", "apiKey")) == "own"
    assert render.dig(config, ("tools", "extra")) == "mine", "an own value beats the host fallback"


def test_inherit_llm_takes_the_hosts_brains_not_its_limits():
    host = {
        "providers": {"open": {"apiKey": "k"}},
        "routing": {"rules": []},
        "agents": {"defaults": {"provider": "open", "model": "m", "maxToolIterations": 40}},
    }
    config = {"agents": {"defaults": {"maxToolIterations": 20}}}
    taken = render.inherit_llm(config, host)
    assert "provider=open" in taken and "model=m" in taken
    assert config["providers"] == host["providers"]
    assert config["routing"] == {"rules": []}
    assert config["agents"]["defaults"]["provider"] == "open"
    assert config["agents"]["defaults"]["maxToolIterations"] == 20, "operating limits stay the product's"


def test_inherit_llm_declines_without_a_host_key():
    assert render.inherit_llm({}, {"providers": {"open": {"baseUrl": "u"}}}) == ""


@pytest.fixture()
def oauth_host(tmp_path, monkeypatch):
    home = tmp_path / "host"
    token_dir = home / "oauth" / "chatgpt"
    token_dir.mkdir(parents=True)
    monkeypatch.setenv("RAVEN_HOME", str(home))
    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    monkeypatch.delenv("CHATGPT_AUTH_FILE", raising=False)
    for name in ("MODEL", "PROVIDER", "PROTOCOL", "REASONING_EFFORT"):
        monkeypatch.delenv(f"RAVEN_PARENT_{name}", raising=False)
    token = token_dir / "auth.json"
    token.write_text(json.dumps({"refresh_token": "synthetic-refresh-token"}), encoding="utf-8")
    host = {
        "providers": {
            "openai_codex": {
                "models": ["openai-codex/gpt-5.6-sol"],
                "futureSetting": {"opaque": True},
            }
        },
        "agents": {
            "defaults": {
                "provider": "openai_codex",
                "model": "openai-codex/gpt-5.6-sol",
                "reasoningEffort": "low",
                "maxToolIterations": 40,
            }
        },
        "routing": {"rules": []},
        "tools": {"web": {"providers": {"serper": {"apiKey": "synthetic-search-key"}}}},
    }
    return host, token


@pytest.mark.parametrize("name", ["openai_codex", "openaiCodex", "openai-codex"])
@pytest.mark.parametrize(
    "credentials",
    [
        {"access_token": "synthetic-access-token"},
        {"refresh_token": "synthetic-refresh-token"},
        {"access_token": "expired-access-token", "refresh_token": "synthetic-refresh-token", "expires_at": 0},
    ],
)
def test_inherit_llm_accepts_stored_oauth_credentials(oauth_host, name, credentials):
    host, token = oauth_host
    token.write_text(json.dumps(credentials), encoding="utf-8")
    host["providers"][name] = host["providers"].pop("openai_codex")
    host["agents"]["defaults"]["provider"] = name
    config = {"agents": {"defaults": {"maxToolIterations": 20}}}

    taken = render.inherit_llm(config, host)

    assert taken
    assert config["providers"] == host["providers"]
    assert config["routing"] == host["routing"]
    assert config["agents"]["defaults"] == {
        "provider": name,
        "model": "openai-codex/gpt-5.6-sol",
        "reasoningEffort": "low",
        "maxToolIterations": 20,
    }
    assert "synthetic-refresh-token" not in json.dumps(config)
    assert "synthetic-access-token" not in json.dumps(config)


@pytest.mark.parametrize("provider", ["openai_codex", "auto", None])
def test_inherit_llm_accepts_oauth_without_an_explicit_provider_section(oauth_host, provider):
    host, _ = oauth_host
    host["providers"] = {}
    if provider is None:
        host["agents"]["defaults"].pop("provider")
    else:
        host["agents"]["defaults"]["provider"] = provider

    config = {}
    assert render.inherit_llm(config, host)
    assert config["agents"]["defaults"]["model"] == "openai-codex/gpt-5.6-sol"


@pytest.mark.parametrize("contents", [None, "{}", '{"device_code_requested_at": 1}', "invalid json"])
@pytest.mark.parametrize("other_key", [False, True])
def test_inherit_llm_declines_unusable_oauth_even_with_another_provider_key(oauth_host, contents, other_key):
    host, token = oauth_host
    if contents is None:
        token.unlink()
    else:
        token.write_text(contents, encoding="utf-8")
    if other_key:
        host["providers"]["openrouter"] = {"apiKey": "synthetic-other-key"}
    config = {"agents": {"defaults": {"maxToolIterations": 20}}}

    assert render.inherit_llm(config, host) == ""
    assert config == {"agents": {"defaults": {"maxToolIterations": 20}}}


@pytest.mark.parametrize("parent_key", ["", "synthetic-parent-key"])
def test_inherit_llm_checks_the_parent_binding_instead_of_the_host_default(oauth_host, monkeypatch, parent_key):
    host, _ = oauth_host
    host["providers"]["openrouter"] = {"apiKey": parent_key}
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "openrouter/openai/gpt-5.6-sol")
    monkeypatch.setenv("RAVEN_PARENT_PROVIDER", "openrouter")
    config = {}

    taken = render.inherit_llm(config, host)

    assert bool(taken) == bool(parent_key)
    if parent_key:
        assert config["agents"]["defaults"]["provider"] == "openrouter"
        assert config["agents"]["defaults"]["model"] == "openrouter/openai/gpt-5.6-sol"
    else:
        assert config == {}


@pytest.mark.parametrize(
    "provider, model, section",
    [
        ("openrouter", "openrouter/openai/gpt-5.6-sol", {"apiKey": "synthetic-key"}),
        ("openrouter", "openrouter/openai/gpt-5.6-sol", {"api_key": "synthetic-key"}),
        ("openrouter", "openrouter/openai/gpt-5.6-sol", {"endpoints": [{"label": "primary", "apiKey": "k"}]}),
        ("gemini", "gemini/gemini-2.5-pro", {"apiKeyList": ["synthetic-key"]}),
        ("ollama_chat", "ollama_chat/llama3", {"apiBase": "http://localhost:11434"}),
    ],
)
def test_inherit_llm_uses_runtime_credential_rules(provider, model, section):
    host = {
        "providers": {provider: section},
        "agents": {"defaults": {"provider": provider, "model": model}},
    }
    config = {}

    assert render.inherit_llm(config, host)
    assert config["providers"] == host["providers"]
    assert config["agents"]["defaults"]["model"] == model


@pytest.fixture()
def oauth_launcher(oauth_host, monkeypatch, product):
    import importlib.util

    host, _ = oauth_host
    home = Path(os.environ["RAVEN_HOME"])
    (home / "config.json").write_text(json.dumps(host), encoding="utf-8")
    path = Path(render.__file__).resolve().parents[2] / "agents" / product / "run.py"
    spec = importlib.util.spec_from_file_location(f"oauth_{product.replace('-', '_')}", path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher, "env_value", lambda name: None)
    monkeypatch.setattr(launcher, "log", lambda message: None)
    if product == "raven-ppt":
        monkeypatch.setattr(launcher, "resolve_context_window", lambda *args: None)

    def capture_rendered(config, root, **_kwargs):
        target = root / ".config.rendered.json"
        target.write_text(json.dumps(config), encoding="utf-8")
        return target

    monkeypatch.setattr(render, "write_rendered", capture_rendered)
    return launcher


@pytest.mark.parametrize("product", ["raven-code", "raven-design", "raven-oncall", "raven-research", "raven-ppt"])
@pytest.mark.parametrize("custom_token_dir", [False, True])
def test_launchers_inherit_oauth_and_resolve_host_credentials(
    oauth_host, oauth_launcher, tmp_path, monkeypatch, product, custom_token_dir
):
    from raven.config.loader import load_config
    from raven.providers.auth import credential_files
    from raven.providers.factory import check_provider_credentials

    host, token = oauth_host
    if custom_token_dir:
        custom = tmp_path / "custom-auth"
        custom.mkdir()
        relocated = custom / "account.json"
        token.replace(relocated)
        token = relocated
        monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(custom))
        monkeypatch.setenv("CHATGPT_AUTH_FILE", "account.json")
    home = Path(os.environ["RAVEN_HOME"])
    if product == "raven-code":
        rendered = oauth_launcher.render_acp_config(oauth_launcher.DEFAULT_CONFIG)
    else:
        rendered = oauth_launcher.render_config(oauth_launcher.DEFAULT_CONFIG)
    data = json.loads(rendered.read_text(encoding="utf-8"))

    assert data["providers"] == host["providers"]
    assert data["routing"] == host["routing"]
    assert data["agents"]["defaults"]["provider"] == "openai_codex"
    assert data["agents"]["defaults"]["model"] == "openai-codex/gpt-5.6-sol"
    assert data["agents"]["defaults"]["reasoningEffort"] == "low"
    assert "synthetic-refresh-token" not in rendered.read_text(encoding="utf-8")
    assert rendered.parent != home
    loaded = load_config(rendered)
    check_provider_credentials(loaded)
    assert credential_files("openai_codex") == [token]


@pytest.mark.parametrize("product", ["raven-code", "raven-oncall", "raven-research", "raven-ppt"])
def test_launchers_own_keys_do_not_require_host_oauth(oauth_host, oauth_launcher, monkeypatch, product):
    from raven.config.loader import load_config
    from raven.providers.factory import check_provider_credentials

    _, token = oauth_host
    token.unlink()
    own_key = oauth_launcher.REQUIRED_SECRETS[0]
    monkeypatch.setattr(oauth_launcher, "env_value", lambda name: "synthetic-own-key" if name == own_key else None)

    def refuse_inheritance(*_args):
        raise AssertionError("an own-key launcher must not consult the host model")

    monkeypatch.setattr(render, "inherit_llm", refuse_inheritance)
    if product == "raven-code":
        rendered = oauth_launcher.render_acp_config(oauth_launcher.DEFAULT_CONFIG)
    else:
        rendered = oauth_launcher.render_config(oauth_launcher.DEFAULT_CONFIG)
    loaded = load_config(rendered)

    assert loaded.get_provider_name() != "openai_codex"
    assert loaded.get_api_key() == "synthetic-own-key"
    check_provider_credentials(loaded)


@pytest.mark.parametrize(
    "variant, reason",
    [
        ("signed-in", ""),
        ("signed-out", "raven provider login openai-codex"),
        ("signed-out with an OpenRouter key", "raven provider login openai-codex"),
        ("API key", ""),
    ],
)
def test_inherit_refusal_names_what_inherit_llm_refuses_on(oauth_host, variant, reason):
    host, token = oauth_host
    if variant.startswith("signed-out"):
        token.unlink()
    if variant.endswith("OpenRouter key"):
        host["providers"]["openrouter"] = {"apiKey": "synthetic-other-key"}
    if variant == "API key":
        host["providers"] = {"openrouter": {"apiKey": "synthetic-key"}}
        host["agents"]["defaults"].update(provider="openrouter", model="openrouter/openai/gpt-5.6-sol")
    config = {"agents": {"defaults": {"maxToolIterations": 20}}}

    refusal = render.inherit_refusal(copy.deepcopy(config), copy.deepcopy(host))
    taken = render.inherit_llm(copy.deepcopy(config), copy.deepcopy(host))

    assert bool(refusal) == (taken == "")
    if reason:
        assert reason in refusal
    else:
        assert refusal == ""


@pytest.mark.parametrize("name", ["openaiCodex", "openai-codex"])
def test_inherit_refusal_agrees_with_inherit_llm_on_an_aliased_section(oauth_host, name):
    host, _ = oauth_host
    host["providers"][name] = host["providers"].pop("openai_codex")
    host["agents"]["defaults"]["provider"] = name

    assert render.inherit_refusal({}, copy.deepcopy(host)) == ""
    assert render.inherit_llm({}, copy.deepcopy(host))


def test_inherit_refusal_changes_neither_argument(oauth_host, monkeypatch):
    host, _ = oauth_host
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "openai-codex/gpt-5.6-sol")
    monkeypatch.setenv("RAVEN_PARENT_PROVIDER", "openai_codex")
    monkeypatch.setenv("RAVEN_PARENT_PROTOCOL", "responses")
    config = {"agents": {"defaults": {"maxToolIterations": 20}}}
    config_before, host_before = copy.deepcopy(config), copy.deepcopy(host)

    assert render.inherit_refusal(config, host) == ""
    assert (config, host) == (config_before, host_before)
    render.inherit_llm(config, host)
    assert "modelProtocols" in host["providers"]["openai_codex"], "the write inherit_refusal must not make"


def test_inherit_refusal_explains_the_riders_binding_not_the_host_default(oauth_host, monkeypatch):
    host, _ = oauth_host
    host["providers"]["openrouter"] = {"apiKey": ""}
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "openrouter/openai/gpt-5.6-sol")
    monkeypatch.setenv("RAVEN_PARENT_PROVIDER", "openrouter")

    assert render.inherit_llm({}, copy.deepcopy(host)) == ""
    assert "raven provider set openrouter" in render.inherit_refusal({}, host)


def test_inherit_refusal_reads_the_config_when_the_host_names_no_provider(oauth_host):
    host, _ = oauth_host
    del host["agents"]["defaults"]["provider"]
    configured = {"agents": {"defaults": {"provider": "openrouter"}}}

    assert render.inherit_refusal({}, copy.deepcopy(host)) == ""
    assert render.inherit_llm(copy.deepcopy(configured), copy.deepcopy(host)) == ""
    assert "raven provider set openrouter" in render.inherit_refusal(configured, host)


def test_an_empty_credential_summary_still_refuses_with_a_reason(oauth_host, monkeypatch):
    from raven.providers import factory
    from raven.providers.auth import MissingCredentialsError

    def refuse(_config, model=None):
        raise MissingCredentialsError("")

    monkeypatch.setattr(factory, "check_provider_credentials", refuse)
    host, _ = oauth_host

    assert render.inherit_llm({}, copy.deepcopy(host)) == ""
    assert render.inherit_refusal({}, host) == "the inherited model has no usable credentials"


def test_a_host_with_no_providers_gets_a_one_line_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    for name in ("MODEL", "PROVIDER", "PROTOCOL", "REASONING_EFFORT"):
        monkeypatch.delenv(f"RAVEN_PARENT_{name}", raising=False)

    reason = render.inherit_refusal({}, {})

    assert reason
    assert "\n" not in reason, "raven agents new reads a launcher's refusal one line at a time"


def test_inherit_llm_refuses_a_non_string_model_through_config_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    host = {"providers": {"openrouter": {"apiKey": "synthetic-key"}}, "agents": {"defaults": {"model": 42}}}

    with pytest.raises(ValueError):
        render.inherit_llm({}, host)


@pytest.mark.parametrize("product", ["raven-code", "raven-design", "raven-oncall", "raven-research", "raven-ppt"])
def test_launchers_name_why_they_cannot_inherit(oauth_host, oauth_launcher, product):
    _, token = oauth_host
    token.unlink()

    with pytest.raises(SystemExit) as refused:
        if product == "raven-code":
            oauth_launcher.render_acp_config(oauth_launcher.DEFAULT_CONFIG)
        else:
            oauth_launcher.render_config(oauth_launcher.DEFAULT_CONFIG)

    message = str(refused.value)
    assert "raven provider login openai-codex" in message
    if product == "raven-design":
        assert re.search(r"before starting Design \(.+\)$", message), message
    else:
        assert f"{oauth_launcher.REQUIRED_SECRETS[0]} is not set" in message
        assert re.search(r"cannot be inherited \(.+\); put the key in", message), message


@pytest.mark.parametrize("product", ["raven-code", "raven-design", "raven-oncall", "raven-research", "raven-ppt"])
def test_launchers_still_refuse_in_words_on_a_raven_without_inherit_refusal(
    oauth_host, oauth_launcher, monkeypatch, product
):
    _, token = oauth_host
    token.unlink()
    monkeypatch.delattr(render, "inherit_refusal")

    with pytest.raises(SystemExit) as refused:
        if product == "raven-code":
            oauth_launcher.render_acp_config(oauth_launcher.DEFAULT_CONFIG)
        else:
            oauth_launcher.render_config(oauth_launcher.DEFAULT_CONFIG)

    message = str(refused.value)
    if product == "raven-design":
        assert message.endswith("before starting Design")
    else:
        assert (
            f"{oauth_launcher.REQUIRED_SECRETS[0]} is not set and the host's model cannot be inherited; put" in message
        )


def test_inherit_llm_honours_the_parent_riders_on_the_inheritance_branch(monkeypatch):
    """The fork launchers' riders, kept at the shared seat (G1): the cli
    dispatcher injects RAVEN_PARENT_MODEL / RAVEN_PARENT_REASONING_EFFORT per
    spawn; on the inheritance branch the model rider wins, the provider is
    re-inferred by models-list membership (the fork's back-query), and the
    effort rider overrides the host's copied reasoningEffort. Launch-time
    semantics -- weaker than the fork's per-turn form, ledgered in D3."""
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "open/parent-model")
    monkeypatch.setenv("RAVEN_PARENT_REASONING_EFFORT", "low")
    host = {
        "providers": {
            "open": {"apiKey": "k", "models": ["open/parent-model"]},
            "other": {"apiKey": "k2", "models": ["other/m"]},
        },
        "agents": {"defaults": {"provider": "other", "model": "other/m", "reasoningEffort": "high"}},
    }
    config = {}
    taken = render.inherit_llm(config, host)
    defaults = config["agents"]["defaults"]
    assert defaults["model"] == "open/parent-model"
    assert defaults["provider"] == "open", "provider re-inferred from the models list, not the host default"
    assert defaults["reasoningEffort"] == "low"
    assert "reasoning_effort=low" in taken


def test_inherit_llm_honours_parent_provider_and_protocol(monkeypatch):
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "openrouter/openai/gpt-5.6-sol")
    monkeypatch.setenv("RAVEN_PARENT_PROVIDER", "openrouter")
    monkeypatch.setenv("RAVEN_PARENT_PROTOCOL", "responses")
    host = {
        "providers": {
            "openrouter": {"apiKey": "k", "models": ["openrouter/openai/gpt-5.6-sol"]},
            "custom": {"apiKey": "placeholder", "models": ["openai/gpt-5.6-sol"]},
        },
        "agents": {"defaults": {"provider": "custom", "model": "openai/gpt-5.6-sol"}},
    }
    config: dict = {}

    render.inherit_llm(config, host)

    defaults = config["agents"]["defaults"]
    assert defaults["provider"] == "openrouter"
    assert defaults["model"] == "openrouter/openai/gpt-5.6-sol"
    assert config["providers"]["openrouter"]["modelProtocols"] == {"openrouter/openai/gpt-5.6-sol": "responses"}


def test_inherit_llm_names_the_provider_from_the_model_prefix(monkeypatch):
    """A LiteLLM-backed parent sends no RAVEN_PARENT_PROVIDER, and a model the
    page typed in is listed under no block: the stored id's own prefix names
    the provider, and the host default stands in when even that fails."""
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "openrouter/some-new-model")
    monkeypatch.delenv("RAVEN_PARENT_PROVIDER", raising=False)
    host = {
        "providers": {"openrouter": {"apiKey": "k", "models": []}, "custom": {"apiKey": "k", "models": []}},
        "agents": {"defaults": {"provider": "custom", "model": "openai/x"}},
    }
    config: dict = {}
    render.inherit_llm(config, host)
    assert config["agents"]["defaults"]["provider"] == "openrouter"

    monkeypatch.setenv("RAVEN_PARENT_MODEL", "bare-model")
    config = {}
    render.inherit_llm(config, host)
    assert config["agents"]["defaults"]["provider"] == "custom", "host default rather than an empty provider"


def test_design_ignores_own_key_and_inherits_parent_riders(monkeypatch, tmp_path):
    import importlib.util

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DESIGN_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("DESIGN_API_KEY", "sk-own")
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "vendor/parent-model")
    monkeypatch.setenv("RAVEN_PARENT_REASONING_EFFORT", "low")
    for name in ("DESIGN_ACP_HOME", "DESIGN_IMAGE_API_KEY", "DESIGN_SERPER_API_KEY", "DESIGN_JINA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "host/model", "provider": "custom"}},
                "providers": {"custom": {"apiKey": "host-key", "apiBase": "https://vendor.example/v1"}},
            }
        )
    )
    launcher_path = Path(render.__file__).resolve().parents[2] / "agents" / "raven-design" / "run.py"
    spec = importlib.util.spec_from_file_location("design_run_riders", launcher_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    data = json.loads(mod.render_config(launcher_path.parent / "config.json").read_text())
    assert data["agents"]["defaults"]["model"] == "vendor/parent-model"
    assert data["agents"]["defaults"]["reasoningEffort"] == "low"

    assert data["providers"]["custom"]["apiKey"] == "host-key"
    assert "sk-own" not in json.dumps(data)


def test_state_root_prefers_the_override(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    assert render.product_state_root("prod-x", override=str(tmp_path / "s")) == tmp_path / "s"
    assert render.product_state_root("prod-x") == tmp_path / "home" / "workspace" / "subagent_sessions" / "prod-x"


def test_seed_once_writes_once(tmp_path):
    target = tmp_path / "deep" / "soul.md"
    calls: list[int] = []
    assert render.seed_once(target, lambda: calls.append(1) or "first") is True
    assert target.read_text() == "first"
    assert render.seed_once(target, lambda: calls.append(1) or "second") is False
    assert target.read_text() == "first", "a product update must not overwrite what an operator tuned"
    assert calls == [1], "the render is not evaluated when the target exists"


def _receipt(target):
    return target.with_name(f".{target.name}.generated.sha256")


def _backups(target):
    return sorted(target.parent.glob(f"{target.name}.bak-*"))


def test_write_generated_writes_the_product_text_byte_for_byte_with_a_receipt(tmp_path):
    target = tmp_path / "profile" / "agent.md"
    assert render.write_generated(target, "line one\nline two") == "written"
    assert target.read_bytes() == b"line one\nline two"
    assert _receipt(target).read_text().strip() == hashlib.sha256(b"line one\nline two").hexdigest()
    assert render.write_generated(target, "line one\nline two") == "unchanged"
    assert _backups(target) == []


def test_write_generated_follows_every_product_update_of_a_file_it_wrote(tmp_path):
    target = tmp_path / "agent.md"
    render.write_generated(target, "v1")
    assert render.write_generated(target, "v2") == "written"
    assert target.read_text() == "v2"
    assert _backups(target) == [], "the product's own text is not an edit"


def test_an_edited_file_is_backed_up_before_it_is_regenerated(tmp_path):
    target = tmp_path / "agent.md"
    render.write_generated(target, "v1")
    target.write_bytes(b"operator tuned\r\n")
    said: list[str] = []
    assert render.write_generated(target, "v2", notify=said.append) == "written"
    assert target.read_text() == "v2"
    [backup] = _backups(target)
    assert backup.read_bytes() == b"operator tuned\r\n", "the backup is the edited bytes, untranslated"
    assert str(backup) in said[0] and "agent.local.md" in said[0]
    assert render.write_generated(target, "v2", notify=said.append) == "unchanged"
    assert len(said) == 1 and len(_backups(target)) == 1, "a regenerated file is the product's again"


def test_a_file_without_a_receipt_is_the_products_when_a_legacy_digest_or_the_text_matches(tmp_path):
    legacy = tmp_path / "legacy.md"
    legacy.write_text("old release text")
    digest = hashlib.sha256(b"old release text").hexdigest()
    assert render.write_generated(legacy, "new", legacy_digests={digest}) == "written"
    assert _backups(legacy) == []

    current = tmp_path / "current.md"
    current.write_text("same text")
    assert render.write_generated(current, "same text") == "unchanged"
    assert _backups(current) == []
    assert _receipt(current).is_file(), "the receipt is recorded for the next launch"

    unknown = tmp_path / "unknown.md"
    unknown.write_text("whoever wrote this")
    _receipt(unknown).write_text("not a digest\n")
    render.write_generated(unknown, "new")
    assert [b.read_text() for b in _backups(unknown)] == ["whoever wrote this"], "unprovable means backed up"


def test_another_launch_rewriting_the_file_is_not_an_edit(tmp_path):
    """Two bindings of one product share the file; the receipt follows whichever
    launch wrote last, so a model switch is never mistaken for an edit."""
    target = tmp_path / "agent.md"
    render.write_generated(target, "anthropic variant")
    render.write_generated(target, "default variant")
    render.write_generated(target, "anthropic variant")
    assert _backups(target) == []


def test_the_local_addendum_follows_the_product_text(tmp_path):
    target = tmp_path / "profile" / "agent.md"
    local = render.local_addendum(target)
    assert local == tmp_path / "profile" / "agent.local.md"
    render.write_generated(target, "product\n")
    local.write_text("\nmy own rule\n")
    assert render.write_generated(target, "product\n") == "written"
    assert target.read_text() == "product\n\nmy own rule\n"
    local.write_text("a different rule")
    render.write_generated(target, "product v2\n")
    assert target.read_text() == "product v2\n\na different rule\n"
    local.unlink()
    render.write_generated(target, "product v2\n")
    assert target.read_text() == "product v2\n"
    assert _backups(target) == [], "changing the addendum is not editing the file"


def test_turning_a_generated_file_off_removes_only_what_the_product_wrote(tmp_path):
    said: list[str] = []
    managed = tmp_path / "managed.md"
    render.write_generated(managed, "text")
    assert render.write_generated(managed, None, notify=said.append) == "removed"
    assert not managed.exists() and not _receipt(managed).exists()
    assert render.write_generated(managed, None) == "absent"

    edited = tmp_path / "edited.md"
    render.write_generated(edited, "text")
    edited.write_text("operator tuned")
    assert render.write_generated(edited, None, notify=said.append) == "kept"
    assert edited.read_text() == "operator tuned" and not _receipt(edited).exists()
    assert "edited in place" in said[-1]

    foreign = tmp_path / "foreign.md"
    foreign.write_text("a template someone else wrote")
    before = len(said)
    assert render.write_generated(foreign, None, notify=said.append) == "kept"
    assert len(said) == before, "a file this product never tracked is not its business"

    render.local_addendum(managed).write_text("my rule")
    render.write_generated(managed, None, notify=said.append)
    assert "is not applied" in said[-1] and not managed.exists()


def test_a_failed_backup_leaves_the_edited_file_and_its_receipt_alone(tmp_path, monkeypatch):
    target = tmp_path / "agent.md"
    render.write_generated(target, "v1")
    receipt = _receipt(target).read_text()
    target.write_text("operator tuned")

    def no_room(_target, _raw):
        raise OSError("no space left on device")

    monkeypatch.setattr(render, "_back_up", no_room)
    with pytest.raises(OSError):
        render.write_generated(target, "v2")
    assert target.read_text() == "operator tuned"
    assert _receipt(target).read_text() == receipt


def test_a_failed_write_leaves_the_previous_text_and_receipt_in_place(tmp_path, monkeypatch):
    target = tmp_path / "agent.md"
    render.write_generated(target, "v1")
    receipt = _receipt(target).read_text()

    def interrupted(_path, _data, **_kwargs):
        raise OSError("interrupted")

    monkeypatch.setattr(render, "atomic_replace", interrupted)
    with pytest.raises(OSError):
        render.write_generated(target, "v2")
    assert target.read_text() == "v1"
    assert _receipt(target).read_text() == receipt


def _fail_when(monkeypatch, failing) -> None:
    """Make every ``atomic_replace(path, data)`` that ``failing`` matches raise.

    Matched by what is written, not by call order, so the test reads the same
    whichever order the function writes in.
    """
    real = render.atomic_replace

    def flaky(path, data, **kwargs):
        if failing(path, data):
            raise OSError(f"cannot write {path.name}")
        return real(path, data, **kwargs)

    monkeypatch.setattr(render, "atomic_replace", flaky)


def test_a_failed_receipt_after_the_new_text_landed_still_recognises_it(tmp_path, monkeypatch):
    """Target written, final receipt lost: turning the file off must still
    remove it, or a switched-off conduct stays in the model's context."""
    target = tmp_path / "agent.md"
    render.write_generated(target, "v1")
    v2 = hashlib.sha256(b"v2").hexdigest()
    _fail_when(monkeypatch, lambda path, data: path == _receipt(target) and data == v2 + "\n")
    with pytest.raises(OSError):
        render.write_generated(target, "v2")
    monkeypatch.undo()
    assert target.read_text() == "v2"
    assert render.write_generated(target, None) == "removed"
    assert _backups(target) == []


def test_a_failed_target_write_keeps_the_old_text_recognised(tmp_path, monkeypatch):
    target = tmp_path / "agent.md"
    render.write_generated(target, "v1")
    target.write_text("operator tuned")
    _fail_when(monkeypatch, lambda path, _data: path == target)
    with pytest.raises(OSError):
        render.write_generated(target, "v2")
    monkeypatch.undo()
    assert target.read_text() == "operator tuned"
    assert len(_backups(target)) == 1
    assert render.write_generated(target, "v2") == "written"
    assert len(_backups(target)) == 1, "an edit already backed up is not backed up twice"


def test_a_fresh_file_whose_write_failed_leaves_nothing_unrecognised(tmp_path, monkeypatch):
    target = tmp_path / "agent.md"
    _fail_when(monkeypatch, lambda path, _data: path == target)
    with pytest.raises(OSError):
        render.write_generated(target, "v1")
    monkeypatch.undo()
    assert not target.exists()
    assert render.write_generated(target, "v1") == "written"
    assert render.write_generated(target, None) == "removed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not available on Windows")
def test_a_backup_is_never_more_open_than_the_file_it_copies(tmp_path):
    previous = os.umask(0o022)
    try:
        private = tmp_path / "private.md"
        render.write_generated(private, "v1")
        private.write_text("private tuning")
        private.chmod(0o600)
        render.write_generated(private, "v2")
        [backup] = _backups(private)
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert stat.S_IMODE(private.stat().st_mode) == 0o600

        shared = tmp_path / "shared.md"
        shared.write_text("group-readable tuning")
        shared.chmod(0o644)
        render.write_generated(shared, "v1")
        [backup] = _backups(shared)
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600, "owner-only even when the original was not"
    finally:
        os.umask(previous)


def test_backups_taken_in_the_same_second_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(render.time, "strftime", lambda _fmt: "20261016-120000")
    target = tmp_path / "agent.md"
    for edit, release in (("first edit", "v1"), ("second edit", "v2")):
        target.write_text(edit)
        render.write_generated(target, release)
    assert sorted(b.read_text() for b in _backups(target)) == ["first edit", "second edit"]


def test_mode_catalogue_assembles_the_acp_modes_contract(tmp_path):
    (tmp_path / "deep.json").write_text(json.dumps({"flow": {"maxIterations": 60}}), encoding="utf-8")
    labels = {"fast": ("Fast", "the default"), "deep": ("Deep", "longer"), "ghost": ("Ghost", "no file")}
    caps = {False: 20, True: 60}

    def resolve(overlay):
        return caps[bool(overlay)], {"flow": overlay.get("flow", {})}

    catalogue = render.mode_catalogue(
        tmp_path, labels, baseline="fast", overlay_keys=frozenset({"flow"}), resolve=resolve
    )
    assert set(catalogue) == {"fast", "deep"}, "a labeled mode without its overlay file is skipped"
    assert catalogue["fast"]["overlay"] == {"flow": {}}, (
        "the overlay is the product's diff alone; the loop hands hooks the cap as ctx.max_iterations"
    )
    assert catalogue["deep"] == {
        "name": "Deep",
        "description": "longer",
        "maxToolIterations": 60,
        "overlay": {"flow": {"maxIterations": 60}},
    }


def test_mode_catalogue_lifts_a_modes_reasoning_effort_onto_the_entry(tmp_path):
    """The effort a mode asks for is the trunk's knob (`AcpModeConfig.reasoningEffort`),
    so it leaves the overlay and rides the entry, where the trunk dispenses it to
    every call a session in that mode makes. The overlay itself is untouched."""
    (tmp_path / "medium.json").write_text(
        json.dumps({"agents": {"defaults": {"reasoningEffort": "none"}}}), encoding="utf-8"
    )
    labels = {"high": ("High", "the baseline"), "medium": ("Medium", "no thinking")}

    catalogue = render.mode_catalogue(
        tmp_path, labels, baseline="high", overlay_keys=frozenset({"agents"}), resolve=lambda o: (150, o)
    )

    assert catalogue["medium"]["reasoningEffort"] == "none"
    assert catalogue["medium"]["overlay"] == {"agents": {"defaults": {"reasoningEffort": "none"}}}
    assert "reasoningEffort" not in catalogue["high"], "the baseline inherits agents.defaults"


def test_mode_catalogue_refuses_an_unknown_overlay_key(tmp_path):
    (tmp_path / "deep.json").write_text(json.dumps({"surprise": 1}), encoding="utf-8")
    labels = {"fast": ("Fast", "d"), "deep": ("Deep", "d")}
    with pytest.raises(SystemExit, match="surprise"):
        render.mode_catalogue(
            tmp_path, labels, baseline="fast", overlay_keys=frozenset({"flow"}), resolve=lambda o: (None, {})
        )


def test_mode_catalogue_is_empty_without_a_modes_dir(tmp_path):
    out = render.mode_catalogue(
        tmp_path / "absent",
        {"fast": ("F", "d")},
        baseline="fast",
        overlay_keys=frozenset(),
        resolve=lambda o: (None, {}),
    )
    assert out == {}


def test_sweep_removes_only_the_dead(tmp_path):
    own = tmp_path / f".config.rendered.{os.getpid()}.json"
    own.write_text("{}")
    dead = tmp_path / ".config.rendered.999999.json"
    dead.write_text("{}")
    malformed = tmp_path / ".config.rendered.notapid.json"
    malformed.write_text("{}")

    render.sweep_stale_renders(tmp_path)

    assert own.exists(), "a live server's render survives the sweep"
    assert not dead.exists()
    assert not malformed.exists()


def test_sweep_preserves_a_running_child_and_removes_its_render_after_exit(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"], stdin=subprocess.PIPE)
    rendered = tmp_path / f".config.rendered.{child.pid}.json"
    rendered.write_text("{}", encoding="utf-8")
    try:
        render.sweep_stale_renders(tmp_path)
        assert rendered.exists()
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=1)
        child.communicate(input=b"x", timeout=10)
        render.sweep_stale_renders(tmp_path)
        assert not rendered.exists()
    finally:
        child.kill()
        child.wait(timeout=10)


def test_write_rendered_is_owner_only_and_pid_named(tmp_path):
    rendered = render.write_rendered({"a": 1}, tmp_path)
    assert rendered.name == f".config.rendered.{os.getpid()}.json"
    if os.name != "nt":
        assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
    assert json.loads(rendered.read_text()) == {"a": 1}


def test_write_rendered_works_without_fchmod(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "fchmod", raising=False)

    rendered = render.write_rendered({"a": 1}, tmp_path)

    assert rendered.name == f".config.rendered.{os.getpid()}.json"
    assert json.loads(rendered.read_text(encoding="utf-8")) == {"a": 1}


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not available on Windows")
def test_write_rendered_narrows_existing_permissions(tmp_path):
    rendered = tmp_path / f".config.rendered.{os.getpid()}.json"
    rendered.write_text("{}", encoding="utf-8")
    rendered.chmod(0o644)

    assert render.write_rendered({"a": 1}, tmp_path) == rendered

    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
    assert json.loads(rendered.read_text(encoding="utf-8")) == {"a": 1}


# --- product_acp_home: the engine home never inside the host's (w109) ---------


@pytest.fixture()
def homed(tmp_path, monkeypatch):
    """A scratch RAVEN_HOME with a host config the helper reads."""
    home = tmp_path / "rhome"
    home.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    (home / "config.json").write_text("{}", encoding="utf-8")
    return home


def test_acp_home_defaults_into_the_data_dir_outside_the_host_home(homed):
    got = render.product_acp_home("raven-ppt")
    assert got == homed / "subagent_sessions" / "raven-ppt" / "acp"
    host = render.host_agent_home()
    assert host == homed / "workspace"
    assert got != host and host not in got.parents


def test_acp_home_falls_beside_a_host_home_that_swallows_the_default(homed):
    """An operator who points agents.defaults.workspace at RAVEN_HOME (or any
    ancestor) puts the data directory inside the very tree being handed over;
    the seat then falls beside that home, tagged per instance."""
    (homed / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(homed)}}}), encoding="utf-8"
    )
    got = render.product_acp_home("raven-ppt")
    assert got.parent.parent == homed.parent
    assert got.name == "acp"
    assert got.parent.name.startswith(".raven-ppt-")
    assert got != homed and homed not in got.parents


def test_acp_home_refuses_loud_when_no_outside_placement_exists(homed, monkeypatch):
    """A host home at the filesystem root leaves nowhere beside it; the
    refusal names the product's own override variable."""
    (homed / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"workspace": homed.anchor}}}), encoding="utf-8"
    )
    with pytest.raises(SystemExit) as caught:
        render.product_acp_home("raven-ppt")
    assert "PPT_ACP_HOME" in str(caught.value)


def test_acp_home_refuses_an_uncreatable_placement_actionably(homed, monkeypatch):
    """Outside is not enough: a seat whose nearest existing ancestor is not
    writable is refused naming the override, not with a bare PermissionError."""
    monkeypatch.setattr(render.os, "access", lambda *_a, **_k: False)
    with pytest.raises(SystemExit) as caught:
        render.product_acp_home("raven-code")
    assert "CODE_ACP_HOME" in str(caught.value)
    assert "cannot be created" in str(caught.value)


def test_acp_home_override_wins_outright(homed):
    got = render.product_acp_home("raven-oncall", override="~/elsewhere/acp")
    assert got == render.Path("~/elsewhere/acp").expanduser()


def test_acp_home_var_derivation_matches_the_state_root_family():
    assert render._acp_home_var("raven-code") == "CODE_ACP_HOME"
    assert render._acp_home_var("raven-research-ng") == "RESEARCH_NG_ACP_HOME"


def _case_insensitive_fs(tmp_path) -> bool:
    """Probe, don't assume: APFS defaults one way, CI filesystems the other."""
    probe = tmp_path / "CaseProbe"
    probe.touch()
    return (tmp_path / "caseprobe").exists()


def test_acp_home_sees_through_a_case_variant_host_home(tmp_path, monkeypatch):
    """The HIGH-1 regression pin: a workspace spelled as a case variant of
    RAVEN_HOME names the same physical tree on a case-insensitive volume;
    resolve() keeps the given spelling, so a string comparison calls the
    default seat "outside" while it sits physically INSIDE the tree the host
    hands over. The guard asks the filesystem (samefile over existing
    ancestors) and must fall beside instead."""
    if not _case_insensitive_fs(tmp_path):
        pytest.skip("filesystem is case-sensitive; the alias cannot be built here")
    home = tmp_path / "rhome"
    home.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    variant = tmp_path / "RHOME"
    (home / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(variant)}}}), encoding="utf-8"
    )
    got = render.product_acp_home("raven-ppt")
    assert os.path.samefile(variant, home), "probe premise: the two spellings are one directory"
    got_existing = next(a for a in (got, *got.parents) if a.exists())
    assert not os.path.samefile(got_existing, home)
    assert got.parent.name.startswith(".raven-ppt-")
    assert got.name == "acp"


def test_acp_home_instance_tag_tells_same_named_homes_apart(tmp_path, monkeypatch):
    """Two instances named alike under different parents must not share the
    sibling seat (the isolation RAVEN_HOME exists to give)."""
    tags = []
    for parent in ("one", "two"):
        home = tmp_path / parent / "rhome"
        home.mkdir(parents=True)
        monkeypatch.setenv("RAVEN_HOME", str(home))
        tags.append(render._instance_tag())
    assert tags[0] != tags[1]
    assert all(tag.startswith("rhome-") for tag in tags)


# --- inherit_host_denials: the host's refusals reach every product render ------


def _exec_tier(tools: dict, command: str):
    from raven.permissions.rules import user_tier

    return user_tier("exec", {"command": command}, tools)


def test_host_denials_merge_into_the_product_and_a_refusal_always_wins():
    host = {
        "permissions": {
            "mode": "full",
            "tools": {
                "exec": {"curl *": "deny", "git *": "allow", "rm *": "ask"},
                "web_fetch": "deny",
                "write_file": "ask",
            },
        },
        "tools": {"exec": {"extraDenyPatterns": [r"\bnc\b", "(", r"\bdd\b"]}},
    }
    config = {
        "permissions": {"mode": "ask", "tools": {"exec": {"curl *": "allow", "ls *": "allow"}, "web_fetch": "allow"}},
        "tools": {"exec": {"timeout": 600, "extraDenyPatterns": [r"\bdd\b"]}},
    }

    carried = render.inherit_host_denials(config, host)

    assert config["permissions"] == {
        "mode": "ask",
        "tools": {"exec": {"curl *": "deny", "ls *": "allow"}, "web_fetch": "deny"},
    }, "only refusals travel; the product's mode and its own allows stay"
    assert config["tools"]["exec"] == {"timeout": 600, "extraDenyPatterns": [r"\bdd\b", r"\bnc\b"]}, (
        "patterns are unioned, and one that does not compile is left behind"
    )
    assert carried == ["exec curl *", "web_fetch", r"\bnc\b"]
    from raven.contracts.permissions import Tier

    assert _exec_tier(config["permissions"]["tools"], "curl -s https://example.com") is Tier.DENY
    assert _exec_tier(config["permissions"]["tools"], "ls -la") is Tier.ALLOW


def test_a_product_exec_tier_becomes_the_fallback_beside_the_hosts_patterns():
    from raven.contracts.permissions import Tier

    config = {"permissions": {"tools": {"exec": "allow"}}}
    render.inherit_host_denials(config, {"permissions": {"tools": {"exec": {"curl *": "deny"}}}})

    tools = config["permissions"]["tools"]
    assert tools["exec"] == {"*": "allow", "curl *": "deny"}
    assert _exec_tier(tools, "curl x") is Tier.DENY
    assert _exec_tier(tools, "make test") is Tier.ALLOW, "the product's own tier still answers everything else"


def test_a_host_that_denies_a_whole_tool_denies_it_in_the_product():
    config = {"permissions": {"tools": {"exec": {"ls *": "allow"}}}}
    render.inherit_host_denials(config, {"permissions": {"tools": {"exec": "deny"}}})
    assert config["permissions"]["tools"]["exec"] == "deny"

    already = {"permissions": {"tools": {"exec": "deny"}}}
    render.inherit_host_denials(already, {"permissions": {"tools": {"exec": {"curl *": "deny"}}}})
    assert already["permissions"]["tools"]["exec"] == "deny", "a whole-tool refusal is already the strictest"


@pytest.mark.parametrize(
    "host",
    [
        {},
        {"permissions": {"mode": "ask", "tools": {"exec": {"git *": "allow"}, "write_file": "ask"}}},
        {"permissions": "garbage", "tools": {"exec": {"extraDenyPatterns": "not-a-list"}}},
        {"tools": {"exec": {"extraDenyPatterns": ["("]}}},
    ],
)
def test_a_host_with_nothing_to_refuse_leaves_the_render_untouched(host):
    config = {"agents": {"defaults": {"model": "m"}}}
    assert render.inherit_host_denials(config, host) == []
    assert config == {"agents": {"defaults": {"model": "m"}}}, "no empty permissions block appears"


def test_write_rendered_carries_the_hosts_denials(homed):
    (homed / "config.json").write_text(
        json.dumps(
            {
                "permissions": {"tools": {"exec": {"curl *": "deny"}}},
                "tools": {"exec": {"extra_deny_patterns": [r"\bcurl\b"]}},
            }
        ),
        encoding="utf-8",
    )
    rendered = json.loads(render.write_rendered({"permissions": {"mode": "ask"}}, homed).read_text())
    assert rendered["permissions"] == {"mode": "ask", "tools": {"exec": {"curl *": "deny"}}}
    assert rendered["tools"]["exec"]["extraDenyPatterns"] == [r"\bcurl\b"], "either host spelling is read"


LAUNCHERS = sorted(Path(__file__).resolve().parents[1].glob("agents/*/run.py")) + [
    Path(__file__).resolve().parents[1] / "raven" / "templates" / "agents_scaffold" / "run.py"
]


@pytest.mark.parametrize("launcher_path", LAUNCHERS, ids=lambda p: p.parent.name)
@pytest.mark.parametrize("exit_code", [0, 17])
def test_windows_launchers_wait_for_the_server_and_forward_its_exit(launcher_path, tmp_path, monkeypatch, exit_code):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"waiting_{launcher_path.parent.name.replace('-', '_')}", launcher_path
    )
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    rendered = tmp_path / "rendered.json"
    monkeypatch.setattr(launcher, "render_config", lambda *args, **kwargs: rendered)
    if hasattr(launcher, "render_acp_config"):
        monkeypatch.setattr(launcher, "render_acp_config", lambda *args, **kwargs: rendered)
    monkeypatch.setattr(launcher, "log", lambda message: None)
    monkeypatch.setattr(
        sys, "argv", ["run.py"] if launcher_path.parent.name == "raven-research" else ["run.py", "--acp"]
    )
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []

    def wait_for_child(argv):
        calls.append(argv)
        return exit_code

    monkeypatch.setattr(launcher.subprocess, "call", wait_for_child)
    monkeypatch.setattr(os, "execv", lambda *args: pytest.fail("Windows must keep the launcher alive"))

    with pytest.raises(SystemExit) as caught:
        launcher.main()

    assert caught.value.code == exit_code
    assert calls == [[sys.executable, "-m", "raven", "acp", "--config", str(rendered)]]


@pytest.mark.parametrize("launcher_path", LAUNCHERS[:-1], ids=lambda p: p.parent.name)
def test_launchers_render_after_sweeping_dead_configs(launcher_path, homed, monkeypatch):
    import importlib.util

    from raven.config.loader import load_config

    monkeypatch.setenv("SERPER_API_KEY", "synthetic-search-key")
    (homed / "config.json").write_text(
        json.dumps(
            {
                "providers": {"openrouter": {"apiKey": "synthetic-host-key"}},
                "agents": {"defaults": {"provider": "openrouter", "model": "openrouter/openai/gpt-4o"}},
            }
        ),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location(f"stale_{launcher_path.parent.name.replace('-', '_')}", launcher_path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher, "env_value", lambda name: None)
    monkeypatch.setattr(launcher, "log", lambda message: None)
    if launcher_path.parent.name == "raven-ppt":
        monkeypatch.setattr(launcher, "resolve_context_window", lambda *args: None)
    is_code = launcher_path.parent.name == "raven-code"
    root = render.product_acp_home(launcher.PRODUCT).resolve() if is_code else launcher.state_root()
    root.mkdir(parents=True, exist_ok=True)
    stale = root / ".config.rendered.999999.json"
    stale.write_text("{}", encoding="utf-8")

    rendered = (
        launcher.render_acp_config(launcher.DEFAULT_CONFIG)
        if is_code
        else launcher.render_config(launcher.DEFAULT_CONFIG)
    )

    assert not stale.exists()
    assert rendered.parent == root
    assert rendered.name == f".config.rendered.{os.getpid()}.json"
    assert load_config(rendered).get_api_key() == "synthetic-host-key"


@pytest.mark.parametrize("launcher", LAUNCHERS, ids=lambda p: p.parent.name)
def test_every_launcher_writes_its_render_through_write_rendered(launcher):
    """The host's refusals are merged in ``write_rendered``, so a launcher
    that wrote its render any other way would ship a product without them."""
    source = launcher.read_text(encoding="utf-8")
    assert "render.write_rendered(" in source
    assert ".config.rendered" not in source


# --- plugin opt-outs: what the host switched off stays off in its products ----


def test_opt_outs_carry_the_hosts_list_after_the_products_own():
    config = {"plugins": {"disabled": ["mine"], "config": {"x": {}}}}
    host = {"plugins": {"disabled": ["everme-memory", "ppt-engine", "mine", 3, "everme-memory"]}}

    carried = render.inherit_plugin_opt_outs(config, host, own=("ppt-engine",))

    assert carried == ["everme-memory"]
    assert config["plugins"] == {"disabled": ["mine", "everme-memory"], "config": {"x": {}}}


def test_opt_outs_leave_a_config_alone_when_the_host_has_none_to_lend():
    for host in ({}, {"plugins": None}, {"plugins": {"disabled": "everme-memory"}}, {"plugins": {"disabled": []}}):
        config: dict = {}
        assert render.inherit_plugin_opt_outs(config, host) == []
        assert config == {}


def test_opt_outs_never_carry_the_products_own_engine():
    config: dict = {}
    assert (
        render.inherit_plugin_opt_outs(config, {"plugins": {"disabled": ["design-engine"]}}, own=("design-engine",))
        == []
    )
    assert config == {}


# --- the memory backend: the store is the installation's, the name is the product's


def _product(backend="everos", slice_=None, distribution="everos-memory"):
    """A product config shaped like the five under agents/."""
    config = {"memory": {"backend": backend, "userId": "raven-x", "agentId": "raven-x"}}
    if slice_ is not None:
        config["plugins"] = {"config": {distribution: dict(slice_)}}
    return config


def test_the_hosts_backend_and_address_replace_the_products():
    config = _product(slice_={"user_id": "raven-x", "agent_id": "raven-x"})
    host = {
        "memory": {"backend": "everos", "userId": "default", "agentId": "default"},
        "plugins": {"config": {"everos-memory": {"base_url": "http://localhost:18962", "port": 18962}}},
    }

    assert render.inherit_memory_backend(config, host) == "everos"

    assert config["plugins"]["config"]["everos-memory"] == {
        "user_id": "raven-x",
        "agent_id": "raven-x",
        "base_url": "http://localhost:18962",
        "owned": False,
    }
    # Identity is the product's in both spellings; port is not a key the
    # product reads, so it does not travel either.
    assert config["memory"] == {"backend": "everos", "userId": "raven-x", "agentId": "raven-x"}


def test_a_product_follows_the_host_onto_the_cloud_backend():
    config = _product()
    host = {
        "memory": {"backend": "everos-cloud"},
        "plugins": {"config": {"everos-cloud-memory": {"base_url": "https://api.example", "api_key": "host-key"}}},
    }

    assert render.inherit_memory_backend(config, host) == "everos-cloud"

    assert config["memory"]["backend"] == "everos-cloud"
    assert config["plugins"]["config"]["everos-cloud-memory"] == {
        "base_url": "https://api.example",
        "api_key": "host-key",
    }
    # The local slice is not the one in force, so nothing was written into it.
    assert "everos-memory" not in config["plugins"]["config"]


def test_a_credential_the_product_holds_itself_is_not_replaced():
    config = _product(backend="everos-cloud", slice_={"api_key": "mine"}, distribution="everos-cloud-memory")
    host = {
        "memory": {"backend": "everos-cloud"},
        "plugins": {"config": {"everos-cloud-memory": {"base_url": "https://api.example", "api_key": "host-key"}}},
    }

    render.inherit_memory_backend(config, host)

    slice_ = config["plugins"]["config"]["everos-cloud-memory"]
    assert slice_["api_key"] == "mine"
    assert slice_["base_url"] == "https://api.example"


def test_the_product_disclaims_the_servers_ownership_even_with_nothing_to_inherit():
    """The hole this closes: a slice naming no root reads as raven's own, so a
    product answering a host that is merely not serving would start a second
    server against a root of its own."""
    for host in ({}, {"memory": {"backend": "everos"}}, {"plugins": {"config": {}}}):
        config = _product()
        assert render.inherit_memory_backend(config, host) == "everos"
        assert config["plugins"]["config"]["everos-memory"] == {"owned": False}


def test_the_hosts_off_switch_travels_and_an_absent_key_does_not():
    """``memory.backend: null`` is how an operator turns long-term memory off,
    and a product that kept writing would be the one surface that ignored them.
    An absent key is the other answer and must not read as the same thing."""
    off = _product()
    assert render.inherit_memory_backend(off, {"memory": {"backend": None}}) is None
    assert off["memory"]["backend"] is None
    assert "plugins" not in off

    absent = _product()
    assert render.inherit_memory_backend(absent, {"memory": {}}) == "everos"
    assert absent["memory"]["backend"] == "everos"


def test_a_host_that_names_no_backend_leaves_the_products_own_standing():
    config = _product(backend="everos-cloud", slice_={"api_key": "mine"}, distribution="everos-cloud-memory")

    assert render.inherit_memory_backend(config, {"memory": {}}) == "everos-cloud"

    assert config["memory"]["backend"] == "everos-cloud"
    assert config["plugins"]["config"]["everos-cloud-memory"] == {"api_key": "mine"}


def test_a_backend_no_distribution_is_known_for_is_named_but_not_furnished():
    config: dict = {}

    assert render.inherit_memory_backend(config, {"memory": {"backend": "someone-elses"}}) == "someone-elses"

    assert config == {"memory": {"backend": "someone-elses"}}


def test_neither_side_naming_a_backend_leaves_the_config_alone():
    config: dict = {}
    assert render.inherit_memory_backend(config, {}) is None
    assert config == {}


def test_write_rendered_carries_the_hosts_memory_backend(homed, tmp_path):
    (homed / "config.json").write_text(
        json.dumps(
            {
                "memory": {"backend": "everos-cloud"},
                "plugins": {"config": {"everos-cloud-memory": {"base_url": "https://api.example"}}},
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    out.mkdir()

    rendered = render.write_rendered(_product(), out)

    data = json.loads(rendered.read_text())
    assert data["memory"] == {"backend": "everos-cloud", "userId": "raven-x", "agentId": "raven-x"}
    assert data["plugins"]["config"]["everos-cloud-memory"] == {"base_url": "https://api.example"}


def test_write_rendered_carries_the_hosts_opt_outs(homed, tmp_path):
    (homed / "config.json").write_text(
        json.dumps({"plugins": {"disabled": ["everme-memory", "research-flow"]}}), encoding="utf-8"
    )
    out = tmp_path / "out"
    out.mkdir()

    rendered = render.write_rendered({"plugins": {"dirs": ["/p"]}}, out, own_plugins=("research-flow",))

    assert json.loads(rendered.read_text())["plugins"] == {"dirs": ["/p"], "disabled": ["everme-memory"]}


def test_every_launcher_names_its_engine_when_it_writes_the_render():
    """A launcher that names no engine inherits every opt-out, its own included,
    so a host that switched the product's engine off for itself would start the
    product without it. Each launcher's render call is read here, the scaffold
    a new product is copied from included."""
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    launchers = sorted((repo / "agents").glob("*/run.py")) + [repo / "raven/templates/agents_scaffold/run.py"]
    assert len(launchers) >= 6
    for path in launchers:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {
            node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith("_PLUGIN_ID")
            and isinstance(node.value, ast.Constant)
        }
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write_rendered"
        ]
        assert len(calls) == 1, path
        [keyword] = [k for k in calls[0].keywords if k.arg == "own_plugins"] or [None]
        assert keyword is not None, f"{path} writes its render without naming its engine"
        named = {constants[e.id] for e in keyword.value.elts if isinstance(e, ast.Name) and e.id in constants}
        assert named == set(constants.values()) and named, path
