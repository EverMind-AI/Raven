"""Atomic agent-config write path (req5 / P4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.config.update_subagents import (
    add_agent,
    get_agents,
    remove_agent,
    set_agents,
)


def _cfg(tmp_path: Path) -> Path:
    return tmp_path / "config.json"


def test_set_and_get_roundtrip(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    set_agents(
        [
            {"name": "claude_code", "kind": "cli", "command": "claude -p {prompt}"},
            {"name": "mirothinker", "kind": "openai", "base_url": "http://x/v1", "model": "m"},
        ],
        config_path=p,
    )
    got = get_agents(config_path=p)
    assert [g["name"] for g in got] == ["claude_code", "mirothinker"]
    # written under the current key, not the pre-rename one
    raw = json.loads(p.read_text())
    assert "agents" in raw["subagents"]
    assert "thirdParty" not in raw["subagents"]


def test_invalid_kind_rejected_and_not_written(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    with pytest.raises(ValidationError):
        set_agents([{"name": "bad", "kind": "nope"}], config_path=p)
    assert not p.exists()  # nothing written on validation failure


def test_duplicate_names_rejected(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    with pytest.raises(ValueError):
        set_agents(
            [
                {"name": "dup", "kind": "cli", "command": "a {prompt}"},
                {"name": "dup", "kind": "cli", "command": "b {prompt}"},
            ],
            config_path=p,
        )


def test_add_replaces_by_name(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    add_agent({"name": "codex", "kind": "cli", "command": "codex exec {prompt}"}, config_path=p)
    add_agent({"name": "codex", "kind": "cli", "command": "codex exec2 {prompt}"}, config_path=p)
    got = get_agents(config_path=p)
    assert len(got) == 1 and got[0]["command"] == "codex exec2 {prompt}"


def test_remove(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    set_agents([{"name": "codex", "kind": "cli", "command": "codex {prompt}"}], config_path=p)
    assert remove_agent("codex", config_path=p) is True
    assert remove_agent("codex", config_path=p) is False
    assert get_agents(config_path=p) == []


def test_preserves_other_config_sections(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    p.write_text(json.dumps({"providers": {"custom": {"apiBase": "http://x"}}, "agents": {}}))
    set_agents([{"name": "codex", "kind": "cli", "command": "codex {prompt}"}], config_path=p)
    raw = json.loads(p.read_text())
    assert raw["providers"]["custom"]["apiBase"] == "http://x"  # untouched
    assert raw["subagents"]["agents"][0]["name"] == "codex"


def test_a_config_on_the_old_key_is_read_and_migrated_on_write(tmp_path: Path) -> None:
    """The rename must not orphan a stored config, and must not leave two lists.

    Both spellings loading is what keeps an existing install working; writing only
    the new one is what stops a later read having to guess which list is current.
    """
    p = _cfg(tmp_path)
    p.write_text(
        json.dumps({"subagents": {"thirdParty": [{"name": "codex", "kind": "cli", "command": "codex {prompt}"}]}})
    )

    assert [g["name"] for g in get_agents(config_path=p)] == ["codex"]

    add_agent({"name": "claude_code", "kind": "cli", "command": "claude -p {prompt}"}, config_path=p)
    raw = json.loads(p.read_text())
    assert "thirdParty" not in raw["subagents"]
    assert [e["name"] for e in raw["subagents"]["agents"]] == ["codex", "claude_code"]


def test_a_builtin_name_cannot_be_claimed_by_another_transport(tmp_path: Path) -> None:
    """An override may retune a built-in agent; it may not replace it.

    Redeclaring the name as cli leaves the in-process agent unreachable under a
    name stored playbooks and instance records already point at.
    """
    p = _cfg(tmp_path)
    with pytest.raises(ValueError, match="built-in"):
        set_agents([{"name": "research-raven", "kind": "cli", "command": "x {prompt}"}], config_path=p)
    assert not p.exists()

    # Overriding it as a built-in row is the supported edit.
    set_agents([{"name": "research-raven", "kind": "builtin", "skills": ["local/web-search"]}], config_path=p)
    assert get_agents(config_path=p)[0]["skills"] == ["local/web-search"]
