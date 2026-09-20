"""Tests for ``subagents.update``'s model and factory-description handling.

Split out of ``test_rpc_subagents.py`` (per the plan) rather than added to it:
these exercise the acp-menu / host-catalogue validation and the broadened
description reset, none of which the existing file's fixture or helpers touch.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.rpc.errors import ConfigFieldReadonlyError, ConfigValidationError
from raven.rpc.methods.subagents import subagents_list, subagents_update


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config file the handlers write to instead of the real ``~/.raven``."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "subagents": {
                    "agents": [
                        {
                            "name": "Hermes Agent",
                            "preset": "hermes",
                            "kind": "acp",
                            "command": "hermes acp --accept-hooks",
                            "description": "custom desc",
                            "enabled": True,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
    monkeypatch.setattr("raven.rpc.methods.subagents.get_config_path", lambda: path)
    return path


def _stored(path: Path) -> list[dict]:
    return json.loads(path.read_text())["subagents"]["agents"]


def _fake_agent_meta(choices: tuple[str, ...]):
    """A stand-in for ``agent_meta`` reporting a fixed acp model menu.

    Real ``agent_meta`` reads a capability snapshot off disk; these tests are
    about the update handler's own validation, not about the snapshot store,
    so the menu it reads from is nailed down instead of measured.
    """

    def fake(cfg, *, snapshot=None):
        if getattr(cfg, "kind", None) != "acp":
            return SimpleNamespace(model_choices=())
        return SimpleNamespace(model_choices=tuple(SimpleNamespace(value=v) for v in choices))

    return fake


async def test_update_accepts_a_model_the_acp_row_advertises(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a", "vendor/b")))
    out = await subagents_update({"name": "Hermes Agent", "model": "vendor/a"})
    assert out == {"updated": True, "name": "Hermes Agent"}
    entry = next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")
    assert entry["model"] == "vendor/a"


async def test_update_rejects_a_model_the_acp_row_does_not_advertise(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a",)))
    with pytest.raises(ConfigValidationError, match="offers 1"):
        await subagents_update({"name": "Hermes Agent", "model": "vendor/bogus"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")
    assert "model" not in entry or entry["model"] is None


async def test_update_rejects_any_model_when_the_acp_row_advertises_none(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(()))
    with pytest.raises(ConfigValidationError, match="offers none"):
        await subagents_update({"name": "Hermes Agent", "model": "vendor/a"})


def _with_providers(config_path: Path, providers: dict) -> None:
    raw = json.loads(config_path.read_text())
    raw["providers"] = providers
    config_path.write_text(json.dumps(raw), encoding="utf-8")


async def test_update_materializes_a_builtin_override_and_checks_its_model_against_ravens_providers(
    config_path: Path,
) -> None:
    """``Raven`` (the generic built-in agent) has no config row until its first
    edit; setting its model must both create the override row and validate the
    value against raven's own providers, not against any agent menu: the id
    names a provider raven knows, and that provider holds a credential."""
    _with_providers(config_path, {"openai": {"apiKey": "sk-test"}})

    out = await subagents_update({"name": "Raven", "model": "openai/gpt-5"})
    assert out == {"updated": True, "name": "Raven"}
    entry = next(e for e in _stored(config_path) if e["name"] == "Raven")
    assert entry["kind"] == "builtin"
    assert entry["model"] == "openai/gpt-5"


async def test_update_accepts_a_builtin_model_a_configured_section_lists_by_hand(config_path: Path) -> None:
    """A passthrough vendor no spec matches is reachable only through the model
    list its section carries, so that list is the other thing the check reads --
    and the id is stored naming the section, which is what the pool binds."""
    _with_providers(
        config_path, {"custom": {"apiBase": "http://127.0.0.1:8000/v1", "apiKey": "k", "models": ["my-local-model"]}}
    )

    await subagents_update({"name": "Raven", "model": "my-local-model"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Raven")
    assert entry["model"] == "custom/my-local-model"


async def test_update_rejects_a_builtin_model_no_provider_of_ravens_serves(config_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="none of them can serve 'nonsense-model-xyz'"):
        await subagents_update({"name": "Raven", "model": "nonsense-model-xyz"})
    assert not any(e["name"] == "Raven" for e in _stored(config_path)), (
        "a rejected write must not leave a half-made override behind"
    )


async def test_update_rejects_a_builtin_model_whose_provider_holds_no_credential(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recognising the id is not enough: the dispatch pairs it with that
    provider's credential (``ProviderPool.bind_pin``) and drops the pin when
    there is none, so a write that passed here would run on the conversation's
    model with one log line to say so. Refused instead."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConfigValidationError, match="no usable credentials"):
        await subagents_update({"name": "Raven", "model": "openai/gpt-5"})


async def test_update_stores_a_builtin_model_naming_the_provider_it_was_picked_under(config_path: Path) -> None:
    """A bare id is claimed by keyword matching at dispatch, which sends it
    wherever those rules land; the pair the reader picked is kept by spelling
    the provider into the stored id, the way ``config.set model`` stores the
    host's."""
    _with_providers(config_path, {"openrouter": {"apiKey": "sk-or-test"}})

    await subagents_update({"name": "Raven", "model": "gpt-5", "provider": "openrouter"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Raven")
    assert entry["model"] == "openrouter/gpt-5"


async def test_update_edits_a_builtin_override_stored_under_the_legacy_spelling_in_place(config_path: Path) -> None:
    """An install that wrote its override before the generic row was renamed
    holds it as ``raven``; the page addresses the merged row as ``Raven``. The
    edit has to land on that row -- a second one would be dropped by the merge,
    taking the reader's model with it."""
    _with_providers(config_path, {"openai": {"apiKey": "sk-test"}})
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append({"name": "raven", "kind": "builtin", "skills": ["one", "two"]})
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    await subagents_update({"name": "Raven", "model": "openai/gpt-5"})

    builtin = [e for e in _stored(config_path) if e.get("kind") == "builtin"]
    assert [e["name"] for e in builtin] == ["raven"]
    assert builtin[0]["skills"] == ["one", "two"]
    assert builtin[0]["model"] == "openai/gpt-5"


async def test_update_edits_a_legacy_spelled_acp_transport_override_in_place(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An acp row of a seed's name is the supported transport switch, and the
    table lists it under the canonical name; the edit must land on it, by its
    own rule (its advertised menu), not materialize a builtin row beside it that
    the merge would then drop the transport for."""
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append({"name": "raven", "kind": "acp", "command": ""})
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a",)))

    await subagents_update({"name": "Raven", "model": "vendor/a"})

    of_name = [e for e in _stored(config_path) if e["name"].lower() == "raven"]
    assert [(e["name"], e["kind"], e.get("model")) for e in of_name] == [("raven", "acp", "vendor/a")]


async def test_update_refuses_a_builtin_name_config_holds_an_ignored_row_for(config_path: Path) -> None:
    """A cli row of a seed's name is one the table ignores and the list leaves
    out, showing the seed instead. Editing the seed would write a second row of
    the name; editing the cli row would change nothing anyone sees. Refused,
    naming the row to remove."""
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append({"name": "Raven", "kind": "cli", "command": "echo {prompt}"})
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigFieldReadonlyError, match="'cli' row of that name"):
        await subagents_update({"name": "Raven", "model": "openai/gpt-5"})
    assert [e["kind"] for e in _stored(config_path) if e["name"] == "Raven"] == ["cli"]


async def test_update_refuses_to_rename_a_materialized_builtin_override(config_path: Path) -> None:
    """The row is bound to the seed by name; renamed, it would be a second
    built-in agent with nothing behind it."""
    _with_providers(config_path, {"openai": {"apiKey": "sk-test"}})
    with pytest.raises(ConfigFieldReadonlyError):
        await subagents_update({"name": "Raven", "new_name": "Scribe", "model": "openai/gpt-5"})
    assert not any(e.get("kind") == "builtin" for e in _stored(config_path))


async def test_update_clear_model_wins_over_a_model_sent_beside_it(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a",)))
    await subagents_update({"name": "Hermes Agent", "model": "vendor/a"})
    await subagents_update({"name": "Hermes Agent", "model": "vendor/a", "clear_model": True})
    entry = next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")
    assert entry.get("model") is None


async def test_update_refuses_to_clear_the_model_of_a_row_with_no_menu(config_path: Path) -> None:
    """An openai row's model is a required config value and a cli row has none:
    clearing meets the same refusal setting does, not a validation error from a
    field nulled underneath the schema."""
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append(
        {"name": "Researcher", "kind": "openai", "baseUrl": "https://api.example.test/v1", "model": "m", "apiKey": "k"}
    )
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigFieldReadonlyError):
        await subagents_update({"name": "Researcher", "clear_model": True})
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["model"] == "m"


async def test_update_clearing_the_description_with_explicit_null_reverts_to_the_preset_default(
    config_path: Path,
) -> None:
    """``description: null`` (an explicit key, not merely absent) must reset to
    the factory text on the same terms a blank string already does."""
    from raven.agent.subagent.presets import third_party_subagent_preset

    out = await subagents_update({"name": "Hermes Agent", "description": None})
    assert out == {"updated": True, "name": "Hermes Agent"}
    entry = next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")
    assert entry["description"] == third_party_subagent_preset("hermes")["description"]
    assert entry["description"] != "custom desc"


async def test_update_clearing_the_description_with_explicit_null_puts_a_builtin_override_back_on_the_seed(
    config_path: Path,
) -> None:
    """A built-in row's ``description: null`` clears the override rather than
    copying today's seed text in: ``""`` is the schema's own "the seed's", so
    the row reads the package text now and keeps following it when a release
    changes it -- a copy would have frozen this one."""
    from raven.agent.subagent.builtin_agents import builtin_agent_seeds

    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append(
        {"name": "Raven", "kind": "builtin", "description": "a retuned blurb", "enabled": True}
    )
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    await subagents_update({"name": "Raven", "description": None})

    entry = next(e for e in _stored(config_path) if e["name"] == "Raven")
    assert entry["description"] == ""
    seed = next(s for s in builtin_agent_seeds() if s.name == "Raven")
    listed = next(r for r in (await subagents_list({"probe": False}))["rows"] if r["name"] == "Raven")
    assert listed["description"] == seed.description
