"""The Memory record: what a sub-agent wrote into everos for one call."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import httpx
import pytest

from raven.agent import subagent_memory as subagent_memory_mod
from raven.agent.subagent_memory import (
    EverosIdentity,
    MemoryItem,
    collect_memories,
    identity_from_config,
    prime_from_turn,
    record_memories,
    trace_session_id,
)
from raven.config.schema import SubagentEverosConfig


class _MockEverOS:
    """Canned /memory/get responses, keyed by the memory_type asked for."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.rows: dict[str, list[dict]] = {"episode": [], "agent_case": []}
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.requests.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "boom"})
        kind = body["memory_type"]
        key = {"episode": "episodes", "agent_case": "agent_cases"}[kind]
        return httpx.Response(200, json={"request_id": "t", "data": {key: self.rows[kind]}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _identity(**kw) -> EverosIdentity:
    return EverosIdentity(
        user_id=kw.get("user_id", "raven-code"),
        agent_id=kw.get("agent_id", "raven-code"),
        base_url="http://everos.test",
        session_prefix="cli:",
        source=kw.get("source", "agent"),
    )


def test_identity_defaults_the_base_url_to_the_hosts() -> None:
    cfg = SubagentEverosConfig(user_id="raven-code")
    identity = identity_from_config(cfg, "http://localhost:18791")
    assert identity is not None
    assert identity.base_url == "http://localhost:18791"
    assert identity.agent_id is None


def test_identity_keeps_its_own_base_url_when_declared() -> None:
    cfg = SubagentEverosConfig(agent_id="raven-code", base_url="http://elsewhere:9/")
    identity = identity_from_config(cfg, "http://localhost:18791")
    assert identity is not None
    assert identity.base_url == "http://elsewhere:9"


def test_no_config_means_no_identity() -> None:
    assert identity_from_config(None, "http://localhost:18791") is None


@pytest.mark.asyncio
async def test_an_episode_becomes_one_item_of_its_summary() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "summary": "Audited the checkout read-only."}]
    async with mock.client() as client:
        items = await collect_memories(client, _identity(), "cli:abc")
    assert items == [MemoryItem(type="episode", text="Audited the checkout read-only.")]


@pytest.mark.asyncio
async def test_a_case_joins_its_intent_and_insight() -> None:
    mock = _MockEverOS()
    mock.rows["agent_case"] = [
        {"id": "c1", "task_intent": "Fix the flaky test", "key_insight": "It raced on the index lock."}
    ]
    async with mock.client() as client:
        items = await collect_memories(client, _identity(user_id=None), "cli:abc")
    assert items == [MemoryItem(type="agent_case", text="Fix the flaky test - It raced on the index lock.")]


@pytest.mark.asyncio
async def test_the_query_carries_the_session_id_and_one_owner_per_call() -> None:
    mock = _MockEverOS()
    async with mock.client() as client:
        await collect_memories(client, _identity(), "cli:abc")
    assert len(mock.requests) == 2
    for body in mock.requests:
        assert body["filters"] == {"session_id": "cli:abc"}
        assert ("user_id" in body) != ("agent_id" in body)
    assert {b["memory_type"] for b in mock.requests} == {"episode", "agent_case"}


@pytest.mark.asyncio
async def test_only_the_declared_owner_is_queried() -> None:
    mock = _MockEverOS()
    async with mock.client() as client:
        await collect_memories(client, _identity(agent_id=None), "cli:abc")
    assert [b["memory_type"] for b in mock.requests] == ["episode"]


@pytest.mark.asyncio
async def test_profile_and_skill_are_never_queried() -> None:
    mock = _MockEverOS()
    async with mock.client() as client:
        await collect_memories(client, _identity(), "cli:abc")
    assert not {b["memory_type"] for b in mock.requests} & {"profile", "agent_skill"}


@pytest.mark.asyncio
async def test_an_item_with_no_usable_text_is_dropped() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "summary": "   "}]
    async with mock.client() as client:
        items = await collect_memories(client, _identity(agent_id=None), "cli:abc")
    assert items == []


@pytest.mark.asyncio
async def test_an_http_error_propagates_to_the_caller() -> None:
    mock = _MockEverOS()
    mock.status = 500
    async with mock.client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await collect_memories(client, _identity(), "cli:abc")


