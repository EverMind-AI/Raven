"""EverosCloudBackend over the fake cloud: what goes on the wire, and when."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
import pytest

from raven.contracts.memory import BackendHealth, Memory
from raven.plugins import PluginContext, ServiceLocator
from raven_everos_cloud import backend as mod
from raven_everos_cloud.backend import EverosCloudBackend, resolve_api_key
from tests._everos_cloud_fake import FakeCloud

pytestmark = pytest.mark.everos_cloud

KEY = "ecm-key-7f3a"


def _ctx(tmp_path: Path, config: dict | None = None) -> PluginContext:
    services = ServiceLocator(workspace=tmp_path, user_id="ecm-user", agent_id="ecm-agent")
    return PluginContext(config={"api_key": KEY, **(config or {})}, services=services, logger=logging.getLogger("t"))


def _backend(tmp_path: Path, fake: FakeCloud, config: dict | None = None) -> EverosCloudBackend:
    return EverosCloudBackend(_ctx(tmp_path, config), client=httpx.AsyncClient(transport=fake.transport()))


def _requests(fake: FakeCloud, path_end: str) -> list[dict]:
    return [e for e in fake.ledger if e["path"].endswith(path_end)]


_TURN = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi there"}]


# ── key resolution (C7) ──────────────────────────────────────────────


def test_key_resolution_prefers_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVEROS_CLOUD_API_KEY", "from-env")
    assert resolve_api_key({"api_key": "from-file"}) == ("from-file", "file")
    assert resolve_api_key({}) == ("from-env", "env")
    monkeypatch.delenv("EVEROS_CLOUD_API_KEY")
    assert resolve_api_key({}) == ("", None)


# ── recall (A10, A12, A13) ──────────────────────────────────────────


async def test_recall_sends_bearer_and_include_profile_and_sorts(tmp_path: Path) -> None:
    fake = FakeCloud()
    fake.rows["episodes"] = [
        {"id": "e-low", "summary": "low", "score": 0.3, "session_id": None},
        {"id": "e-high", "summary": "high", "score": 0.9, "session_id": None},
    ]
    b = _backend(tmp_path, fake)
    hits = await b.recall("q", user_id="ecm-user", top_k=5)
    assert [h.metadata["id"] for h in hits] == ["pr-5c1d", "e-high", "e-low"]  # profile sorts first at 1.0
    req = _requests(fake, "/search")[0]
    assert req["headers"]["authorization"] == f"Bearer {KEY}"
    assert req["body"]["include_profile"] is True and req["body"]["user_id"] == "ecm-user"
    assert "agent_id" not in req["body"]


async def test_profile_is_capped_with_marker(tmp_path: Path) -> None:
    fake = FakeCloud()
    fake.rows["profiles"] = [{"id": "p", "profile_data": {f"k{i}": "v" * 50 for i in range(60)}, "score": None}]
    b = _backend(tmp_path, fake)
    hits = await b.recall("q", user_id="ecm-user", top_k=5)
    profile = next(h for h in hits if h.metadata["type"] == "profile")
    assert "[profile truncated," in profile.text and profile.text.endswith("chars omitted]")
    assert len(profile.text) < 1400


async def test_recall_hang_returns_empty_within_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr(mod, "RECALL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(mod, "SESSION_TIMEOUT_S", 0.3)
    fake = FakeCloud(mode="hang")
    b = _backend(tmp_path, fake)
    with caplog.at_level(logging.WARNING, logger="t"):
        t0 = time.monotonic()
        assert await b.recall("q", user_id="ecm-user", top_k=5) == []
        assert 0.3 <= time.monotonic() - t0 < 0.8
        t0 = time.monotonic()
        assert await b.recall_session("s", user_id="ecm-user") == []
        assert 0.3 <= time.monotonic() - t0 < 0.8
    assert "recall timed out after 0.3 s; returning empty" in caplog.text


@pytest.mark.parametrize("mode", ["500", "429"])
async def test_recall_errors_return_empty_and_store_false(tmp_path: Path, mode: str) -> None:
    fake = FakeCloud(mode=mode)
    b = _backend(tmp_path, fake)
    assert await b.recall("q", user_id="ecm-user", top_k=5) == []
    assert await b.store("s", _TURN) is False


async def test_agent_track_maps_skills_and_cases(tmp_path: Path) -> None:
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    hits = await b.recall("q", agent_id="ecm-agent", top_k=5)
    assert {h.metadata["type"] for h in hits} == {"skill", "case"}
    assert all(isinstance(h, Memory) for h in hits)
    body = _requests(fake, "/search")[0]["body"]
    assert body["agent_id"] == "ecm-agent" and "user_id" not in body and "include_profile" not in body


async def test_both_or_neither_owner_is_empty_without_a_request(tmp_path: Path, caplog) -> None:
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    with caplog.at_level(logging.WARNING, logger="t"):
        assert await b.recall("q", top_k=5) == []
        assert await b.recall("q", user_id="u", agent_id="a", top_k=5) == []
    assert fake.ledger == [] and "exactly one of user_id / agent_id" in caplog.text


# ── store (A11, A14, A16, A17) ─────────────────────────────────────


async def test_store_sends_mode_agent_and_owner_ids_and_no_flush(tmp_path: Path) -> None:
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    for _ in range(20):
        assert await b.store("s1", _TURN) is True
    adds = _requests(fake, "/add")
    assert len(adds) == 20 and _requests(fake, "/flush") == []
    body = adds[0]["body"]
    assert body["mode"] == "agent" and body["session_id"] == "s1"
    user, assistant = body["messages"]
    assert user["sender_id"] == "ecm-user" and assistant["sender_id"] == "ecm-agent"
    assert all(isinstance(m["timestamp"], int) and m["timestamp"] > 10**12 for m in body["messages"])


async def test_flush_metadata_flushes_once_with_per_call_owners(tmp_path: Path) -> None:
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    assert await b.store("sub", _TURN, metadata={"flush": True, "user_id": "u2", "agent_id": "a2"}) is True
    adds, flushes = _requests(fake, "/add"), _requests(fake, "/flush")
    assert len(adds) == 1 and len(flushes) == 1
    assert [m["sender_id"] for m in adds[0]["body"]["messages"]] == ["u2", "a2"]
    assert flushes[0]["body"] == {"session_id": "sub"}


async def test_camel_case_owner_override_also_fires(tmp_path: Path) -> None:
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    assert await b.store("sub", _TURN, metadata={"flush": True, "userId": "u2", "agentId": "a2"}) is True
    assert [m["sender_id"] for m in _requests(fake, "/add")[0]["body"]["messages"]] == ["u2", "a2"]
    assert len(_requests(fake, "/flush")) == 1


async def test_is_final_flushes_once_after_the_last_batch(tmp_path: Path) -> None:
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    for _ in range(3):
        assert await b.store("imp", _TURN, metadata={"bulk": True, "is_final": False}) is True
    assert _requests(fake, "/flush") == []
    assert await b.store("imp", _TURN, metadata={"bulk": True, "is_final": True}) is True
    paths = [e["path"] for e in fake.ledger]
    assert paths.count("/api/v2/memory/add") == 4 and paths[-1] == "/api/v2/memory/flush"


async def test_six_hundred_messages_split_into_two_adds(tmp_path: Path) -> None:
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(600)]
    assert await b.store("big", msgs) is True
    adds = _requests(fake, "/add")
    assert [len(a["body"]["messages"]) for a in adds] == [500, 100]
    assert all(a["body"]["mode"] == "agent" for a in adds)


async def test_large_slice_splits_at_a_message_boundary(tmp_path: Path) -> None:
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    msgs = [{"role": "user", "content": "x" * 10_000} for _ in range(40)]  # ~400 KB
    assert await b.store("wide", msgs) is True
    adds = _requests(fake, "/add")
    assert len(adds) >= 2 and sum(len(a["body"]["messages"]) for a in adds) == 40
    assert all(a["status"] == 202 for a in adds)


async def test_oversize_message_fails_its_own_add_and_logs_index(tmp_path: Path, caplog) -> None:
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    msgs = (
        [{"role": "user", "content": "small"}] * 2
        + [{"role": "user", "content": "y" * 300_000}]
        + [{"role": "user", "content": "small"}] * 2
    )
    with caplog.at_level(logging.WARNING, logger="t"):
        assert await b.store("over", msgs) is False
    adds = _requests(fake, "/add")
    assert len(adds) == 1 and len(adds[0]["body"]["messages"]) == 4
    assert "message 2 of session over" in caplog.text


async def test_batches_are_sized_in_utf8_bytes_not_code_points(tmp_path: Path, caplog) -> None:
    """Two-byte characters: 30 messages of 5,000 code points are 300 KB on the wire,
    over the 250 KB ceiling, so the slice must split; counted as code points it
    would go out as one 413."""
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    msgs = [{"role": "user", "content": "\u00e9" * 5_000} for _ in range(30)]
    assert await b.store("wide-utf8", msgs) is True
    adds = _requests(fake, "/add")
    assert len(adds) >= 2 and sum(len(a["body"]["messages"]) for a in adds) == 30
    assert all(a["status"] == 202 for a in adds)
    assert all(len(json.dumps(a["body"], ensure_ascii=False).encode("utf-8")) <= 300_000 for a in adds)

    fake2 = FakeCloud()
    b2 = _backend(tmp_path, fake2)
    big = [{"role": "user", "content": "\u00e9" * 150_000}, {"role": "user", "content": "small"}]
    with caplog.at_level(logging.WARNING, logger="t"):
        assert await b2.store("over-utf8", big) is False
    adds2 = _requests(fake2, "/add")
    assert len(adds2) == 1 and len(adds2[0]["body"]["messages"]) == 1
    assert "message 0 of session over-utf8 is 300" in caplog.text


async def test_a_malformed_flush_timeout_in_the_environment_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C8: store never raises -- a typo in the test hook must not become a ValueError."""
    monkeypatch.setenv("RAVEN_EVEROS_CLOUD_FLUSH_TIMEOUT_S", "5s")
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    assert await b.store("typo", [{"role": "user", "content": "hi"}], metadata={"flush": True}) is True
    assert [r["path"] for r in fake.ledger if r["path"].endswith(("/add", "/flush"))] == [
        "/api/v2/memory/add",
        "/api/v2/memory/flush",
    ]


