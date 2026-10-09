"""The fake cloud honours or refuses every field the backend can send.

A fake that silently accepted a field would hide that dimension from every test
built on it, so this file pins the field table the acceptance document lists.
"""

from __future__ import annotations

import json

import httpx

from tests._everos_cloud_fake import EPISODE_SUBJECT, MODE_ROUTE, REFUSED_MESSAGE, TOTALS, FakeCloud

_MSG = {"sender_id": "u", "role": "user", "timestamp": 1_700_000_000_000, "content": "x"}


async def _post(fake: FakeCloud, path: str, body: dict, key: str | None = "k") -> httpx.Response:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(transport=fake.transport(), base_url="http://fake") as client:
        return await client.post(path, json=body, headers=headers)


async def test_search_requires_exactly_one_owner() -> None:
    fake = FakeCloud()
    r = await _post(fake, "/api/v2/memory/search", {"query": "q", "top_k": 5})
    assert r.status_code == 422 and "user_id" in r.text and "agent_id" in r.text
    r = await _post(fake, "/api/v2/memory/search", {"query": "q", "top_k": 5, "user_id": "u", "agent_id": "a"})
    assert r.status_code == 422


async def test_agent_rows_only_for_an_agent_owner() -> None:
    fake = FakeCloud()
    r = await _post(fake, "/api/v2/memory/search", {"query": "q", "top_k": 5, "user_id": "u"})
    data = r.json()["data"]
    assert data["agent_skills"] == [] and data["agent_cases"] == []
    assert data["episodes"][0]["subject"] == EPISODE_SUBJECT
    r = await _post(fake, "/api/v2/memory/search", {"query": "q", "top_k": 5, "agent_id": "a"})
    data = r.json()["data"]
    assert data["episodes"] == [] and data["agent_skills"] and data["agent_cases"]


async def test_profile_only_when_asked_for() -> None:
    fake = FakeCloud()
    r = await _post(fake, "/api/v2/memory/search", {"query": "q", "top_k": 5, "user_id": "u"})
    assert r.json()["data"]["profiles"] == []
    r = await _post(fake, "/api/v2/memory/search", {"query": "q", "top_k": 5, "user_id": "u", "include_profile": True})
    assert r.json()["data"]["profiles"][0]["id"] == "pr-5c1d"


async def test_unknown_field_is_refused_by_name() -> None:
    r = await _post(FakeCloud(), "/api/v2/memory/search", {"query": "q", "top_k": 5, "user_id": "u", "bogus": 1})
    assert r.status_code == 400 and "bogus" in r.text


async def test_get_memory_type_must_match_the_owner_track() -> None:
    fake = FakeCloud()
    r = await _post(fake, "/api/v2/memory/get", {"user_id": "u", "memory_type": "agent_case", "page_size": 1})
    assert r.status_code == 422 and "agent_case" in r.text
    r = await _post(fake, "/api/v2/memory/get", {"user_id": "u", "memory_type": "episode", "page_size": 1})
    assert r.status_code == 200 and r.json()["data"]["total_count"] == TOTALS["episode"]


async def test_session_filter_fills_wildcard_rows_and_drops_others() -> None:
    fake = FakeCloud()
    fake.rows["episodes"].append({**fake.rows["episodes"][0], "id": "ep-other", "session_id": "other"})
    r = await _post(
        fake, "/api/v2/memory/get", {"user_id": "u", "memory_type": "episode", "filters": {"session_id": "s1"}}
    )
    rows = r.json()["data"]["episodes"]
    assert [row["id"] for row in rows] == ["ep-5c1d"] and rows[0]["session_id"] == "s1"


