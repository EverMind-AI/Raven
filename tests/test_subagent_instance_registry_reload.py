"""The instance registry is shared between processes, not owned by one.

A sub-agent spawn is its own ``cli`` process and registers itself in this file,
while the gateway serving the UI is a different process reading it. The gateway
therefore has to notice writes it did not make.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.subagent.instances import InstanceRegistry


def _write(path: Path, records: list[dict[str, object]]) -> None:
    """Write the file the way another process would: wholesale, from outside."""
    path.write_text(json.dumps({"version": 1, "instances": records}), encoding="utf-8")


def _row(session: str, handle: str) -> dict[str, object]:
    return {
        "kind": "cli",
        "sessionKey": session,
        "agent": "Raven-Research",
        "handle": handle,
        "createdAtMs": 1,
        "updatedAtMs": 1,
    }


class TestAWriteFromAnotherProcessIsPickedUp:
    def test_a_record_added_after_the_first_read_is_listed(self, tmp_path: Path) -> None:
        path = tmp_path / "reg.json"
        _write(path, [_row("s1", "first")])
        registry = InstanceRegistry(path)
        assert [r["handle"] for r in registry.list_instances("s1")] == ["first"]

        # The spawn's own process registers itself while the gateway is running.
        _write(path, [_row("s1", "first"), _row("s1", "second")])

        listed = sorted(str(r["handle"]) for r in registry.list_instances("s1"))
        assert listed == ["first", "second"], (
            "the gateway answered from the snapshot it loaded at startup, so a "
            "session's own sub-agents stayed invisible until it was restarted"
        )

    def test_a_session_that_did_not_exist_at_startup_is_found(self, tmp_path: Path) -> None:
        path = tmp_path / "reg.json"
        _write(path, [])
        registry = InstanceRegistry(path)
        assert registry.list_instances("later") == []

        _write(path, [_row("later", "ran-after-boot")])

        assert [r["handle"] for r in registry.list_instances("later")] == ["ran-after-boot"]


class TestOurOwnWritesDoNotCauseARereadThatLosesThem:
    @pytest.mark.asyncio
    async def test_a_commit_survives_the_next_read(self, tmp_path: Path) -> None:
        path = tmp_path / "reg.json"
        _write(path, [_row("s1", "first")])
        registry = InstanceRegistry(path)

        await registry.commit("s1", "Raven-Research", "mine", "agent-id-1")

        listed = sorted(str(r["handle"]) for r in registry.list_instances("s1"))
        assert listed == ["first", "mine"]

    @pytest.mark.asyncio
    async def test_a_commit_does_not_make_the_next_read_reparse(self, tmp_path: Path) -> None:
        """Our own write is not news. Without re-stamping after the flush, every
        write left the cache looking stale and the next read re-parsed the whole
        file -- on a registry that grows to thousands of rows."""
        path = tmp_path / "reg.json"
        _write(path, [_row("s1", "first")])
        registry = InstanceRegistry(path)
        registry.list_instances("s1")

        reads = 0
        real = Path.read_text

        def counted(self: Path, *a: object, **kw: object) -> str:
            nonlocal reads
            if self == path:
                reads += 1
            return real(self, *a, **kw)  # type: ignore[arg-type]

        await registry.commit("s1", "Raven-Research", "mine", "agent-id-1")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "read_text", counted)
            registry.list_instances("s1")
        assert reads == 0

    @pytest.mark.asyncio
    async def test_a_commit_keeps_what_another_process_added_first(self, tmp_path: Path) -> None:
        """The reload is also what stops a stale cache overwriting the file.

        ``_flush`` writes the whole cached map back, so committing from a cache
        loaded before another process appended would drop that process's row.
        """
        path = tmp_path / "reg.json"
        _write(path, [_row("s1", "first")])
        registry = InstanceRegistry(path)
        registry.list_instances("s1")

        _write(path, [_row("s1", "first"), _row("s1", "from-elsewhere")])
        await registry.commit("s1", "Raven-Research", "mine", "agent-id-1")

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        handles = sorted(str(r["handle"]) for r in on_disk["instances"])
        assert handles == ["first", "from-elsewhere", "mine"]
