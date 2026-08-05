"""Atomic third-party sub-agent config write path (req5 / P4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.config.update_subagents import (
    add_third_party_subagent,
    get_third_party_subagents,
    remove_third_party_subagent,
    set_third_party_subagents,
)


def _cfg(tmp_path: Path) -> Path:
    return tmp_path / "config.json"


def test_set_and_get_roundtrip(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    set_third_party_subagents(
        [
            {"name": "claude_code", "kind": "cli", "command": "claude -p {prompt}"},
            {"name": "mirothinker", "kind": "openai", "base_url": "http://x/v1", "model": "m"},
        ],
        config_path=p,
    )
    got = get_third_party_subagents(config_path=p)
    assert [g["name"] for g in got] == ["claude_code", "mirothinker"]
    # written under the camelCase alias key
    raw = json.loads(p.read_text())
    assert "thirdParty" in raw["subagents"]


def test_invalid_kind_rejected_and_not_written(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    with pytest.raises(ValidationError):
        set_third_party_subagents([{"name": "bad", "kind": "nope"}], config_path=p)
    assert not p.exists()  # nothing written on validation failure


def test_duplicate_names_rejected(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    with pytest.raises(ValueError):
        set_third_party_subagents(
            [
                {"name": "dup", "kind": "cli", "command": "a {prompt}"},
                {"name": "dup", "kind": "cli", "command": "b {prompt}"},
            ],
            config_path=p,
        )


def test_add_replaces_by_name(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    add_third_party_subagent({"name": "codex", "kind": "cli", "command": "codex exec {prompt}"}, config_path=p)
    add_third_party_subagent({"name": "codex", "kind": "cli", "command": "codex exec2 {prompt}"}, config_path=p)
    got = get_third_party_subagents(config_path=p)
    assert len(got) == 1 and got[0]["command"] == "codex exec2 {prompt}"


def test_remove(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    set_third_party_subagents([{"name": "codex", "kind": "cli", "command": "codex {prompt}"}], config_path=p)
    assert remove_third_party_subagent("codex", config_path=p) is True
    assert remove_third_party_subagent("codex", config_path=p) is False
    assert get_third_party_subagents(config_path=p) == []


def test_preserves_other_config_sections(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    p.write_text(json.dumps({"providers": {"custom": {"apiBase": "http://x"}}, "agents": {}}))
    set_third_party_subagents([{"name": "codex", "kind": "cli", "command": "codex {prompt}"}], config_path=p)
    raw = json.loads(p.read_text())
    assert raw["providers"]["custom"]["apiBase"] == "http://x"  # untouched
    assert raw["subagents"]["thirdParty"][0]["name"] == "codex"
