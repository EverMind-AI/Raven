"""The self-configuration catalog against the schema, the live readers and the writers."""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path

import pytest

from raven.config import self_surface as surface
from raven.config.self_surface import Effect

# Where each next-turn claim is honoured: the module and the name that re-reads
# the setting while the process serves. A new NEXT_TURN entry must add its
# evidence here, or say in the catalog that it is not next-turn.
_NEXT_TURN_READERS: dict[str, tuple[str, str]] = {
    "agents.defaults.reasoningEffort": ("raven.config.live", "reasoning_effort"),
    "agents.defaults.maxToolIterations": ("raven.config.live", "max_tool_iterations"),
    "agents.defaults.contextWindowTokens": ("raven.config.live", "context_window_tokens"),
    "agents.defaults.enablePersonalization": ("raven.config.live", "personalization_enabled"),
    "routing.profile": ("raven.config.live", "routing_profile"),
    "providers.*.apiKey": ("raven.providers.resolving_provider", "ResolvingProvider"),
    "providers.*.apiBase": ("raven.providers.resolving_provider", "ResolvingProvider"),
    "providers.*.models": ("raven.rpc.methods.model", "model_options"),
    "tools.disabledTools": ("raven.config.live", "disabled_tool_names"),
    "tools.exec.timeout": ("raven.config.live", "exec_timeout"),
    "tools.exec.extraDenyPatterns": ("raven.config.live", "exec_extra_deny_patterns"),
    "tools.web.search.provider": ("raven.config.live", "web_providers"),
    "tools.web.fetch.provider": ("raven.config.live", "web_providers"),
    "tools.mcpServers.*.enabled": ("raven.config.live", "mcp_server_configs"),
    "memory.memoryTopK": ("raven.config.live", "memory_top_k"),
    "skillForge.blocklist": ("raven.config.live", "skill_blocklist"),
    "skillForge": ("raven.config.live", "skill_gate_pin"),
    "context": ("raven.config.live", "curator_pin"),
    "playbooks.disabled": ("raven.config.live", "disabled_playbook_names"),
    "tracing.enabled": ("raven.tracing.config", "def enabled"),
    "tracing.previewLen": ("raven.tracing.config", "preview_len"),
    "sessionTitle.enabled": ("raven.rpc.methods.turn", "load_raven_config"),
    "sessions.autoArchiveAfterDays": ("raven.rpc.methods.session", "load_raven_config"),
    "permissions.mode": ("raven.config.live", "permissions_config"),
    "permissions.judgeModel": ("raven.config.live", "permissions_config"),
    "language": ("raven.i18n", "set_language"),
    "session.model": ("raven.agent.loop.wiring", "set_session_binding"),
}


def _next_turn_paths() -> set[str]:
    out = set()
    for s in surface.all_settings():
        if s.effect is not Effect.NEXT_TURN:
            continue
        if s.path.startswith(("tools.web.providers.", "tools.media.")):
            continue
        out.add(s.path)
    return out


def test_every_next_turn_claim_names_a_live_reader():
    assert _next_turn_paths() == set(_NEXT_TURN_READERS)
    for path, (module, name) in _NEXT_TURN_READERS.items():
        source = inspect.getsource(importlib.import_module(module))
        assert name in source, f"{path}: {module} no longer carries {name}"


def test_the_media_and_vendor_key_claims_ride_the_live_readers():
    live = importlib.import_module("raven.config.live")
    assert hasattr(live, "media_tool_config")
    assert hasattr(live, "web_provider_keys")


def test_every_concrete_path_is_a_schema_field():
    missing = []
    for s in surface.all_settings():
        if "*" in s.path or s.kind == "pin" or s.session or s.stored_at:
            continue
        if surface.default_of(s.path) is None and not s.nullable and s.path != "agents.defaults.reasoningEffort":
            missing.append(s.path)
    assert missing == []


def test_paths_are_unique():
    paths = [s.path for s in surface.all_settings()]
    assert len(paths) == len(set(paths))


def test_settings_writer_paths_are_ones_settings_set_accepts():
    from raven.rpc.methods import console

    source = inspect.getsource(console.settings_set)
    for s in surface.all_settings():
        if s.writer != "settings":
            continue
        assert s.path in console._SETTINGS_SIMPLE_KEYS or f'"{s.path}"' in source, s.path


def test_no_catalog_entry_composes_a_command():
    for s in surface.all_settings():
        assert not s.path.endswith((".command", ".env", ".args")), s.path


def test_find_prefers_an_exact_entry_and_binds_wildcards():
    setting, bound = surface.find("tools.web.providers.jina.apiKey")
    assert setting.path == "tools.web.providers.jina.apiKey" and bound == []
    setting, bound = surface.find("providers.openrouter.apiBase")
    assert setting.path == "providers.*.apiBase" and bound == ["openrouter"]
    assert surface.find("providers.openrouter") is None