def _sink() -> tuple[list[str], Callable]:
    written: list[str] = []

    async def write(text: str) -> None:
        written.append(text)

    return written, write


async def _key(value: str | None = "cli:abc"):
    return value


async def _true() -> bool:
    return True


@pytest.mark.asyncio
async def test_a_found_memory_is_recorded_as_settled() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "summary": "Ran the audit."}]
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(agent_id=None),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=0.0,
            client=client,
        )
    payload = json.loads(written[0])
    assert payload == {
        "agent": "Raven-Code",
        "source": "agent",
        "status": "settled",
        "memories": [{"type": "episode", "text": "Ran the audit."}],
    }


@pytest.mark.asyncio
async def test_nothing_found_within_the_budget_is_pending() -> None:
    mock = _MockEverOS()
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=0.0,
            client=client,
        )
    payload = json.loads(written[0])
    assert payload["status"] == "pending"
    assert payload["memories"] == []


@pytest.mark.asyncio
async def test_an_unreachable_everos_is_unavailable_not_a_raise() -> None:
    mock = _MockEverOS()
    mock.status = 500
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=0.0,
            client=client,
        )
    assert json.loads(written[0])["status"] == "unavailable"


@pytest.mark.asyncio
async def test_no_join_key_writes_no_file_at_all() -> None:
    mock = _MockEverOS()
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(),
            resolve_session_id=lambda: _key(None),
            write=write,
            budget_s=0.0,
            client=client,
        )
    assert written == []
    assert mock.requests == []


@pytest.mark.asyncio
async def test_polling_stops_once_the_result_stops_growing() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "summary": "First."}]
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(agent_id=None),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=5.0,
            client=client,
        )
    # One poll finds it, a second confirms it stopped growing. No third.
    assert len(mock.requests) == 2
    assert json.loads(written[0])["status"] == "settled"


@pytest.mark.asyncio
async def test_a_failing_write_never_raises_at_the_caller() -> None:
    mock = _MockEverOS()

    async def write(_: str) -> None:
        raise OSError("read-only file system")

    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=0.0,
            client=client,
        )


@pytest.mark.asyncio
async def test_a_failing_resolver_never_raises_at_the_caller() -> None:
    mock = _MockEverOS()
    written, write = _sink()

    async def failing_resolver() -> str | None:
        raise RuntimeError("registry unavailable")

    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(),
            resolve_session_id=failing_resolver,
            write=write,
            budget_s=0.0,
            client=client,
        )
    assert written == []
    assert mock.requests == []


@pytest.mark.asyncio
async def test_a_transient_error_after_growth_keeps_what_was_already_found(monkeypatch) -> None:
    """`unavailable` means nothing was ever read, not that reading stopped.

    A poll whose first two looks saw the result still growing and whose third
    hit a 502 used to discard the two already-collected items and report
    `unavailable`. It must report `settled` with what it already had instead.
    """
    monkeypatch.setattr(subagent_memory_mod, "_BACKOFF_S", (0.01, 0.01, 0.01, 0.01))

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            rows = [{"id": "e1", "summary": "First."}]
        elif calls["n"] == 2:
            rows = [{"id": "e1", "summary": "First."}, {"id": "e2", "summary": "Second."}]
        else:
            return httpx.Response(502, json={"detail": "boom"})
        return httpx.Response(200, json={"request_id": "t", "data": {"episodes": rows}})

    written, write = _sink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(agent_id=None),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=1.0,
            client=client,
        )
    payload = json.loads(written[0])
    assert payload["status"] == "settled"
    assert payload["memories"] == [
        {"type": "episode", "text": "First."},
        {"type": "episode", "text": "Second."},
    ]


@pytest.mark.asyncio
async def test_a_stalled_look_is_cut_off_by_the_remaining_budget() -> None:
    """`budget_s` bounds the whole poll, not just the sleeps between looks.

    A look that runs past its share of the remaining budget must be cut off
    rather than allowed to run to its own, much longer, HTTP timeout -- or a
    stalled everos keeps a recorder alive for minutes past what the budget says.
    """

    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"data": {"episodes": [{"id": "e1", "summary": "First."}]}})
        await asyncio.sleep(5.0)
        return httpx.Response(200, json={"data": {"episodes": []}})

    written, write = _sink()
    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(agent_id=None),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=3.0,
            client=client,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 4.5, "a stalled look must not be allowed to run past the budget"
    payload = json.loads(written[0])
    assert payload["status"] == "settled"
    assert payload["memories"] == [{"type": "episode", "text": "First."}]


