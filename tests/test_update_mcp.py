"""Unit tests for the ``tools.mcpServers`` read/write path.

Mirrors ``test_update_skills.py``: every case drives a config file in tmp_path,
so nothing touches the real ``~/.raven/config.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.config.update_mcp import get_mcp_servers, set_mcp_servers


def _write(tmp: Path, cfg: dict) -> Path:
    p = tmp / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def _tools(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))["tools"]


def test_get_returns_one_entry_per_server_with_its_name(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {"tools": {"mcpServers": {"browser": {"command": "npx", "args": ["@playwright/mcp@latest"]}}}},
    )
    out = get_mcp_servers(config_path=p)
    assert len(out) == 1
    assert out[0]["name"] == "browser"
    assert out[0]["command"] == "npx"
    assert out[0]["args"] == ["@playwright/mcp@latest"]


def test_get_accepts_the_snake_case_spelling(tmp_path: Path) -> None:
    p = _write(tmp_path, {"tools": {"mcp_servers": {"api": {"url": "https://example.test/mcp"}}}})
    assert [s["name"] for s in get_mcp_servers(config_path=p)] == ["api"]


def test_get_on_a_config_without_the_block_is_empty(tmp_path: Path) -> None:
    assert get_mcp_servers(config_path=_write(tmp_path, {"tools": {}})) == []
    assert get_mcp_servers(config_path=tmp_path / "missing.json") == []


def test_set_replaces_the_list_and_preserves_other_tools_keys(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {"tools": {"restrictToWorkspace": True, "mcpServers": {"old": {"command": "old-cmd"}}}},
    )
    set_mcp_servers([{"name": "new", "command": "new-cmd"}], config_path=p)
    tools = _tools(p)
    assert list(tools["mcpServers"]) == ["new"]
    assert tools["mcpServers"]["new"]["command"] == "new-cmd"
    assert tools["restrictToWorkspace"] is True


def test_set_writes_under_the_spelling_already_in_the_file(tmp_path: Path) -> None:
    """Writing camelCase into a snake_case file would leave two blocks, one of
    them shadowing the other."""
    p = _write(tmp_path, {"tools": {"mcp_servers": {"old": {"command": "x"}}}})
    set_mcp_servers([{"name": "keep", "command": "y"}], config_path=p)
    tools = _tools(p)
    assert "mcpServers" not in tools
    assert list(tools["mcp_servers"]) == ["keep"]


def test_set_to_empty_removes_every_server(tmp_path: Path) -> None:
    p = _write(tmp_path, {"tools": {"mcpServers": {"a": {"command": "x"}}}})
    set_mcp_servers([], config_path=p)
    assert _tools(p)["mcpServers"] == {}


def test_set_rejects_a_server_with_neither_command_nor_url(tmp_path: Path) -> None:
    """It would be skipped at connect time, so it would look configured and
    never work."""
    p = _write(tmp_path, {"tools": {}})
    with pytest.raises(ValueError, match="command"):
        set_mcp_servers([{"name": "broken"}], config_path=p)


def test_set_rejects_a_nameless_server(tmp_path: Path) -> None:
    p = _write(tmp_path, {"tools": {}})
    with pytest.raises(ValueError, match="name"):
        set_mcp_servers([{"command": "x"}], config_path=p)


def test_set_rejects_duplicate_names(tmp_path: Path) -> None:
    p = _write(tmp_path, {"tools": {}})
    with pytest.raises(ValueError, match="duplicate"):
        set_mcp_servers([{"name": "a", "command": "x"}, {"name": "a", "url": "http://y"}], config_path=p)


def test_set_rejects_an_unknown_transport_type(tmp_path: Path) -> None:
    p = _write(tmp_path, {"tools": {}})
    with pytest.raises(ValidationError):
        set_mcp_servers([{"name": "a", "command": "x", "type": "carrier-pigeon"}], config_path=p)


def test_round_trip_survives_get_after_set(tmp_path: Path) -> None:
    p = _write(tmp_path, {"tools": {}})
    set_mcp_servers(
        [
            {"name": "browser", "command": "npx", "args": ["@playwright/mcp@latest"]},
            {"name": "api", "url": "https://example.test/mcp", "headers": {"X-Key": "k"}},
        ],
        config_path=p,
    )
    out = {s["name"]: s for s in get_mcp_servers(config_path=p)}
    assert out["browser"]["command"] == "npx"
    assert out["api"]["url"] == "https://example.test/mcp"
    assert out["api"]["headers"] == {"X-Key": "k"}


def test_get_reports_a_malformed_entry_instead_of_failing_the_whole_list(tmp_path: Path) -> None:
    """One hand-broken server must not blank the panel it is listed in."""
    p = _write(tmp_path, {"tools": {"mcpServers": {"ok": {"command": "npx"}, "bad": {"command": 123}}}})
    out = {s["name"]: s for s in get_mcp_servers(config_path=p)}
    assert out["ok"]["command"] == "npx"
    assert "error" not in out["ok"]
    assert "command" in out["bad"]["error"]


def test_set_keeps_keys_the_schema_does_not_know(tmp_path: Path) -> None:
    """A save from the panel must not erase a key this build cannot parse:
    the panel round-trips through a validated shape that has dropped it."""
    p = _write(tmp_path, {"tools": {"mcpServers": {"browser": {"command": "npx", "futureField": 7}}}})
    set_mcp_servers(get_mcp_servers(config_path=p), config_path=p)
    assert _tools(p)["mcpServers"]["browser"] == {"futureField": 7, "command": "npx"}


def test_set_does_not_persist_the_live_state_the_list_call_merges_in(tmp_path: Path) -> None:
    """``raven.mcp.list`` returns config merged with ``connected``/``tools``
    read off the running agent, and the panel sends that same object back on
    every save. Persisting it would make the config assert a connection state
    that was true for one instant of one gateway run."""
    p = _write(tmp_path, {"tools": {"mcpServers": {"browser": {"command": "npx"}}}})
    listed = [{**s, "connected": True, "tools": ["click", "navigate"]} for s in get_mcp_servers(config_path=p)]
    set_mcp_servers([*listed, {"name": "api", "url": "https://x.test/mcp"}], config_path=p)
    assert _tools(p)["mcpServers"] == {
        "browser": {"command": "npx"},
        "api": {"url": "https://x.test/mcp"},
    }


def test_set_scrubs_live_state_an_earlier_build_persisted(tmp_path: Path) -> None:
    """Filtering only the caller's entry would preserve the junk already on
    disk forever, since unknown keys are carried through from the file too."""
    p = _write(
        tmp_path,
        {"tools": {"mcpServers": {"browser": {"command": "npx", "connected": True, "tools": ["click"]}}}},
    )
    set_mcp_servers(get_mcp_servers(config_path=p), config_path=p)
    assert _tools(p)["mcpServers"]["browser"] == {"command": "npx"}


def test_set_accepts_the_snake_case_spelling_the_schema_accepts(tmp_path: Path) -> None:
    """``MCPServerConfig`` takes both spellings, so a caller using snake_case
    must not have the value silently filtered out before validation."""
    p = _write(tmp_path, {"tools": {}})
    set_mcp_servers([{"name": "api", "url": "https://x.test", "tool_timeout": 90}], config_path=p)
    assert _tools(p)["mcpServers"]["api"]["toolTimeout"] == 90


def test_set_drops_the_other_spelling_of_the_block(tmp_path: Path) -> None:
    """Leaving the shadowed block behind means a deleted server returns the
    moment someone removes the block that was hiding it."""
    p = _write(
        tmp_path,
        {"tools": {"mcpServers": {"camel": {"command": "a"}}, "mcp_servers": {"snake": {"command": "b"}}}},
    )
    set_mcp_servers([{"name": "camel", "command": "a"}], config_path=p)
    assert "mcp_servers" not in _tools(p)
    assert list(_tools(p)["mcpServers"]) == ["camel"]


def test_set_rejects_a_non_list_and_a_non_object_entry(tmp_path: Path) -> None:
    p = _write(tmp_path, {"tools": {}})
    with pytest.raises(ValueError, match="must be a list"):
        set_mcp_servers({"browser": {"command": "npx"}}, config_path=p)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be an object"):
        set_mcp_servers(["browser"], config_path=p)  # type: ignore[list-item]
