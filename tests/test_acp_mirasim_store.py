"""Which mirasim conversation each ACP session continues, across adapter processes."""

from __future__ import annotations

import json
from pathlib import Path

from raven.acp.mirasim import store as store_mod
from raven.acp.mirasim.store import Binding, BindingStore, store_path


def test_a_record_written_by_one_store_is_read_by_another(tmp_path: Path) -> None:
    path = tmp_path / "claude.json"
    BindingStore(path).put(Binding(session_id="acp-1", cwd="/w", mirasim_key="sess-1"))
    found = BindingStore(path).get("acp-1")
    assert found is not None
    assert (found.cwd, found.mirasim_key) == ("/w", "sess-1")
    assert found.used_at_ms > 0
    # Only what continuing needs: no model and no effort, which Raven re-asserts.
    assert set(json.loads(path.read_text())["sessions"]["acp-1"]) == {"cwd", "mirasimKey", "usedAtMs"}


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


def test_each_agent_has_its_own_file_under_the_raven_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    assert store_path("claude") == tmp_path / "home" / "subagent_sessions" / "mirasim" / "claude.json"
    assert store_path("claude") != store_path("codex")


def test_a_row_without_a_directory_is_no_record(tmp_path: Path) -> None:
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"version": 1, "sessions": {"acp-1": {"mirasimKey": "sess-1", "usedAtMs": 1}}}))
    assert BindingStore(path).get("acp-1") is None