async def test_a_tool_call_only_turn_is_stored_with_empty_content(tmp_path: Path) -> None:
    """The local plugin's rule: ``None`` content is a tool-call-only turn, not the word None."""
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    msgs = [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        },
    ]
    assert await b.store("tools", msgs) is True
    sent = _requests(fake, "/add")[0]["body"]["messages"]
    assert sent[1]["content"] == "" and sent[1]["role"] == "assistant"


# ── recall_session (A15) ────────────────────────────────────────────


async def test_recall_session_filters_by_session_and_track(tmp_path: Path) -> None:
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    hits = await b.recall_session("sid-1", user_id="u2")
    body = _requests(fake, "/get")[0]["body"]
    assert body == {"memory_type": "episode", "filters": {"session_id": "sid-1"}, "page_size": 100, "user_id": "u2"}
    assert hits and hits[0].metadata["session_id"] == "sid-1" and hits[0].metadata["type"] == "episode"
    # The read-back carries the whole episode under its subject, not the 200-character summary.
    assert hits[0].text == "ECM-EP-5c1d - The reader prefers short answers and names the file they mean."
    await b.recall_session("sid-1", agent_id="a2")
    assert _requests(fake, "/get")[1]["body"]["memory_type"] == "agent_case"


# ── stop (A18, A34) ─────────────────────────────────────────────────