@pytest.mark.parametrize(
    ("path", "value", "ok"),
    [
        ("tools.exec.timeout", 30, True),
        ("tools.exec.timeout", 2, False),
        ("tools.exec.timeout", "30", False),
        ("tools.exec.timeout", 30.5, False),
        ("tools.exec.timeout", True, False),
        ("routing.profile", "eco", True),
        ("routing.profile", "cheap", False),
        ("tools.disabledTools", ["exec"], True),
        ("tools.disabledTools", "exec", False),
        ("agents.defaults.contextWindowTokens", None, True),
        ("tools.exec.timeout", None, False),
        ("agents.defaults.model", {"provider": "openrouter", "model": "x/y"}, True),
        ("agents.defaults.model", {"provider": "openrouter"}, False),
    ],
)
def test_check_value(path, value, ok):
    setting, _ = surface.find(path)
    if ok:
        surface.check_value(setting, value)
    else:
        with pytest.raises(ValueError):
            surface.check_value(setting, value)


def test_change_line_states_the_effect_and_the_risk():
    line = surface.change_line({"action": "set", "path": "permissions.mode", "value": "full"})
    assert "permissions.mode" in line and "full" in line
    assert "next turn" in line
    assert "without asking" in line
    assert "Reload" in surface.change_line({"action": "restart", "value": "reload"})
    assert "whole Raven process" in surface.change_line({"action": "restart", "value": '"restart"'})


def _home(tmp_path: Path, monkeypatch, data: dict) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps(data))
    monkeypatch.setenv("RAVEN_HOME", str(home))
    return home / "config.json"


def test_write_keeps_the_spelling_already_in_the_file(tmp_path, monkeypatch):
    path = _home(tmp_path, monkeypatch, {"tools": {"exec": {"timeout": 60}}, "agents": {"defaults": {}}})
    previous = surface.write_value("tools.exec.timeout", 90)
    surface.write_value("agents.defaults.maxToolIterations", 12)
    data = json.loads(path.read_text())
    assert previous == 60
    assert data["tools"]["exec"] == {"timeout": 90}
    assert data["agents"]["defaults"] == {"maxToolIterations": 12}


def test_write_follows_a_snake_case_file(tmp_path, monkeypatch):
    path = _home(tmp_path, monkeypatch, {"agents": {"defaults": {"max_tool_iterations": 5}}})
    surface.write_value("agents.defaults.maxToolIterations", 7)
    assert json.loads(path.read_text())["agents"]["defaults"] == {"max_tool_iterations": 7}


def test_a_write_the_schema_rejects_is_refused_and_nothing_changes(tmp_path, monkeypatch):
    path = _home(tmp_path, monkeypatch, {"tools": {"exec": {"timeout": 60}}})
    before = path.read_text()
    with pytest.raises(ValueError, match="would not load"):
        surface.write_value("tools.exec.timeout", "soon")
    assert path.read_text() == before


def test_a_file_that_was_already_broken_does_not_block_a_write(tmp_path, monkeypatch):
    path = _home(tmp_path, monkeypatch, {"bogusTopLevel": 1})
    surface.write_value("tools.exec.timeout", 45)
    assert json.loads(path.read_text())["tools"]["exec"]["timeout"] == 45


def test_remove_restores_the_default(tmp_path, monkeypatch):
    path = _home(tmp_path, monkeypatch, {"tools": {"exec": {"timeout": 90}}})
    assert surface.remove_value("tools.exec.timeout") == 90
    assert json.loads(path.read_text())["tools"]["exec"] == {}
    assert surface.remove_value("tools.exec.timeout") is None


def test_extension_blocks_validate_too(tmp_path, monkeypatch):
    path = _home(tmp_path, monkeypatch, {})
    surface.write_value("tracing.enabled", False)
    assert json.loads(path.read_text()) == {"tracing": {"enabled": False}}
    with pytest.raises(ValueError, match="would not load"):
        surface.write_value("sentinel.nudgePolicy.maxNudgesPerHour", "many")


@pytest.mark.parametrize(
    "path", ["tools.web.proxy", "tools.media.proxy", "providers.openai.apiBase", "permissions.judgeModel"]
)
def test_a_setting_that_redirects_keyed_traffic_stays_with_the_user(path):
    """Smart mode's reviewer settles anything not marked sensitive, and each of
    these sends Raven's keys and traffic to a host the call names."""
    assert surface.touches_sensitive({"action": "set", "path": path, "value": "http://127.0.0.1:9"})


@pytest.mark.parametrize("spelled", [" {}", "{} ", "{}.", ".{}", " {}. "])
def test_the_gate_classifies_the_path_the_tool_writes(spelled):
    """The tool trims spaces and dots before it writes; classifying the raw
    argument let one trailing space turn a secret into an ordinary setting."""
    from raven.permissions.rules import self_config_tier

    sensitive = {"action": "set", "path": spelled.format("tools.restrictToWorkspace"), "value": False}
    assert surface.touches_sensitive(sensitive)
    secret = {"action": "set", "path": spelled.format("channels.telegram.token"), "value": "123:PLAINTEXT"}
    assert self_config_tier("raven_config", secret).value == "deny"
    assert "PLAINTEXT" not in surface.change_line(secret)


