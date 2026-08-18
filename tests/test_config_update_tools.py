"""Unit tests for raven.config.update_tools -- the ``tools.*`` write path.

Pins the camelCase-on-disk contract: writes land as ``tools.deepResearch.apiKey``
(not snake_case), matching the rest of config.json.

``tools.web.search`` and ``tools.media.<tool>`` are nested inside a section that
holds unrelated settings of its own, so most of what is asserted below is that a
patch replaces the addressed subtree and nothing above it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.config import update_tools as ut


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    return tmp_path / "config.json"


def _raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_set_writes_camelcase_no_snake_leak(cfg: Path):
    ut.set_deep_research({"api_key": "sk-abc", "model": "mirothinker-1-7-deepresearch-mini"}, config_path=cfg)
    section = _raw(cfg)["tools"]["deepResearch"]
    assert section == {"apiKey": "sk-abc", "apiBase": "", "model": "mirothinker-1-7-deepresearch-mini"}
    assert "api_key" not in section  # no parallel snake_case key


def test_set_merges_and_returns_prev(cfg: Path):
    ut.set_deep_research({"api_key": "sk-1"}, config_path=cfg)
    prev = ut.set_deep_research({"model": "mirothinker-1-7-deepresearch"}, config_path=cfg)
    section = _raw(cfg)["tools"]["deepResearch"]
    assert section["apiKey"] == "sk-1"  # earlier field preserved across a later patch
    assert section["model"] == "mirothinker-1-7-deepresearch"
    assert prev == {"model": ""}


def test_set_rejects_unknown_field(cfg: Path):
    with pytest.raises(KeyError):
        ut.set_deep_research({"bogus": "x"}, config_path=cfg)


def test_set_preserves_sibling_tool_sections(cfg: Path):
    cfg.write_text(json.dumps({"tools": {"web": {"searchProvider": "brave"}}}), encoding="utf-8")
    ut.set_deep_research({"api_key": "sk-abc"}, config_path=cfg)
    tools = _raw(cfg)["tools"]
    assert tools["web"] == {"searchProvider": "brave"}  # untouched
    assert tools["deepResearch"]["apiKey"] == "sk-abc"


def test_set_initializes_on_upgraded_config_without_tools_key(cfg: Path):
    # Upgraded user whose config predates deep_research: no `tools` key at all.
    # `enable` must create tools.deepResearch (not raise KeyError) and leave the
    # rest of the config intact -- so users can opt in without re-running onboard.
    cfg.write_text(json.dumps({"providers": {"openai": {"apiKey": "sk-o"}}}), encoding="utf-8")
    ut.set_deep_research({"api_key": "sk-new"}, config_path=cfg)
    data = _raw(cfg)
    assert data["tools"]["deepResearch"]["apiKey"] == "sk-new"
    assert data["providers"]["openai"]["apiKey"] == "sk-o"  # untouched


def test_get_redacts_key(cfg: Path):
    ut.set_deep_research({"api_key": "sk-secret", "model": "m"}, config_path=cfg)
    got = ut.get_deep_research(redact=True, config_path=cfg)
    assert got["api_key"] == "****set****"
    assert got["model"] == "m"
    assert ut.get_deep_research(redact=False, config_path=cfg)["api_key"] == "sk-secret"


def test_get_empty_when_unset(cfg: Path):
    assert ut.get_deep_research(redact=True, config_path=cfg)["api_key"] == "(empty)"


def test_malformed_config_refuses_write_and_preserves_file(cfg: Path):
    # REGRESSION: a present-but-unparseable config (e.g. // comments, trailing
    # comma) must NEVER be clobbered. set/get/reset raise ConfigReadError and
    # leave the file byte-for-byte intact -- returning {} then writing here once
    # wiped a real config down to just the deepResearch section.
    original = '{\n  "providers": {"openai": {"apiKey": "sk-o"}},\n  // a comment => invalid JSON\n}\n'
    cfg.write_text(original, encoding="utf-8")
    for op in (
        lambda: ut.set_deep_research({"api_key": "sk-x"}, config_path=cfg),
        lambda: ut.get_deep_research(config_path=cfg),
        lambda: ut.reset_deep_research(config_path=cfg),
    ):
        with pytest.raises(ut.ConfigReadError):
            op()
    assert cfg.read_text(encoding="utf-8") == original  # untouched after all three


def test_tolerates_invalid_section(cfg: Path):
    cfg.write_text(json.dumps({"tools": {"deepResearch": {"apiKey": ["not", "a", "string"]}}}), encoding="utf-8")
    # section fails validation -> falls back to defaults instead of crashing
    assert ut.get_deep_research(config_path=cfg)["api_key"] == "(empty)"


def test_reset_clears_key(cfg: Path):
    ut.set_deep_research({"api_key": "sk-abc", "model": "m"}, config_path=cfg)
    ut.reset_deep_research(config_path=cfg)
    section = _raw(cfg)["tools"]["deepResearch"]
    assert section == {"apiKey": "", "apiBase": "", "model": ""}


# ---------------------------------------------------------------------------
# tools.web.search


def test_web_search_set_writes_camelcase_under_search(cfg: Path):
    ut.set_web_search({"api_key": "serper-abc"}, config_path=cfg)
    assert _raw(cfg)["tools"]["web"]["search"] == {"apiKey": "serper-abc", "maxResults": 5}


def test_web_search_set_preserves_the_rest_of_tools_web(cfg: Path):
    # The reason the writer addresses `tools.web.search` and not `tools.web`:
    # jinaApiKey and proxy are the user's, set from somewhere else entirely, and
    # writing back a validated parent section would reset them to defaults.
    cfg.write_text(
        json.dumps({"tools": {"web": {"jinaApiKey": "jina-1", "proxy": "http://127.0.0.1:7890"}}}),
        encoding="utf-8",
    )
    ut.set_web_search({"api_key": "serper-abc"}, config_path=cfg)
    web = _raw(cfg)["tools"]["web"]
    assert web["jinaApiKey"] == "jina-1"
    assert web["proxy"] == "http://127.0.0.1:7890"
    assert web["search"]["apiKey"] == "serper-abc"


def test_web_search_set_merges_and_returns_prev(cfg: Path):
    ut.set_web_search({"api_key": "serper-1"}, config_path=cfg)
    prev = ut.set_web_search({"max_results": 8}, config_path=cfg)
    section = _raw(cfg)["tools"]["web"]["search"]
    assert section == {"apiKey": "serper-1", "maxResults": 8}
    assert prev == {"max_results": 5}


def test_web_search_set_rejects_unknown_field(cfg: Path):
    with pytest.raises(KeyError):
        ut.set_web_search({"apiKey": "camel-is-not-a-field-name"}, config_path=cfg)


def test_web_search_get_redacts_key(cfg: Path):
    assert ut.get_web_search(config_path=cfg) == {"api_key": "(empty)", "max_results": 5}
    ut.set_web_search({"api_key": "serper-abc"}, config_path=cfg)
    assert ut.get_web_search(config_path=cfg)["api_key"] == "****set****"
    assert ut.get_web_search(redact=False, config_path=cfg)["api_key"] == "serper-abc"


# ---------------------------------------------------------------------------
# tools.media.<tool>


def test_media_set_writes_only_its_own_tool(cfg: Path):
    ut.set_media("image", {"model": "google/gemini-2.5-flash-image"}, config_path=cfg)
    media = _raw(cfg)["tools"]["media"]
    assert media["image"] == {"apiKey": "", "apiBase": "", "model": "google/gemini-2.5-flash-image"}
    assert set(media) == {"image"}  # speech/video not materialised by an image write


def test_media_set_preserves_siblings_and_shared_settings(cfg: Path):
    cfg.write_text(
        json.dumps(
            {
                "tools": {
                    "media": {
                        "proxy": "http://127.0.0.1:7890",
                        "outputSubdir": "art",
                        "speech": {"model": "openai/gpt-audio-mini"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    ut.set_media("image", {"api_key": "sk-img"}, config_path=cfg)
    media = _raw(cfg)["tools"]["media"]
    assert media["proxy"] == "http://127.0.0.1:7890"
    assert media["outputSubdir"] == "art"
    assert media["speech"] == {"model": "openai/gpt-audio-mini"}
    assert media["image"]["apiKey"] == "sk-img"


def test_media_rejects_an_unknown_tool(cfg: Path):
    # Not a silent no-op: the caller named a tool that will never register, and
    # writing `tools.media.music` would look like it had worked.
    with pytest.raises(KeyError):
        ut.set_media("music", {"model": "m"}, config_path=cfg)
    with pytest.raises(KeyError):
        ut.get_media("music", config_path=cfg)


def test_media_get_redacts_key_and_reports_the_model(cfg: Path):
    assert ut.get_media("video", config_path=cfg) == {"api_key": "(empty)", "api_base": "", "model": ""}
    ut.set_media("video", {"api_key": "sk-v", "model": "kwaivgi/kling-v3.0-std"}, config_path=cfg)
    got = ut.get_media("video", config_path=cfg)
    assert got == {"api_key": "****set****", "api_base": "", "model": "kwaivgi/kling-v3.0-std"}


def test_tolerates_a_non_mapping_where_a_section_should_be(cfg: Path):
    # A hand-edited file can hold anything. Reads fall back to defaults, and a
    # write replaces the bad node rather than raising out of `setdefault`.
    cfg.write_text(json.dumps({"tools": {"web": "not-a-section", "media": 3}}), encoding="utf-8")
    assert ut.get_web_search(config_path=cfg)["api_key"] == "(empty)"
    assert ut.get_media("image", config_path=cfg)["model"] == ""
    ut.set_web_search({"api_key": "serper-abc"}, config_path=cfg)
    ut.set_media("image", {"model": "m"}, config_path=cfg)
    tools = _raw(cfg)["tools"]
    assert tools["web"]["search"]["apiKey"] == "serper-abc"
    assert tools["media"]["image"]["model"] == "m"


def test_malformed_config_refuses_the_new_writes_too(cfg: Path):
    original = '{\n  "providers": {"openai": {"apiKey": "sk-o"}},\n  // a comment => invalid JSON\n}\n'
    cfg.write_text(original, encoding="utf-8")
    for op in (
        lambda: ut.set_web_search({"api_key": "x"}, config_path=cfg),
        lambda: ut.get_web_search(config_path=cfg),
        lambda: ut.set_media("image", {"model": "m"}, config_path=cfg),
        lambda: ut.get_media("image", config_path=cfg),
    ):
        with pytest.raises(ut.ConfigReadError):
            op()
    assert cfg.read_text(encoding="utf-8") == original