async def test_stop_keeps_sweeping_past_a_refused_flush(tmp_path: Path, caplog) -> None:
    """One 500 at shutdown must not abandon the other sessions' flushes, and the
    warning says what happened rather than blaming the budget."""
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    for sid in ("s1", "s2", "s3"):
        assert await b.store(sid, [{"role": "user", "content": sid}]) is True
    fake.set_mode("500")
    with caplog.at_level(logging.WARNING, logger="t"):
        await b.stop()
    assert [r["body"]["session_id"] for r in _requests(fake, "/flush")] == ["s1", "s2", "s3"]
    assert "3 session(s) left unflushed" in caplog.text and "budget 5.0s exhausted" not in caplog.text


async def test_stop_flushes_only_unflushed_sessions_and_closes(tmp_path: Path, caplog) -> None:
    fake = FakeCloud()
    client = httpx.AsyncClient(transport=fake.transport())
    b = EverosCloudBackend(_ctx(tmp_path), client=client)
    b._owns_client = True
    for sid in ("s1", "s2", "s3"):
        await b.store(sid, _TURN)
    await b.store("s2", _TURN, metadata={"flush": True})
    with caplog.at_level(logging.INFO, logger="t"):
        await b.stop()
    flushed = [e["body"]["session_id"] for e in _requests(fake, "/flush")]
    assert flushed[0] == "s2" and sorted(flushed[1:]) == ["s1", "s3"]
    assert client.is_closed and "flushed 2 session(s) at stop" in caplog.text