# --- the record carries everos's complete text, not its truncated prefix -----
#
# everos's `summary` is a hard 200-character cut of `episode`, mid-word, and its
# `approach` says how a case was solved. An earlier revision recorded `summary`
# alone and dropped both `episode` and `approach`, so the file opened a sentence
# it never finished. Verified against live everos 1.2.1: `summary` is a literal
# prefix of `episode`.


@pytest.mark.asyncio
async def test_an_episode_carries_its_subject_and_full_text() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [
        {
            "id": "e1",
            "subject": "Audit of the checkout",
            "summary": "It began by reading every tracked file and then",
            "episode": "It began by reading every tracked file and then reported the findings in full.",
        }
    ]
    async with mock.client() as client:
        items = await collect_memories(client, _identity(agent_id=None), "cli:abc")
    assert items == [
        MemoryItem(
            type="episode",
            text="Audit of the checkout - It began by reading every tracked file and then reported the findings in full.",
        )
    ]


@pytest.mark.asyncio
async def test_a_case_carries_its_approach_too() -> None:
    mock = _MockEverOS()
    mock.rows["agent_case"] = [
        {
            "id": "c1",
            "task_intent": "Fix the flaky test",
            "approach": "Serialised the two writers behind the index lock",
            "key_insight": "It raced on the index lock",
        }
    ]
    async with mock.client() as client:
        items = await collect_memories(client, _identity(user_id=None), "cli:abc")
    assert items == [
        MemoryItem(
            type="agent_case",
            text=("Fix the flaky test - Serialised the two writers behind the index lock - It raced on the index lock"),
        )
    ]


@pytest.mark.asyncio
async def test_long_text_is_no_longer_capped() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "subject": "s", "episode": "x" * 4000}]
    async with mock.client() as client:
        items = await collect_memories(client, _identity(agent_id=None), "cli:abc")
    # The reader is a sub-agent whose file tool handles length; truncating here
    # only loses the end of what the sub-agent actually concluded.
    assert items[0].text == "s - " + "x" * 4000


@pytest.mark.asyncio
async def test_an_episode_falls_back_when_a_field_is_missing() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "summary": "only a summary survived"}]
    async with mock.client() as client:
        items = await collect_memories(client, _identity(agent_id=None), "cli:abc")
    assert items == [MemoryItem(type="episode", text="only a summary survived")]


# --- the record names the instance, when the call had one --------------------
#
# Every stateful sub-agent now has one (minted when the caller named none), and
# it is the handle a reader passes back as spawn's `instance` to continue that
# same conversation -- actionable, unlike the identity and session id, which
# stay in the log.


@pytest.mark.asyncio
async def test_the_record_names_the_instance() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "subject": "s", "episode": "did the thing"}]
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(agent_id=None),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=0.0,
            client=client,
            instance="audit-a3f9c1",
        )
    payload = json.loads(written[0])
    assert payload["instance"] == "audit-a3f9c1"
    assert list(payload) == ["agent", "instance", "source", "status", "memories"]


@pytest.mark.asyncio
async def test_a_call_with_no_instance_omits_the_key() -> None:
    mock = _MockEverOS()
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=0.0,
            client=client,
        )
    payload = json.loads(written[0])
    # Omitted rather than null: a key that is always there but usually empty
    # costs every reader a check and tells it nothing.
    assert "instance" not in payload
    assert list(payload) == ["agent", "source", "status", "memories"]


def test_identity_carries_source() -> None:
    cfg = SubagentEverosConfig(user_id="liv", agent_id="coder", source="trace")
    identity = identity_from_config(cfg, "http://127.0.0.1:8080")
    assert identity is not None
    assert identity.source == "trace"


def test_identity_source_defaults_to_agent() -> None:
    cfg = SubagentEverosConfig(agent_id="raven-code")
    identity = identity_from_config(cfg, "http://127.0.0.1:8080")
    assert identity is not None
    assert identity.source == "agent"


