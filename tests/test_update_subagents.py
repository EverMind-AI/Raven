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


def _entries(path: Path) -> list[dict]:
    return json.loads(path.read_text())["subagents"]["agents"]


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
        set_agents([{"name": "raven", "kind": "cli", "command": "x {prompt}"}], config_path=p)
    assert not p.exists()

    # Overriding it as a built-in row is the supported edit.
    set_agents([{"name": "raven", "kind": "builtin", "skills": ["local/web-search"]}], config_path=p)
    assert get_agents(config_path=p)[0]["skills"] == ["local/web-search"]


class TestTheBuiltinNamesAreReserved:
    """Both spellings of the generic row's name belong to it, and no transport
    may claim either -- but a config that already holds one must stay editable.

    `Raven` was accepted before the generic row was renamed to it, so real installs
    can hold such an entry. Refusing every write while one is on disk would lock the
    user out of the whole config surface, including the edit that removes it: the
    guard rejects *introducing* a reserved name, not possessing one.
    """

    @pytest.mark.parametrize("name", ["Raven", "raven"])
    @pytest.mark.parametrize("kind", ["cli", "openai"])
    def test_a_new_entry_cannot_claim_either_spelling(self, tmp_path: Path, name: str, kind: str) -> None:
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"subagents": {"agents": []}}))

        with pytest.raises(ValueError, match="built-in"):
            set_agents([{"name": name, "kind": kind, "command": "x {prompt}"}], config_path=p)

    @pytest.mark.parametrize("name", ["Raven", "raven"])
    def test_an_acp_redeclaration_of_a_builtin_name_is_allowed(self, tmp_path: Path, name: str) -> None:
        """An acp row of a seed's name is the supported transport switch: the
        generic agent is then served over this raven's own `raven acp`. An empty
        command is kept as written -- merge_builtin_seeds fills the host command
        at materialization, so a config file stays machine-portable."""
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"subagents": {"agents": []}}))

        set_agents([{"name": name, "kind": "acp", "command": ""}], config_path=p)

        from raven.agent.subagent.builtin_agents import canonical_agent_name

        stored = {canonical_agent_name(e["name"]): e for e in _entries(p)}
        assert stored["Raven"]["kind"] == "acp"

    def test_another_casing_is_not_reserved(self, tmp_path: Path) -> None:
        # Only the two spellings the row answers to are taken. `RAVEN` resolves to
        # itself, collides with no seed, and is an ordinary name.
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"subagents": {"agents": []}}))

        set_agents([{"name": "RAVEN", "kind": "cli", "command": "x {prompt}"}], config_path=p)

        assert [e["name"] for e in _entries(p)] == ["RAVEN"]

    def test_an_entry_already_on_disk_does_not_block_an_unrelated_edit(self, tmp_path: Path) -> None:
        p = tmp_path / "config.json"
        p.write_text(
            json.dumps(
                {
                    "subagents": {
                        "agents": [
                            {"name": "Raven", "kind": "cli", "command": "x {prompt}", "description": "legacy"},
                            {"name": "Coder", "kind": "cli", "command": "x {prompt}", "description": "unrelated"},
                        ]
                    }
                }
            )
        )

        # The edit the user is actually making is to Coder. Before this guard knew
        # what was already on disk, the legacy row made every write fail -- including
        # the one that would have removed it.
        set_agents(
            [
                {"name": "Raven", "kind": "cli", "command": "x {prompt}", "description": "legacy"},
                {"name": "Coder", "kind": "cli", "command": "x {prompt}", "description": "edited"},
            ],
            config_path=p,
        )

        stored = {e["name"]: e for e in _entries(p)}
        assert stored["Coder"]["description"] == "edited"
        assert "Raven" in stored

    def test_and_removing_the_legacy_entry_is_allowed(self, tmp_path: Path) -> None:
        p = tmp_path / "config.json"
        p.write_text(
            json.dumps(
                {
                    "subagents": {
                        "agents": [{"name": "Raven", "kind": "cli", "command": "x {prompt}", "description": "l"}]
                    }
                }
            )
        )

        set_agents([], config_path=p)

        assert _entries(p) == []