async def test_add_limits_and_message_shape() -> None:
    fake = FakeCloud()
    r = await _post(fake, "/api/v2/memory/add", {"session_id": "s", "mode": "agent", "messages": [_MSG] * 501})
    assert r.status_code == 413
    r = await _post(
        fake, "/api/v2/memory/add", {"session_id": "s", "mode": "agent", "messages": [{**_MSG, "timestamp": "2026"}]}
    )
    assert r.status_code == 400 and "timestamp" in r.text
    r = await _post(
        fake, "/api/v2/memory/add", {"session_id": "s", "mode": "agent", "messages": [{**_MSG, "extra": 1}]}
    )
    assert r.status_code == 400 and "extra" in r.text
    r = await _post(fake, "/api/v2/memory/add", {"session_id": "s", "mode": "agent", "messages": [_MSG]})
    assert r.status_code == 202 and r.json()["data"]["status"] == "queued"


async def test_flush_reports_whether_anything_was_pending() -> None:
    fake = FakeCloud()
    r = await _post(fake, "/api/v2/memory/flush", {"session_id": "s"})
    assert r.json()["data"]["status"] == "no_extraction"
    await _post(fake, "/api/v2/memory/add", {"session_id": "s", "mode": "agent", "messages": [_MSG]})
    r = await _post(fake, "/api/v2/memory/flush", {"session_id": "s"})
    assert r.json()["data"]["status"] == "extracted"


async def test_missing_bearer_is_401_in_ok_mode() -> None:
    r = await _post(
        FakeCloud(), "/api/v2/memory/get", {"user_id": "u", "memory_type": "episode", "page_size": 1}, key=None
    )
    assert r.status_code == 401


async def test_ledger_records_unknown_routes_too() -> None:
    fake = FakeCloud()
    await _post(fake, "/health", {})
    assert fake.ledger[-1]["path"] == "/health" and fake.ledger[-1]["status"] == 404


async def test_mode_switch_changes_the_answer() -> None:
    fake = FakeCloud()
    r = await _post(fake, "/_fake/mode", {"mode": "401"}, key=None)
    assert r.status_code == 200
    r = await _post(fake, "/api/v2/memory/get", {"user_id": "u", "memory_type": "episode", "page_size": 1})
    assert r.status_code == 401 and r.json()["error"]["message"] == REFUSED_MESSAGE
    fake.set_mode("empty")
    r = await _post(fake, "/api/v2/memory/get", {"user_id": "u", "memory_type": "episode", "page_size": 1})
    assert r.json()["data"]["total_count"] == 0 and r.json()["data"]["episodes"] == []


def test_http_face_serves_the_same_handler(tmp_path) -> None:
    import socket
    import threading
    import urllib.request

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    fake = FakeCloud(ledger_path=tmp_path / "requests.jsonl")
    server = fake.serve(port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v2/memory/get",
            data=json.dumps({"user_id": "u", "memory_type": "episode", "page_size": 1}).encode(),
            headers={"Authorization": "Bearer k", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["data"]["total_count"] == TOTALS["episode"]
        lines = (tmp_path / "requests.jsonl").read_text().splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["headers"]["authorization"] == "Bearer k"
    finally:
        server.shutdown()
        server.server_close()


def test_control_route_answers_while_everything_else_hangs() -> None:
    fake = FakeCloud(mode="hang")
    assert fake.hangs("/api/v2/memory/search") and fake.hangs("/api/v2/memory/flush")
    assert not fake.hangs(MODE_ROUTE)
    status, _ = fake.handle("POST", MODE_ROUTE, {}, json.dumps({"mode": "ok"}).encode())
    assert status == 200 and fake.mode == "ok"


def test_auth_off_admits_a_request_without_a_key_as_the_oss_server_would() -> None:
    fake = FakeCloud()
    body = json.dumps({"memory_type": "episode", "user_id": "u", "page_size": 1}).encode()
    assert fake.handle("POST", "/api/v2/memory/get", {}, body)[0] == 401
    status, data = fake.handle("POST", MODE_ROUTE, {}, json.dumps({"mode": "ok", "auth": False}).encode())
    assert status == 200 and data["data"]["auth"] is False
    assert fake.handle("POST", "/api/v2/memory/get", {}, body)[0] == 200
