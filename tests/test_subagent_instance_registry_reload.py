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
from raven.config.agent_names import GENERIC_AGENT


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


class TestARowTheFileCannotHoldGoesNoFurther:
    @pytest.mark.asyncio
    async def test_a_later_write_from_another_session_still_lands(self, tmp_path: Path) -> None:
        """A text the file cannot encode fails its own write, and that write alone.

        Every session of the process shares the cache, and ``_flush`` writes the whole of it
        back: a row left in it after its own write failed made every later write fail the same
        way, whichever session made it, until the process restarted.
        """
        path = tmp_path / "reg.json"
        _write(path, [_row("s1", "first")])
        registry = InstanceRegistry(path)
        unencodable = "asks how " + chr(0xD83D) + " to configure it"

        with pytest.raises(UnicodeEncodeError):
            await registry.upsert_dag_node(
                "s2", "r1", "triage", "Raven-Research", "completed", route_evidence=unencodable
            )
        await registry.commit("s1", "Raven-Research", "mine", "agent-id-1")

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert sorted(str(r["handle"]) for r in on_disk["instances"]) == ["first", "mine"]
        assert registry.list_instances("s2") == []


class TestARowWrittenUnderAFormerNameOfItsAgent:
    """The generic agent was renamed, ``raven`` -> ``Raven`` -> ``Raven-General``,
    without this file being rewritten. A spawn continuing one of its instances asks
    under the current name, and has to reach the session the old row bound -- or
    the instance silently restarts without its context."""

    @staticmethod
    def _old_row(agent: str, *, updated: int = 1, agent_id: str = "sess-1") -> dict[str, object]:
        return {
            "kind": "acp",
            "sessionKey": "s1",
            "agent": agent,
            "handle": "h1",
            "agentId": agent_id,
            "createdAtMs": 1,
            "updatedAtMs": updated,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("former", ["raven", "Raven"])
    async def test_a_lookup_under_the_current_name_finds_it(self, tmp_path: Path, former: str) -> None:
        path = tmp_path / "reg.json"
        _write(path, [self._old_row(former)])

        assert await InstanceRegistry(path).lookup("s1", GENERIC_AGENT, "h1", kind="acp") == "sess-1"

    def test_it_is_listed_under_the_current_name(self, tmp_path: Path) -> None:
        path = tmp_path / "reg.json"
        _write(path, [self._old_row("Raven")])

        assert [r["agent"] for r in InstanceRegistry(path).list_instances("s1")] == [GENERIC_AGENT]

    def test_two_spellings_of_one_instance_keep_the_newer(self, tmp_path: Path) -> None:
        """The first rename could leave both: the old row, and the one a later
        resume wrote under the then-current name."""
        path = tmp_path / "reg.json"
        _write(
            path,
            [self._old_row("Raven", updated=5, agent_id="newer"), self._old_row("raven", updated=2, agent_id="older")],
        )

        (row,) = InstanceRegistry(path).list_instances("s1")
        assert row["agentId"] == "newer"

    @pytest.mark.parametrize("former", ["raven", "Raven"])
    async def test_alias_binding_reads_and_writes_agree_before_and_after_reload(self, tmp_path: Path, former: str):
        path = tmp_path / "reg.json"
        registry = InstanceRegistry(path)
        await registry.commit("s1", former, "h1", "sess-1", kind="acp")

        for reader in (registry, InstanceRegistry(path)):
            for name in (GENERIC_AGENT, "Raven", "raven"):
                assert await reader.lookup("s1", name, "h1", kind="acp") == "sess-1"
        assert await registry.bind_if_absent("s1", GENERIC_AGENT, "h1", "other", kind="acp") == "sess-1"
        assert await registry.unbind("s1", former, "h1")
        assert await registry.lookup("s1", GENERIC_AGENT, "h1", kind="acp") is None
        assert await registry.forget("s1", former, "h1")
        assert InstanceRegistry(path).list_instances("s1") == []

    @pytest.mark.parametrize("former", ["raven", "Raven"])
    async def test_alias_status_and_dag_link_update_the_existing_record(self, tmp_path: Path, former: str):
        path = tmp_path / "reg.json"
        _write(path, [self._old_row(former)])
        registry = InstanceRegistry(path)

        await registry.upsert_spawn("s1", former, "h1", "running")
        await registry.link_dag_node("s1", former, "h1", "run1", "node1")

        for reader in (registry, InstanceRegistry(path)):
            (record,) = reader.list_instances("s1")
            assert (record["agent"], record["agentId"], record["kind"]) == (GENERIC_AGENT, "sess-1", "acp")
            assert (record["status"], record["runId"], record["nodeId"]) == ("running", "run1", "node1")

        await registry.upsert_dag_node("s1", "run1", "node1", former, "running")
        await registry.upsert_dag_node("s1", "run1", "node1", GENERIC_AGENT, "completed")
        assert len(registry.list_instances("s1")) == 2
        assert {record["agent"] for record in registry.list_instances("s1")} == {GENERIC_AGENT}
