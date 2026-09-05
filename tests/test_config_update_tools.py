"""Unit tests for raven.config.update_tools (tools.deepResearch write path).

Pins the camelCase-on-disk contract: writes land as ``tools.deepResearch.apiKey``
(not snake_case), matching the rest of config.json.
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
