"""The REST proxy for tool credentials: `/raven/tools`.

These routes carry no logic -- they name a gateway method and hand the body
through -- which is exactly what makes them worth pinning. A wrong path, a
mistyped method name or a dropped `fields` key fails the same way in the
browser: an empty page and no error, because the gateway answers a method
nobody called with nothing.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import raven_config_routes as routes


class _FakeGatewayClient:
    """Records what the route asked the gateway for."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._fail = fail

    async def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if self._fail:
            raise RuntimeError("unknown media tool 'music'")
        return {"ok": True}


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> _FakeGatewayClient:
    client = _FakeGatewayClient()

    async def _shared() -> _FakeGatewayClient:
        return client

    monkeypatch.setattr(routes.GatewayClient, "shared", _shared)
    return client


@pytest.fixture
def app_client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.build_raven_config_router())
    return TestClient(app)


def test_list_reaches_the_gateway_method(gateway: _FakeGatewayClient, app_client: TestClient) -> None:
    r = app_client.get("/raven/tools")
    assert r.status_code == 200
    assert gateway.calls == [("raven.tools.list", {})]


def test_set_carries_the_kind_from_the_path_and_the_fields_from_the_body(
    gateway: _FakeGatewayClient, app_client: TestClient
) -> None:
    r = app_client.put("/raven/tools/image", json={"fields": {"model": "google/gemini-2.5-flash-image"}})
    assert r.status_code == 200
    assert gateway.calls == [
        ("raven.tools.set", {"kind": "image", "fields": {"model": "google/gemini-2.5-flash-image"}}),
    ]


def test_set_without_fields_sends_an_empty_patch_not_none(
    gateway: _FakeGatewayClient, app_client: TestClient
) -> None:
    # The writer indexes `fields`; `None` would raise inside the gateway rather
    # than being the no-op an empty body reads as.
    app_client.put("/raven/tools/image", json={})
    assert gateway.calls[0][1]["fields"] == {}


def test_a_rejected_write_becomes_a_400_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    failing = _FakeGatewayClient(fail=True)

    async def _shared() -> _FakeGatewayClient:
        return failing

    monkeypatch.setattr(routes.GatewayClient, "shared", _shared)
    r = app_client.put("/raven/tools/music", json={"fields": {"model": "m"}})
    assert r.status_code == 400
    assert "music" in r.json()["detail"]
