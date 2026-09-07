"""Persistent identity generations, configuration reconciliation and exact resolution."""

import json

import pytest

from raven.agent.registry.identity import IdentityRegistry
from raven.contracts.terminal import TerminalError, TerminalRecord


def terminal():
    return TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work")


def test_register_roundtrip_and_generations(tmp_path):
    rows = [{"name": "coder", "kind": "cli", "command": "codex", "preset": "codex"}]
    path = tmp_path / "agent_registry.json"
    registry = IdentityRegistry(path, config_rows=lambda: rows)
    first = registry.register(
        "worker-a", kind_ref="coder", binding=terminal(), aliases=[{"alias": "coder-a", "source": "human"}]
    )
    second = registry.register("worker-a", kind_ref="coder", binding=terminal())
    assert first.binding_generation == 1
    assert second.binding_generation == 2
    assert second.first_observed_at == first.first_observed_at
    assert second.brand == "codex"
    loaded = IdentityRegistry(path, config_rows=lambda: rows)
    assert loaded.show("worker-a") == second
    raw = json.loads(path.read_text())
    assert raw["schema_version"] == 1
    assert len(raw["records"]) == 2
    assert len(loaded.list()) == 1


def test_unknown_kind_ref_never_writes(tmp_path):
    path = tmp_path / "agent_registry.json"
    registry = IdentityRegistry(path, config_rows=lambda: [])
    with pytest.raises(TerminalError, match="kind_ref"):
        registry.register("worker-a", kind_ref="missing")
    assert not path.exists()


def test_startup_and_hot_apply_keep_orphans_listable(tmp_path):
    rows = [{"name": "coder", "kind": "builtin"}]
    path = tmp_path / "agent_registry.json"
    registry = IdentityRegistry(path, config_rows=lambda: rows)
    registry.register("worker-a", kind_ref="coder")
    rows.clear()
    registry.reconcile()
    assert registry.list()[0].orphan
    assert IdentityRegistry(path, config_rows=lambda: rows).show("worker-a").orphan
    rows.append({"name": "coder", "kind": "builtin"})
    registry.reconcile()
    assert not registry.show("worker-a").orphan


def test_resolution_reports_ambiguous_aliases_without_fuzzy_matching(tmp_path):
    registry = IdentityRegistry(
        tmp_path / "agent_registry.json", config_rows=lambda: [{"name": "coder", "kind": "builtin"}]
    )
    for name in ["worker-a", "worker-b"]:
        registry.register(name, kind_ref="coder", aliases=[{"alias": "coder", "source": "human"}])
    assert registry.resolve("worker-a")["unique"]
    assert not registry.resolve("coder")["unique"]
    assert len(registry.resolve("coder")["candidates"]) == 2
    assert registry.resolve("worker")["candidates"] == []


def test_corrupt_state_does_not_block_construction_or_get_overwritten(tmp_path):
    path = tmp_path / "agent_registry.json"
    path.write_text("broken")
    registry = IdentityRegistry(path, config_rows=lambda: [])
    with pytest.raises(TerminalError):
        registry.list()
    assert path.read_text() == "broken"


def test_exited_generations_archive_after_thirty_days(tmp_path):
    now = [100.0]
    path = tmp_path / "agent_registry.json"
    registry = IdentityRegistry(path, config_rows=lambda: [{"name": "coder", "kind": "builtin"}], clock=lambda: now[0])
    record = registry.register("worker-a", kind_ref="coder", binding=terminal())
    registry.mark_exited(record.binding.handle)
    now[0] += 31 * 86400
    registry.reconcile()
    assert registry.list() == []
    assert len(json.loads(path.read_text())["archive"]) == 1


def test_native_host_identity_is_unbound_and_idempotent(tmp_path):
    registry = IdentityRegistry(tmp_path / "agent_registry.json", config_rows=lambda: [{"name": "generic", "kind": "builtin"}])
    host = registry.ensure_host()
    assert host.agent_name == "raven"
    assert host.kind_ref == "generic"
    assert host.binding.handle is None
    assert registry.ensure_host().binding_generation == host.binding_generation