class TestPrime:
    """`prime` hands everos a conversation before the first read."""

    @pytest.mark.asyncio
    async def test_prime_runs_before_the_first_poll(self) -> None:
        order: list[str] = []

        async def _prime(session_id: str) -> bool:
            order.append(f"prime:{session_id}")
            return True

        def _handler(request: httpx.Request) -> httpx.Response:
            order.append("read")
            return httpx.Response(200, json={"data": {"episodes": []}})

        written, write = _sink()
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            await record_memories(
                agent="Coder",
                identity=_identity(source="trace"),
                resolve_session_id=lambda: _key("trace:Coder:c1"),
                write=write,
                prime=_prime,
                budget_s=0.0,
                client=client,
            )
        assert order[0] == "prime:trace:Coder:c1"
        assert "read" in order

    @pytest.mark.asyncio
    async def test_prime_returning_false_skips_the_poll(self) -> None:
        reads = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal reads
            reads += 1
            return httpx.Response(200, json={"data": {"episodes": []}})

        async def _prime(session_id: str) -> bool:
            return False

        written, write = _sink()
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            await record_memories(
                agent="Coder",
                identity=_identity(source="trace"),
                resolve_session_id=lambda: _key("trace:Coder:c1"),
                write=write,
                prime=_prime,
                client=client,
            )
        # Nothing landed, so nothing can have been extracted: polling would
        # spend the whole budget confirming an absence already known.
        assert reads == 0
        assert json.loads(written[0])["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_prime_raising_is_caught(self) -> None:
        reads = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal reads
            reads += 1
            return httpx.Response(500)

        async def _prime(session_id: str) -> bool:
            raise RuntimeError("everos refused the write")

        written, write = _sink()
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            await record_memories(
                agent="Coder",
                identity=_identity(source="trace"),
                resolve_session_id=lambda: _key("trace:Coder:c1"),
                write=write,
                prime=_prime,
                client=client,
            )
        # A poll that ran at all would hit this same 500 transport and also
        # convert to "unavailable", so the status alone cannot tell skipped
        # from ran-and-failed; the read count is what actually pins it.
        assert reads == 0
        assert json.loads(written[0])["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_no_prime_leaves_the_existing_path_alone(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"episodes": [{"subject": "s", "episode": "e"}]}})

        written, write = _sink()
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            await record_memories(
                agent="Raven-Code",
                identity=_identity(source="agent"),
                resolve_session_id=lambda: _key(),
                write=write,
                budget_s=0.0,
                client=client,
            )
        record = json.loads(written[0])
        assert record["status"] == "settled"
        assert record["source"] == "agent"
        assert record["memories"] == [{"type": "episode", "text": "s - e"}]

    @pytest.mark.asyncio
    async def test_record_names_its_source(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"episodes": [{"subject": "s", "episode": "e"}]}})

        written, write = _sink()
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            await record_memories(
                agent="Coder",
                identity=_identity(source="trace"),
                resolve_session_id=lambda: _key("trace:Coder:c1"),
                write=write,
                prime=lambda _s: _true(),
                budget_s=0.0,
                client=client,
            )
        assert json.loads(written[0])["source"] == "trace"


class TestTraceSessionId:
    def test_id_is_its_own_namespace(self) -> None:
        # Not the configured session_prefix: that documents itself as a fork
        # launcher's convention, and stamping `cli:` on an acp agent's memories
        # would name a transport that was never involved.
        assert trace_session_id("Coder", "c1") == "trace:Coder:c1"

    def test_id_is_unique_per_call(self) -> None:
        assert trace_session_id("Coder", "c1") != trace_session_id("Coder", "c2")


class TestMonotonic:
    """`_monotonic` only moves the first row's clock, and only when it would sort last."""

    def test_an_already_ordered_turn_passes_through_untouched(self) -> None:
        turn = [
            {"role": "user", "content": "read it", "timestamp": "2026-08-20T09:50:00.000000"},
            {"role": "assistant", "content": "step one", "timestamp": "2026-08-20T09:51:00.000000"},
            {"role": "assistant", "content": "step two", "timestamp": "2026-08-20T09:50:30.000000"},
        ]
        result = subagent_memory_mod._monotonic(turn)
        assert result[0]["timestamp"] == "2026-08-20T09:50:00.000000"
        assert result[1]["timestamp"] == "2026-08-20T09:51:00.000000"
        assert result[2]["timestamp"] == "2026-08-20T09:50:30.000000"


