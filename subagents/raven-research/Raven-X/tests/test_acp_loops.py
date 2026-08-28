"""The per-session engine registry: isolation, the cap, and the close order."""

import asyncio

import pytest

from raven.acp.loops import DEFAULT_MAX_LOOPS, AcpLoops, close_loop


class _StatefulTool:
    """Stands in for WebSearchTool: per-turn state on the instance, reset at the
    start of every turn -- the exact shape that made a shared engine unsafe."""

    def __init__(self):
        self.searches = 0
        self.seen: set[str] = set()
        self.retry_budget = 3

    def start_turn(self):
        self.searches = 0
        self.seen.clear()
        self.retry_budget = 3


class _StubLoop:
    def __init__(self, conversation=None):
        self.conversation = conversation
        self.workspace = "/shared"
        self.file_workspace = f"/shared/sessions/{conversation}" if conversation else None
        self.tool = _StatefulTool()
        self.tools = {"web_search": self.tool}
        self.closed: list[str] = []

    def stop(self):
        self.closed.append("stop")

    async def drain_backend_stores(self):
        self.closed.append("drain")

    async def close_executor(self):
        self.closed.append("executor")

    async def close_mcp(self):
        self.closed.append("mcp")


def _registry(**kwargs) -> AcpLoops:
    return AcpLoops(_StubLoop, **kwargs)


async def test_each_conversation_gets_its_own_engine_and_keeps_it():
    loops = _registry()
    first = await loops.get("acp:a")
    second = await loops.get("acp:b")
    assert first is not second
    assert await loops.get("acp:a") is first
    assert loops.resident() == ("acp:b", "acp:a")


async def test_concurrent_sessions_do_not_clear_each_others_turn_state():
    # The assertion this whole change exists for. On one shared engine B's
    # start_turn() wipes A's in-flight searches, seen set and retry budget; with
    # an engine each, A's state survives B's turn untouched.
    loops = _registry()
    a = await loops.get("acp:a")
    b = await loops.get("acp:b")

    a.tool.start_turn()
    a.tool.searches = 4
    a.tool.seen.add("https://example.com/a")
    a.tool.retry_budget = 1

    b.tool.start_turn()

    assert a.tool.searches == 4
    assert a.tool.seen == {"https://example.com/a"}
    assert a.tool.retry_budget == 1
    assert b.tool.searches == 0
    assert b.tool.seen == set()


async def test_a_single_registry_shares_one_engine_and_says_so():
    loop = _StubLoop("acp:only")
    loops = AcpLoops.single(loop)
    assert loops.per_session is False
    assert await loops.get("acp:a") is loop
    assert await loops.get("acp:b") is loop


async def test_the_session_manager_is_readable_before_any_engine_exists():
    # session/load peeks the store for a session no engine was built for yet,
    # so the manager cannot be fished off a loop at that point.
    manager = object()
    loops = _registry(session_manager=manager)
    assert loops.session_manager is manager
    assert loops.resident() == ()


async def test_single_takes_the_session_manager_off_the_engine():
    loop = _StubLoop("acp:only")
    loop.sessions = object()
    assert AcpLoops.single(loop).session_manager is loop.sessions


async def test_create_hooks_run_on_every_engine_including_later_ones():
    loops = _registry()
    seen: list[str] = []
    loops.on_create(lambda agent_loop: seen.append(agent_loop.conversation))
    await loops.get("acp:a")
    await loops.get("acp:b")
    await loops.get("acp:a")
    assert seen == ["acp:a", "acp:b"]


async def test_a_failing_create_hook_does_not_lose_the_engine():
    loops = _registry()

    def boom(_agent_loop):
        raise RuntimeError("hook exploded")

    loops.on_create(boom)
    engine = await loops.get("acp:a")
    assert loops.peek("acp:a") is engine


async def test_cwd_for_reports_the_sessions_own_file_root_and_never_builds():
    loops = _registry()
    assert loops.cwd_for("acp:a") is None
    await loops.get("acp:a")
    assert loops.cwd_for("acp:a") == "/shared/sessions/acp:a"
    assert loops.resident() == ("acp:a",)


async def test_the_cap_evicts_the_least_recently_used_idle_session():
    loops = _registry(max_loops=2)
    a = await loops.get("acp:a")
    await loops.get("acp:b")
    await loops.get("acp:a")  # touch a, so b is now the oldest
    await loops.get("acp:c")
    assert set(loops.resident()) == {"acp:a", "acp:c"}
    assert loops.peek("acp:b") is None
    await loops.aclose()
    assert a.closed == ["stop", "drain", "executor", "mcp"]


async def test_a_session_mid_turn_is_never_evicted():
    """Also the only case that distinguishes the ``keep`` guard.

    Two properties, one situation: the busy session is not a candidate, and
    neither is the engine the caller is being handed -- with the busy sibling
    skipped it is the oldest idle key, so a cap-first eviction would close the
    engine the turn is about to run on.
    """
    loops = _registry(max_loops=1)
    loops.set_busy(lambda conversation: conversation == "acp:busy")
    busy = await loops.get("acp:busy")
    idle = await loops.get("acp:idle")
    assert loops.peek("acp:busy") is busy
    assert loops.peek("acp:idle") is idle
    await loops.aclose()