def test_a_batch_names_its_settings_the_way_the_tool_writes_them():
    assert surface.touches_sensitive({"action": "set", "path": " ", "value": {"tools.restrictToWorkspace": False}})
    assert surface.touches_sensitive({"action": "set", "value": {"tools.restrictToWorkspace ": False}})


def test_every_field_a_channel_declares_secret_is_secret_to_the_gate():
    """The adapter's spec decides; Feishu's encrypt_key names no credential marker."""
    from pydantic.alias_generators import to_camel

    from raven.config.update_channels import channel_field_specs, channel_names

    declared = [
        f"channels.{name}.{to_camel(field)}"
        for name in channel_names()
        for field, spec in channel_field_specs(name).items()
        if spec.get("is_secret")
    ]
    assert "channels.feishu.encryptKey" in declared
    assert [path for path in declared if not surface.is_secret_path(path)] == []
    assert not surface.is_secret_path("channels.feishu.appId")
    assert not surface.is_secret_path("channels.nosuchchannel.encryptKey")


def test_a_channel_secret_inside_an_object_is_refused_and_never_shown():
    from raven.permissions.rules import self_config_tier

    whole = {"action": "set", "path": "channels.feishu", "value": {"encryptKey": "FEISHU-PLAINTEXT", "appId": "cli_1"}}
    assert self_config_tier("raven_config", whole).value == "deny"
    shown = surface.change_line(whole)
    assert "FEISHU-PLAINTEXT" not in shown and "cli_1" in shown


def _wrappings(core: str) -> list[str]:
    import itertools

    ends = ["".join(chars) for n in range(3) for chars in itertools.product(" \t.", repeat=n)]
    return [f"{before}{core}{after}" for before in ends for after in ends]


def test_the_path_spelling_settles_in_one_pass_whatever_wraps_it():
    """Trimming spaces then dots left ``token .`` as ``token `` -- a secret read as
    an ordinary setting -- so every order of them must come off at once."""
    from raven.permissions.rules import self_config_tier

    for spelled in _wrappings("channels.telegram.token"):
        assert surface.canonical_path(spelled) == "channels.telegram.token", repr(spelled)
        call = {"action": "set", "path": spelled, "value": "123:PLAINTEXT"}
        assert self_config_tier("raven_config", call).value == "deny", repr(spelled)
        assert "PLAINTEXT" not in surface.change_line(call), repr(spelled)
    for spelled in _wrappings("tools.restrictToWorkspace"):
        assert surface.touches_sensitive({"action": "set", "path": spelled, "value": False}), repr(spelled)


def test_every_field_the_provider_schema_treats_as_secret_is_secret_to_the_gate(monkeypatch, tmp_path):
    """The provider writer redacts by the schema's own marker (``extraHeaders``) and
    its patch list (Gemini's ``apiKeyList``); the gate and the scrub read the same."""
    from pydantic.alias_generators import to_camel

    from raven.config import held_secrets
    from raven.config.schema import GeminiProviderConfig, ProviderConfig
    from raven.config.update_providers import _is_secret_field

    declared = [
        name
        for model in (ProviderConfig, GeminiProviderConfig)
        for name, info in model.model_fields.items()
        if _is_secret_field(name, info)
    ]
    assert {"extra_headers", "api_key_list"} <= set(declared)
    assert [n for n in declared if not surface.is_secret_path(f"providers.gemini.{to_camel(n)}")] == []
    assert not surface.is_secret_path("providers.gemini.models")

    config = tmp_path / "config.json"
    header, listed = "hdr-0123456789abcdef", "AIza-listed-0123456789"
    config.write_text(
        json.dumps(
            {
                "providers": {
                    "aihubmix": {"extraHeaders": {"APP-Code": header}},
                    "gemini": {"apiKeyList": [listed]},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(held_secrets, "get_config_path", lambda: config)
    monkeypatch.setattr(held_secrets, "_cache", None)
    scrubbed = held_secrets.scrub_held_secrets(f"APP-Code: {header}; key {listed}")
    assert header not in scrubbed and listed not in scrubbed


def test_a_channel_field_that_sends_its_traffic_somewhere_stays_with_the_user():
    """A proxy or server address carries the channel's credential to whatever host
    it names; the adapter declares it, the way it declares a secret."""
    import re

    from pydantic.alias_generators import to_camel

    from raven.config.update_channels import channel_field_specs, channel_names

    addresses = [
        f"channels.{name}.{to_camel(field)}"
        for name in channel_names()
        for field in channel_field_specs(name)
        if re.search(r"(url|host|proxy|homeserver)$", field)
    ]
    assert "channels.telegram.proxy" in addresses and "channels.matrix.homeserver" in addresses
    unmarked = [p for p in addresses if not surface.touches_sensitive({"action": "set", "path": p, "value": "x"})]
    assert unmarked == [], "declare these sensitive in the adapter's spec"
    line = surface.change_line({"action": "set", "path": "channels.telegram.proxy", "value": "http://h:1"})
    assert "Note: sends this channel's credentials" in line
    assert not surface.touches_sensitive({"action": "set", "path": "channels.telegram.replyToMessage", "value": True})
