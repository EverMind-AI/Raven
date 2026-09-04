"""Tests for ``raven.config.loader.load_config``.

Covers the migrations that drop / relocate retired blocks from old
configs, plus the default-config fallback path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.config.loader import load_config


def _write(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body), encoding="utf-8")


def test_missing_file_uses_defaults(tmp_path: Path) -> None:
    """No file → default Config — loader must not raise."""
    cfg = load_config(tmp_path / "does_not_exist.json")
    # AgentDefaults no longer carries the everos field;
    # check a stable default instead.
    assert cfg.agents.defaults.max_tool_iterations == 40


def test_legacy_everos_block_silently_dropped(tmp_path: Path) -> None:
    """Old configs may still carry ``agents.defaults.everos``. The
    migration strips it so model_validate doesn't reject the file."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "agents": {
                "defaults": {
                    "everos": {"enabled": True, "enableSkill": True},
                },
            },
        },
    )
    cfg = load_config(p)
    assert not hasattr(cfg.agents.defaults, "everos")


def test_legacy_everos_skill_light_relocated_under_agents_defaults(
    tmp_path: Path,
) -> None:
    """Old configs put ``everosSkillLight`` under ``agents.defaults``.
    The migration removes it from that location (the new home is under
    ``skillForge.everos``; see test_config_raven_loader for the
    receiving side)."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "agents": {
                "defaults": {
                    "everosSkillLight": {"enabled": True},
                },
            },
        },
    )
    cfg = load_config(p)
    assert not hasattr(cfg.agents.defaults, "everosSkillLight")
    assert not hasattr(cfg.agents.defaults, "everos_skill_light")


def test_legacy_everos_skill_light_retired_keys_stripped() -> None:
    """everosSkillLight carrying the retired minMessages/minToolCalls must
    relocate to skillForge.everos with those keys dropped (EverOSConfig is
    extra='forbid'), while the surviving fields are kept."""
    from raven.config.loader import _migrate_config

    out = _migrate_config(
        {
            "agents": {
                "defaults": {
                    "everosSkillLight": {
                        "enabled": True,
                        "minMessages": 4,
                        "minToolCalls": 2,
                        "maxSkillsTopK": 5,
                    },
                },
            },
        },
        pop_extension_keys=False,
    )
    everos = out["skillForge"]["everos"]
    assert "minMessages" not in everos
    assert "minToolCalls" not in everos
    assert everos["maxSkillsTopK"] == 5
    assert everos["enabled"] is True


def test_legacy_everos_skill_light_retired_keys_stripped_snake_case() -> None:
    """snake_case variant (min_messages / min_tool_calls) is stripped too."""
    from raven.config.loader import _migrate_config

    out = _migrate_config(
        {
            "agents": {
                "defaults": {
                    "everos_skill_light": {
                        "min_messages": 4,
                        "min_tool_calls": 2,
                        "enabled": False,
                    },
                },
            },
        },
        pop_extension_keys=False,
    )
    everos = out["skillForge"]["everos"]
    assert "min_messages" not in everos
    assert "min_tool_calls" not in everos


def test_corrupted_json_falls_back_to_defaults(tmp_path: Path) -> None:
    """A mid-write race can leave the file half-flushed; tolerate it."""
    p = tmp_path / "config.json"
    p.write_text("{this is not json", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.agents.defaults.max_tool_iterations == 40


def test_schema_validation_error_raises(tmp_path: Path) -> None:
    """A user / programmer config error must NOT silently fall back to
    defaults — that masks misconfig as "feature X did nothing"."""
    p = tmp_path / "config.json"
    # ``max_tool_iterations`` is an int — pass a string to force a
    # pydantic ValidationError, which is a ValueError subclass we
    # explicitly re-raise rather than swallow.
    _write(
        p,
        {
            "agents": {"defaults": {"max_tool_iterations": "not-an-int"}},
        },
    )
    with pytest.raises(ValueError, match="schema validation"):
        load_config(p)


def test_read_raw_or_raise_absent_returns_empty(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    assert read_raw_or_raise(tmp_path / "nope.json") == {}


def test_read_raw_or_raise_valid(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    p = tmp_path / "c.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    assert read_raw_or_raise(p) == {"a": 1}


def test_read_raw_or_raise_malformed_raises(tmp_path: Path) -> None:
    from raven.config.loader import ConfigReadError, read_raw_or_raise

    p = tmp_path / "bad.json"
    p.write_text("{  // comment\n}", encoding="utf-8")
    with pytest.raises(ConfigReadError):
        read_raw_or_raise(p)


def test_load_config_malformed_warns_loudly_and_uses_defaults(tmp_path: Path, capsys) -> None:
    from raven.config.loader import load_config
    from raven.config.schema import Config

    p = tmp_path / "bad.json"
    p.write_text("{  // comment\n}", encoding="utf-8")
    cfg = load_config(p)  # must NOT raise (boot resilience)
    assert isinstance(cfg, Config)
    assert "IGNORING" in capsys.readouterr().err  # loud stderr warning, not silent


def test_read_raw_or_raise_empty_file_is_empty_dict(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    p = tmp_path / "empty.json"
    p.write_text("   \n", encoding="utf-8")
    assert read_raw_or_raise(p) == {}  # empty = no data to lose, not malformed


def test_read_raw_or_raise_json_null_is_empty_dict(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    p = tmp_path / "null.json"
    p.write_text("null", encoding="utf-8")
    assert read_raw_or_raise(p) == {}  # valid JSON but not an object -> {} (no AttributeError)


def test_config_read_error_is_not_runtimeerror() -> None:
    # Intentional: the CLI write commands wrap ops in `except RuntimeError`
    # (OAuth-refusal etc.); ConfigReadError must NOT be a RuntimeError so a parse
    # error bypasses those and reaches the single run() handler. Do not "fix"
    # this to RuntimeError.
    from raven.config.loader import ConfigReadError

    assert not issubclass(ConfigReadError, RuntimeError)
    assert issubclass(ConfigReadError, Exception)


def test_warn_unknown_config_keys_flags_silent_typo() -> None:
    # Regression for the A/B mis-config: ``maxIterations`` is not a field of
    # AgentDefaults (correct: ``maxToolIterations``) and used to be silently
    # dropped, leaving one arm on the 40-iteration default.
    from raven.config.loader import warn_unknown_config_keys

    data = {"agents": {"defaults": {"maxIterations": 16, "model": "m"}}}
    assert warn_unknown_config_keys(data) == ["agents.defaults.maxIterations"]


def test_warn_unknown_config_keys_accepts_both_naming_styles() -> None:
    from raven.config.loader import warn_unknown_config_keys

    data = {
        "agents": {"defaults": {"maxToolIterations": 16, "max_tool_iterations": 16, "requestTimeoutSeconds": 300}},
        "tools": {
            "web": {
                "search": {"provider": "serper", "maxResults": 5},
                "providers": {"serper": {"apiKey": "k"}, "jina": {"api_key": "j"}},
            }
        },
    }
    assert warn_unknown_config_keys(data) == []


def test_legacy_web_keys_move_to_their_vendor() -> None:
    # The per-tool key fields became per-vendor ones: one AnySearch account
    # serves both web tools, so a key held per tool had to be pasted twice.
    # The host raven this agent inherits from is a separate checkout still on
    # the old layout, so its keys keep arriving in the legacy shape.
    from raven.config.loader import _migrate_config, warn_unknown_config_keys

    data = _migrate_config(
        {"tools": {"web": {"jinaApiKey": "j", "search": {"apiKey": "s", "anysearchApiKey": "a"}}}}
    )
    web = data["tools"]["web"]
    assert web["providers"] == {
        "jina": {"apiKey": "j"},
        "serper": {"apiKey": "s"},
        "anysearch": {"apiKey": "a"},
    }
    assert "jinaApiKey" not in web and "apiKey" not in web["search"]
    assert warn_unknown_config_keys(data) == []


def test_a_pre_vendor_config_file_still_reaches_the_tools(tmp_path: Path) -> None:
    # The dict-level assertion above covers the write; this covers the read back,
    # which is the half that decides whether a deploy on the old layout works.
    from raven.agent.tools.web import selected_fetch_key, selected_search_key

    p = tmp_path / "config.json"
    p.write_text(
        json.dumps({"tools": {"web": {"jinaApiKey": "j", "search": {"apiKey": "s", "maxResults": 7}}}}),
        encoding="utf-8",
    )
    web = load_config(p).tools.web

    assert selected_search_key(web) == ("serper", "s")
    assert selected_fetch_key(web) == ("jina", "j")
    assert web.search.max_results == 7


def test_a_hand_migrated_key_wins_over_the_legacy_one() -> None:
    # A config carrying both is being migrated by hand; the new path is the one
    # its author meant, so the legacy value must not overwrite it.
    from raven.config.loader import _migrate_config

    data = _migrate_config(
        {"tools": {"web": {"jinaApiKey": "old", "providers": {"jina": {"apiKey": "new"}}}}}
    )
    assert data["tools"]["web"]["providers"]["jina"] == {"apiKey": "new"}


def test_load_config_warns_but_still_loads_on_unknown_key(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"agents": {"defaults": {"maxIterations": 16}}}), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.agents.defaults.max_tool_iterations == 40  # typo ignored, default kept, but warned


# --------------------------------------------------------------------------- #
# .env seeding                                                                  #
# --------------------------------------------------------------------------- #
#
# The suite disables this globally (tests/conftest.py), so every case here turns
# it back on explicitly. That is deliberate: a test that needed no opt-in would
# be one the default config path could reach, which is the failure the global
# switch exists to prevent.


_SEEDABLE = (
    "SERPER_API_KEY",
    "ANYSEARCH_API_KEY",
    "SERPAPI_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "BRAVE_API_KEY",
    "FIRECRAWL_API_KEY",
    "JINA_API_KEY",
)


@pytest.fixture
def dotenv_on(monkeypatch: pytest.MonkeyPatch):
    """Enable .env seeding and give each case a clean seeded-paths cache.

    The restore is by hand rather than through monkeypatch: what these cases
    exercise writes ``os.environ`` directly, and monkeypatch only undoes its own
    writes - so a seeded key would outlive the test and hand the next one a
    credential it never asked for.
    """
    from raven.config import loader

    monkeypatch.setenv(loader.DOTENV_DISABLE_VAR, "1")
    monkeypatch.setattr(loader, "_DOTENV_SEEDED", set())
    before = {name: os.environ.pop(name, None) for name in _SEEDABLE}
    try:
        yield
    finally:
        for name, value in before.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value


def _config_with_dotenv(tmp_path: Path, dotenv: str, config: dict | None = None) -> Path:
    (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    (tmp_path / ".env").chmod(0o600)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config or {}), encoding="utf-8")
    return path


def test_a_dotenv_beside_the_config_reaches_the_tools(dotenv_on, tmp_path: Path) -> None:
    # The read back, not the write: a key in os.environ that the tool does not
    # resolve is the same as no key. This is the whole point of the feature.
    from raven.agent.tools.web import WebFetchTool, WebSearchTool

    path = _config_with_dotenv(
        tmp_path,
        "# a comment\n\nSERPER_API_KEY=sk-from-dotenv\nJINA_API_KEY='sk-jina-quoted'\n",
    )
    load_config(path)

    assert WebSearchTool().api_key == "sk-from-dotenv"
    assert WebFetchTool().api_key == "sk-jina-quoted"


def test_a_real_export_beats_the_file(dotenv_on, tmp_path: Path, monkeypatch) -> None:
    # Otherwise a stale .env silently overrides the key a caller exported for
    # one run, and the run looks like it used the key it was given.
    monkeypatch.setenv("SERPER_API_KEY", "sk-exported")
    path = _config_with_dotenv(tmp_path, "SERPER_API_KEY=sk-from-dotenv\n")

    load_config(path)

    assert os.environ["SERPER_API_KEY"] == "sk-exported"


def test_the_config_file_still_beats_both(dotenv_on, tmp_path: Path) -> None:
    from raven.agent.tools.web import selected_search_key

    path = _config_with_dotenv(
        tmp_path,
        "SERPER_API_KEY=sk-from-dotenv\n",
        {"tools": {"web": {"providers": {"serper": {"apiKey": "sk-from-config"}}}}},
    )
    web = load_config(path).tools.web

    assert selected_search_key(web) == ("serper", "sk-from-config")


def test_the_anchor_is_the_config_directory_not_the_cwd(
    dotenv_on, tmp_path: Path, monkeypatch
) -> None:
    # Same command launched from another directory must read the same .env, or
    # a run's credentials depend on where the shell happened to be.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("SERPER_API_KEY=sk-wrong\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    path = _config_with_dotenv(home, "SERPER_API_KEY=sk-right\n")
    monkeypatch.chdir(elsewhere)

    load_config(path)

    assert os.environ["SERPER_API_KEY"] == "sk-right"


def test_no_dotenv_is_not_an_error(dotenv_on, tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")

    assert load_config(path).tools.web.search.provider == "serper"


def test_a_world_readable_dotenv_is_reported(dotenv_on, tmp_path: Path) -> None:
    # Through a loguru sink, not caplog/capfd: loguru holds the stderr object it
    # was given at handler-add time, so neither of pytest's captures sees it.
    from loguru import logger

    from raven.config.loader import seed_env_from_dotenv

    (tmp_path / ".env").write_text("SERPER_API_KEY=sk-x\n", encoding="utf-8")
    (tmp_path / ".env").chmod(0o644)

    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(m.record["message"]), level="INFO")
    try:
        seed_env_from_dotenv(tmp_path / "config.json")
    finally:
        logger.remove(sink)

    assert any("chmod 600" in line for line in seen)
    # Names only. A log line that quotes the value defeats the file's purpose.
    assert not any("sk-x" in line for line in seen)


def test_the_file_is_parsed_once_per_process(dotenv_on, tmp_path: Path) -> None:
    # A command that loads its config repeatedly must not resurrect a variable
    # something deliberately removed after the first load.
    from raven.config.loader import seed_env_from_dotenv

    path = _config_with_dotenv(tmp_path, "SERPER_API_KEY=sk-x\n")
    assert seed_env_from_dotenv(path) == ["SERPER_API_KEY"]

    del os.environ["SERPER_API_KEY"]

    assert seed_env_from_dotenv(path) == []
    assert "SERPER_API_KEY" not in os.environ


def test_the_suite_runs_with_seeding_off() -> None:
    # The guard itself, asserted rather than assumed: if conftest stopped
    # setting it, every case above would still pass while the default config
    # path started reading the developer's home.
    from raven.config.loader import DOTENV_DISABLE_VAR

    assert os.environ.get(DOTENV_DISABLE_VAR) == "0"