async def test_an_over_cap_engine_is_reclaimed_once_its_turn_ends():
    """Found in review: the cap was applied at build time and never again.

    Going over it is the deliberate choice while every victim is mid-turn, but
    those turns ending is not an event this registry sees, so the extra engine
    -- a whole tool registry and executor -- stayed resident past the
    configured ceiling for as long as the connection was quiet.
    """
    loops = _registry(max_loops=1)
    busy = {"acp:a"}
    loops.set_busy(lambda conversation: conversation in busy)
    first = await loops.get("acp:a")
    await loops.get("acp:b")
    assert set(loops.resident()) == {"acp:a", "acp:b"}, "over the cap while a runs"

    busy.clear()
    loops.reclaim()
    assert loops.resident() == ("acp:b",)
    await loops.aclose()
    assert first.closed == ["stop", "drain", "executor", "mcp"]


async def test_handing_out_an_existing_engine_rechecks_the_cap():
    """The fast path returned before the cap was consulted.

    So a registry left over the cap by a build that found its victim busy
    stayed over it through every later turn on a session it already had an
    engine for -- which is most turns.
    """
    loops = _registry(max_loops=1)
    busy = {"acp:a"}
    loops.set_busy(lambda conversation: conversation in busy)
    await loops.get("acp:a")
    second = await loops.get("acp:b")
    assert set(loops.resident()) == {"acp:a", "acp:b"}

    busy.clear()
    assert await loops.get("acp:b") is second
    assert loops.resident() == ("acp:b",)
    await loops.aclose()


async def test_reclaiming_never_takes_an_engine_from_a_running_turn():
    """The busy predicate is the whole guard on this path.

    Unlike a build there is no engine being handed out, so ``keep`` protects
    nothing here and a registry whose every resident session is mid-turn has to
    stay over its cap rather than pull the tools out from under a turn.
    """
    loops = _registry(max_loops=1)
    busy = {"acp:a", "acp:b"}
    loops.set_busy(lambda conversation: conversation in busy)
    a = await loops.get("acp:a")
    b = await loops.get("acp:b")

    loops.reclaim()
    assert loops.peek("acp:a") is a
    assert loops.peek("acp:b") is b
    await loops.aclose()


async def test_an_evicted_engine_is_closed_and_the_next_turn_gets_a_fresh_one():
    loops = _registry(max_loops=1)
    first = await loops.get("acp:a")
    await loops.get("acp:b")
    await loops.aclose()
    assert first.closed == ["stop", "drain", "executor", "mcp"]
    assert await loops.get("acp:a") is not first


async def test_aclose_closes_every_resident_engine_and_empties_the_registry():
    loops = _registry()
    engines = [await loops.get(f"acp:{n}") for n in range(3)]
    await loops.aclose()
    assert loops.resident() == ()
    for engine in engines:
        assert engine.closed == ["stop", "drain", "executor", "mcp"]


async def test_closing_survives_an_engine_that_fails_halfway():
    class _Broken(_StubLoop):
        async def drain_backend_stores(self):
            raise RuntimeError("everos gone")

    broken = _Broken("acp:a")
    await close_loop(broken)
    # The steps after the failing one still ran: a leaked sandbox executor
    # outlives the process that leaked it.
    assert broken.closed == ["stop", "executor", "mcp"]


async def test_closing_tolerates_an_engine_with_none_of_the_methods():
    await close_loop(object())


async def test_a_cap_below_one_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        _registry(max_loops=0)


def test_the_default_cap_is_bounded():
    assert DEFAULT_MAX_LOOPS >= 1


async def test_get_is_safe_to_call_concurrently_for_one_conversation():
    loops = _registry()
    engines = await asyncio.gather(*(loops.get("acp:a") for _ in range(5)))
    assert len({id(engine) for engine in engines}) == 1
    assert loops.resident() == ("acp:a",)


async def test_an_engine_is_rebuilt_when_its_session_s_tag_moves():
    """The mode-switch seam. Rebuilt at the START of the next turn, which is the
    only moment ``get`` runs -- so a switch costs the turn that is running
    nothing and the next turn gets what was asked for."""
    tags = {"acp:a": "fast"}
    loops = AcpLoops(_StubLoop, tag_for=lambda cid: tags.get(cid, "fast"))
    first = await loops.get("acp:a")

    assert await loops.get("acp:a") is first

    tags["acp:a"] = "deep"
    second = await loops.get("acp:a")

    assert second is not first
    assert await loops.get("acp:a") is second
    # The old engine's close is detached, so the caller of get() is not charged
    # for draining it; aclose is what waits the tracked task out.
    await loops.aclose()
    assert first.closed == ["stop", "drain", "executor", "mcp"]


async def test_a_tag_that_moves_for_one_session_leaves_the_others_resident():
    tags = {"acp:a": "fast", "acp:b": "fast"}
    loops = AcpLoops(_StubLoop, tag_for=lambda cid: tags[cid])
    a, b = await loops.get("acp:a"), await loops.get("acp:b")

    tags["acp:a"] = "ultra"

    assert await loops.get("acp:a") is not a
    assert await loops.get("acp:b") is b


async def test_without_a_tagger_no_engine_is_ever_stale():
    """Every caller before session modes passes none, and the registry has to
    behave exactly as it did for them."""
    loops = _registry()
    first = await loops.get("acp:a")

    assert await loops.get("acp:a") is first