async def test_stop_hang_returns_within_budget_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setattr(mod, "SHUTDOWN_FLUSH_BUDGET_S", 0.5)
    fake = FakeCloud()
    b = _backend(tmp_path, fake)
    await b.store("s1", _TURN)
    await b.store("s2", _TURN)
    fake.set_mode("hang", hang_on={"/api/v2/memory/flush"})
    with caplog.at_level(logging.WARNING, logger="t"):
        t0 = time.monotonic()
        await b.stop()
        assert time.monotonic() - t0 < 1.0
    assert "2 session(s) left unflushed" in caplog.text and "['s1', 's2']" in caplog.text


async def test_hung_flush_keeps_the_session_for_the_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAVEN_EVEROS_CLOUD_FLUSH_TIMEOUT_S", "0.3")
    fake = FakeCloud(mode="hang", hang_on={"/api/v2/memory/flush"})
    b = _backend(tmp_path, fake)
    t0 = time.monotonic()
    assert await b.store("sub", _TURN, metadata={"flush": True}) is False
    assert 0.3 <= time.monotonic() - t0 < 0.8
    fake.set_mode("ok")
    await b.stop()
    assert [e["body"]["session_id"] for e in _requests(fake, "/flush")] == ["sub", "sub"]


# ── health (A27) and identity (C4) ──────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "ready", "status", "fragment"),
    [
        ("ok", True, "ok", "http://fake"),
        ("401", False, "missing", "API key rejected (401)"),
        ("403", False, "missing", "refused (403); this may mean the account is not authorized"),
        ("429", True, "degraded", "rate limited by"),
        ("418", False, "missing", "answered 418"),
        ("hang", False, "missing", "cannot reach"),
    ],
)
async def test_health_classifies_every_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, ready: bool, status: str, fragment: str
) -> None:
    monkeypatch.setattr(mod, "HEALTH_TIMEOUT_S", 0.3)
    b = _backend(tmp_path, FakeCloud(mode=mode), config={"base_url": "http://fake"})
    h = await b.health()
    assert isinstance(h, BackendHealth) and h.ready is ready
    assert h.checks[0].status == status and fragment in (h.checks[0].hint or "")


async def test_no_key_means_no_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVEROS_CLOUD_API_KEY", raising=False)
    fake = FakeCloud()
    b = EverosCloudBackend(_ctx(tmp_path, {"api_key": ""}), client=httpx.AsyncClient(transport=fake.transport()))
    assert await b.recall("q", user_id="ecm-user", top_k=5) == []
    assert await b.store("s", _TURN) is False
    h = await b.health()
    assert h.ready is False and "no API key" in (h.checks[0].hint or "")
    assert fake.ledger == []


async def test_stale_identity_in_slice_is_ignored_with_a_warning(tmp_path: Path, caplog) -> None:
    fake = FakeCloud()
    with caplog.at_level(logging.WARNING, logger="t"):
        b = _backend(tmp_path, fake, config={"user_id": "stale-user"})
    assert "user_id='stale-user' is ignored" in caplog.text
    await b.recall("q", user_id="ecm-user", top_k=3)
    assert _requests(fake, "/search")[0]["body"]["user_id"] == "ecm-user"


async def test_delete_is_false_and_feedback_is_quiet(tmp_path: Path) -> None:
    b = _backend(tmp_path, FakeCloud())
    assert await b.delete("x", kind="episode") is False
    await b.feedback({"anything": 1})
