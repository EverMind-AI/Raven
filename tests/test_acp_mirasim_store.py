"""Which Mirasim conversation each ACP session continues, across adapter processes."""

from __future__ import annotations

import json
from pathlib import Path

from raven.acp.mirasim import store as store_mod
from raven.acp.mirasim.store import Binding, BindingStore, store_dir, store_path


def test_a_record_written_by_one_store_is_read_by_another(tmp_path: Path) -> None:
    path = tmp_path / "claude.json"
    BindingStore(path).put(Binding(session_id="acp-1", cwd="/w", mirasim_key="sess-1"))
    found = BindingStore(path).get("acp-1")
    assert found is not None
    assert (found.cwd, found.mirasim_key) == ("/w", "sess-1")
    assert found.used_at_ms > 0
    # Only what continuing needs: no model, no effort and no route, which every turn names.
    assert set(json.loads(path.read_text())["sessions"]["acp-1"]) == {"cwd", "mirasimKey", "usedAtMs"}


def test_a_record_written_with_a_route_still_reads_and_drops_it_on_the_next_write(tmp_path: Path) -> None:
    path = tmp_path / "claude.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {"acp-1": {"cwd": "/w", "mirasimKey": "s", "route": "platform", "usedAtMs": 2**62}},
            }
        )
    )
    store = BindingStore(path)
    found = store.get("acp-1")
    assert found is not None and found.mirasim_key == "s"
    store.put(found)
    assert "route" not in json.loads(path.read_text())["sessions"]["acp-1"]


def test_each_recorded_conversation_maps_back_to_the_session_that_continues_it(tmp_path: Path, monkeypatch) -> None:
    store = BindingStore(tmp_path / "claude.json")
    assert store.by_key() == {}
    store.put(Binding(session_id="acp-1", cwd="/w", mirasim_key="claude:a"))
    store.put(Binding(session_id="acp-2", cwd="/w"))
    store.put(Binding(session_id="acp-3", cwd="/w", mirasim_key="claude:c"))
    assert store.by_key() == {"claude:a": "acp-1", "claude:c": "acp-3"}
    later = store_mod._now_ms() + store_mod._RETENTION_MS + 1
    monkeypatch.setattr(store_mod, "_now_ms", lambda: later)
    assert store.by_key() == {}


def test_a_record_past_retention_is_gone_and_the_next_write_drops_it(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "claude.json"
    store = BindingStore(path)
    store.put(Binding(session_id="old", cwd="/w", mirasim_key="sess-1"))
    later = store_mod._now_ms() + store_mod._RETENTION_MS + 1
    monkeypatch.setattr(store_mod, "_now_ms", lambda: later)
    assert store.get("old") is None
    store.put(Binding(session_id="new", cwd="/w", mirasim_key="sess-2"))
    assert set(json.loads(path.read_text())["sessions"]) == {"new"}


def test_delete_forgets_one_record_and_says_whether_there_was_one(tmp_path: Path) -> None:
    store = BindingStore(tmp_path / "claude.json")
    assert store.delete("acp-1") is False
    store.put(Binding(session_id="acp-1", cwd="/w", mirasim_key="sess-1"))
    store.put(Binding(session_id="acp-2", cwd="/w", mirasim_key="sess-2"))
    assert store.delete("acp-1") is True
    assert store.delete("acp-1") is False
    assert store.get("acp-1") is None
    assert store.get("acp-2") is not None


def test_a_file_that_is_not_json_reads_as_empty_and_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "claude.json"
    path.write_text("{ torn")
    store = BindingStore(path)
    assert store.get("acp-1") is None
    store.put(Binding(session_id="acp-1", cwd="/w", mirasim_key="sess-1"))
    assert store.get("acp-1") is not None


def test_an_agent_has_a_file_of_its_own_under_the_raven_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    assert store_path("claude") == tmp_path / "home" / "subagent_sessions" / "mirasim" / "claude.json"
    assert store_path("claude") != store_path("codex")


def test_every_agents_records_live_in_one_folder_under_the_raven_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    assert store_dir() == tmp_path / "home" / "subagent_sessions" / "mirasim"


def test_a_row_without_a_directory_is_no_record(tmp_path: Path) -> None:
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"version": 1, "sessions": {"acp-1": {"mirasimKey": "sess-1", "usedAtMs": 1}}}))
    assert BindingStore(path).get("acp-1") is None