class TestPrimeFromTurn:
    """The turn reaches everos as a conversation under this agent's identity."""

    @pytest.mark.asyncio
    async def test_posts_add_then_flush(self) -> None:
        seen: list[tuple[str, dict]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            landed = await prime_from_turn(
                identity=_identity(source="trace", user_id="liv", agent_id="coder"),
                session_id="trace:Coder:c1",
                turn=[
                    {"role": "user", "content": "read it", "timestamp": "2026-08-20T09:53:46.693637"},
                    {"role": "assistant", "content": "reading", "timestamp": "2026-08-20T09:51:56.109124"},
                ],
                client=client,
            )
        assert landed is True
        assert [path for path, _ in seen] == ["/api/v2/memory/add", "/api/v2/memory/flush"]
        body = seen[0][1]
        assert body["session_id"] == "trace:Coder:c1"
        # Distinct ids so this fails if user/agent routing is inverted or dropped.
        assert [m["sender_id"] for m in body["messages"]] == ["liv", "coder"]

    @pytest.mark.asyncio
    async def test_prompt_is_never_timestamped_after_the_work(self) -> None:
        # append_turn stamps the user row when the turn ends, so its clock is
        # later than the work it caused. List order is right; the clock is not.
        # A consumer that sorts by timestamp would read the prompt last.
        sent: list[dict] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            await prime_from_turn(
                identity=_identity(source="trace"),
                session_id="trace:Coder:c1",
                turn=[
                    {"role": "user", "content": "read it", "timestamp": "2026-08-20T09:53:46.693637"},
                    {"role": "assistant", "content": "reading", "timestamp": "2026-08-20T09:51:56.109124"},
                ],
                client=client,
            )
        stamps = [m["timestamp"] for m in sent[0]["messages"]]
        assert stamps == sorted(stamps)
        assert stamps[0] < stamps[1]

    @pytest.mark.asyncio
    async def test_empty_turn_writes_nothing(self) -> None:
        calls = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            landed = await prime_from_turn(identity=_identity(source="trace"), session_id="s", turn=[], client=client)
        assert landed is False
        assert calls == 0

    @pytest.mark.asyncio
    async def test_a_turn_that_converts_to_nothing_writes_nothing(self) -> None:
        calls = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            landed = await prime_from_turn(
                identity=_identity(source="trace"),
                session_id="s",
                turn=[{"role": "system", "content": "dropped"}],
                client=client,
            )
        assert landed is False
        assert calls == 0

    @pytest.mark.asyncio
    async def test_a_failed_write_is_false_not_an_exception(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, json={}))) as client:
            landed = await prime_from_turn(
                identity=_identity(source="trace"),
                session_id="s",
                turn=[{"role": "user", "content": "hi"}],
                client=client,
            )
        assert landed is False

    @pytest.mark.asyncio
    async def test_a_failed_flush_is_false_even_though_add_landed(self) -> None:
        # add and flush are two independent calls now; a flush failure must not
        # be masked by an add that already succeeded.
        def _handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v2/memory/add":
                return httpx.Response(200, json={})
            return httpx.Response(500, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            landed = await prime_from_turn(
                identity=_identity(source="trace"),
                session_id="s",
                turn=[{"role": "user", "content": "hi"}],
                client=client,
            )
        assert landed is False

    @pytest.mark.asyncio
    async def test_a_missing_user_id_writes_nothing(self) -> None:
        calls = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            landed = await prime_from_turn(
                identity=_identity(source="trace", user_id=None),
                session_id="s",
                turn=[{"role": "user", "content": "hi"}],
                client=client,
            )
        assert landed is False
        assert calls == 0

    @pytest.mark.asyncio
    async def test_a_missing_agent_id_writes_nothing(self) -> None:
        calls = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            landed = await prime_from_turn(
                identity=_identity(source="trace", agent_id=None),
                session_id="s",
                turn=[{"role": "user", "content": "hi"}],
                client=client,
            )
        assert landed is False
        assert calls == 0
