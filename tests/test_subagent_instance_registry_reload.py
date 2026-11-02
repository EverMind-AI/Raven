"""The instance registry is shared between processes, not owned by one.

A sub-agent spawn is its own ``cli`` process and registers itself in this file,
while the gateway serving the UI is a different process reading it. The gateway
therefore has to notice writes it did not make.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.subagent import instances as instances_mod
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
    and the rename itself rewrote nothing here: a row keeps its former name on disk
    until the registry's next write, which saves every loaded row under the current
    one. A spawn continuing one of its instances asks
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


class TestMovingADagNodeRowToAnotherAgent:
    """A node's ``dag-node`` row is keyed by agent, so a reassigned node that only
    wrote a row under its new agent would be listed twice: the old row is still
    there. The move writes the new row and drops the old one in one transaction.

    The row's ``handle`` is synthetic (``<run>/<node>``) and cannot say which
    instance the node actually ran on, so a moved row carries ``nodeInstance``
    and ``nodeInstanceAuto`` beside the attempt history.
    """

    @staticmethod
    async def _alpha_row(registry: InstanceRegistry) -> int:
        await registry.upsert_dag_node("s1", "r1", "fix", "alpha", "exception", selected_routes=[])
        await registry.link_dag_node("s1", "alpha", "fix-h", "r1", "fix")
        return int(next(r for r in registry.list_instances("s1") if r.get("kind") == "dag-node")["createdAtMs"])

    @pytest.mark.asyncio
    async def test_a_move_leaves_one_row_under_the_new_agent_with_its_creation_time_and_history(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "reg.json"
        registry = InstanceRegistry(path)
        created_before = await self._alpha_row(registry)

        await registry.move_dag_node(
            "s1",
            run_id="r1",
            node_id="fix",
            old_agent="alpha",
            new_agent="beta",
            status="running",
            instance="fix-minted",
            instance_auto=True,
            attempt_agents=["alpha", "beta"],
        )

        rows = InstanceRegistry(path).list_instances("s1")
        moved = [r for r in rows if r.get("kind") == "dag-node" and r.get("nodeId") == "fix"]
        assert len(moved) == 1
        moved = moved[0]
        assert moved["agent"] == "beta"
        assert moved["createdAtMs"] == created_before
        assert moved["attemptAgents"] == ["alpha", "beta"]
        assert moved["selectedRoutes"] == []
        assert (moved["nodeInstance"], moved["nodeInstanceAuto"]) == ("fix-minted", True)

    @pytest.mark.asyncio
    async def test_a_move_does_not_carry_the_native_session_binding(self, tmp_path: Path) -> None:
        """The new agent's session is its own. Transplanting the old ``agentId``
        would make the destination resume a conversation it never had, so the
        row the move writes carries none and the new pair binds nothing."""
        path = tmp_path / "reg.json"
        _write(
            path,
            [
                {
                    "kind": "dag-node",
                    "sessionKey": "s1",
                    "agent": "alpha",
                    "handle": "r1/fix",
                    "runId": "r1",
                    "nodeId": "fix",
                    "status": "exception",
                    "agentId": "alpha-native-session",
                    "createdAtMs": 1,
                    "updatedAtMs": 1,
                }
            ],
        )
        registry = InstanceRegistry(path)

        await registry.move_dag_node(
            "s1",
            run_id="r1",
            node_id="fix",
            old_agent="alpha",
            new_agent="beta",
            status="running",
            instance="fix-minted",
            instance_auto=True,
            attempt_agents=["alpha", "beta"],
        )

        moved = next(r for r in InstanceRegistry(path).list_instances("s1") if r.get("kind") == "dag-node")
        assert "agentId" not in moved
        assert await registry.lookup("s1", "beta", "fix-minted") is None
        assert await registry.lookup("s1", "beta", "r1/fix", kind="dag-node") is None

    @pytest.mark.asyncio
    async def test_a_move_keeps_the_branch_outcome_it_was_not_told_again(self, tmp_path: Path) -> None:
        """A branch's selection is the node's, not the attempt's: the move is a
        status change and must not erase what the branch already decided."""
        path = tmp_path / "reg.json"
        registry = InstanceRegistry(path)
        await registry.upsert_dag_node(
            "s1", "r1", "fix", "alpha", "exception", reason="when_false", route_evidence="it said so"
        )
        await registry.upsert_dag_node("s1", "r1", "fix", "alpha", "exception", selected_routes=["docs"])

        await registry.move_dag_node(
            "s1",
            run_id="r1",
            node_id="fix",
            old_agent="alpha",
            new_agent="beta",
            status="running",
            instance=None,
            instance_auto=False,
            attempt_agents=["alpha", "beta"],
        )

        moved = next(r for r in InstanceRegistry(path).list_instances("s1") if r.get("kind") == "dag-node")
        assert moved["selectedRoutes"] == ["docs"]

    @pytest.mark.asyncio
    async def test_a_move_does_not_restore_a_stale_snapshot_over_unrelated_writes(self, tmp_path: Path) -> None:
        """The registry is read-modify-written by several processes. A move made
        from a cache loaded before another process appended must not flush that
        older map back and drop the other row."""
        path = tmp_path / "reg.json"
        _write(path, [_row("s1", "first")])
        registry = InstanceRegistry(path)
        await self._alpha_row(registry)
        await InstanceRegistry(path).commit("s1", "Raven-Research", "from-elsewhere", "agent-x")

        await registry.move_dag_node(
            "s1",
            run_id="r1",
            node_id="fix",
            old_agent="alpha",
            new_agent="beta",
            status="running",
            instance=None,
            instance_auto=False,
            attempt_agents=["alpha", "beta"],
        )

        rows = InstanceRegistry(path).list_instances("s1")
        assert ("Raven-Research", "from-elsewhere") in {(r["agent"], r["handle"]) for r in rows}
        assert len([r for r in rows if r.get("kind") == "dag-node"]) == 1

    @pytest.mark.asyncio
    async def test_a_failed_move_keeps_the_previous_row_and_a_retry_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "reg.json"
        registry = InstanceRegistry(path)
        created_before = await self._alpha_row(registry)
        real = instances_mod.atomic_replace
        calls = {"n": 0}

        def failing(target: Path, text: str) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("no space left on device")
            real(target, text)

        monkeypatch.setattr(instances_mod, "atomic_replace", failing)
        arguments = {
            "run_id": "r1",
            "node_id": "fix",
            "old_agent": "alpha",
            "new_agent": "beta",
            "status": "running",
            "instance": "fix-minted",
            "instance_auto": True,
            "attempt_agents": ["alpha", "beta"],
        }
        with pytest.raises(OSError):
            await registry.move_dag_node("s1", **arguments)

        def on_disk() -> list[dict[str, object]]:
            return InstanceRegistry(path).list_instances("s1")

        # The transaction left the file as it was -- and the writer that failed
        # must not answer from the map it could not persist either, or a reader
        # sharing this process sees a row no other process can.
        for rows in (on_disk(), registry.list_instances("s1")):
            held = [r for r in rows if r.get("kind") == "dag-node"]
            assert len(held) == 1
            assert (held[0]["agent"], held[0].get("attemptAgents")) == ("alpha", None)

        await registry.move_dag_node("s1", **arguments)

        moved = [r for r in on_disk() if r.get("kind") == "dag-node"]
        assert len(moved) == 1
        assert (moved[0]["agent"], moved[0]["createdAtMs"]) == ("beta", created_before)

    @pytest.mark.asyncio
    async def test_a_moved_node_that_never_had_a_row_gets_one_under_the_target(self, tmp_path: Path) -> None:
        """A move accepted while a node was still pending writes its first row
        under the destination; there is no old row to drop."""
        path = tmp_path / "reg.json"

        await InstanceRegistry(path).move_dag_node(
            "s1",
            run_id="r1",
            node_id="fix",
            old_agent="alpha",
            new_agent="beta",
            status="running",
            instance=None,
            instance_auto=False,
            attempt_agents=["beta"],
        )

        (moved,) = InstanceRegistry(path).list_instances("s1")
        assert (moved["agent"], moved["runId"], moved["nodeId"]) == ("beta", "r1", "fix")
        assert moved["attemptAgents"] == ["beta"]


class TestAttemptHistoryOnAStatusWrite:
    """``attemptAgents`` and the execution instance travel on the row of a node
    that moved. A node that never moved keeps exactly the row it always had --
    an empty history on every node would claim a reassignment that never
    happened."""

    @pytest.mark.asyncio
    async def test_a_node_that_never_moved_carries_no_attempt_history(self, tmp_path: Path) -> None:
        registry = InstanceRegistry(tmp_path / "reg.json")

        await registry.upsert_dag_node("s1", "r1", "plain", "alpha", "completed")

        (row,) = registry.list_instances("s1")
        assert not {"attemptAgents", "nodeInstance", "nodeInstanceAuto"} & set(row)

    @pytest.mark.asyncio
    async def test_a_status_write_can_carry_the_attempt_history_and_execution_instance(self, tmp_path: Path) -> None:
        registry = InstanceRegistry(tmp_path / "reg.json")

        await registry.upsert_dag_node(
            "s1", "r1", "moved", "beta", "running", attempt_agents=["alpha", "beta"], instance=None, instance_auto=False
        )

        (row,) = InstanceRegistry(tmp_path / "reg.json").list_instances("s1")
        assert row["attemptAgents"] == ["alpha", "beta"]
        assert row["nodeInstance"] is None
        assert row["nodeInstanceAuto"] is False
