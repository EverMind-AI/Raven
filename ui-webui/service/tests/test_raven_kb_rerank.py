"""Unit tests for the rerank stage."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import raven_kb_rerank
from raven_kb_rerank import _order_from, _request, rerank

VLLM = {"provider": "vllm", "base_url": "https://host/api/v1/", "model": "cohere/rerank-4-fast"}


def test_the_vllm_request_matches_ravens_own_probe():
    """The onboarding wizard's test button and this call must agree on what a
    working endpoint looks like, or a provider passes the test and fails here."""
    url, body = _request(VLLM, "q", ["a", "b"])

    assert url == "https://host/api/v1/rerank"
    assert body == {"model": "cohere/rerank-4-fast", "query": "q", "documents": ["a", "b"]}


def test_the_dashscope_request_uses_its_nested_shape():
    url, body = _request({**VLLM, "provider": "dashscope", "base_url": "https://ds"}, "q", ["a"])

    assert url == "https://ds/api/v1/services/rerank/text-rerank/text-rerank"
    assert body["input"] == {"query": "q", "documents": ["a"]}


def test_the_deepinfra_request_puts_the_model_in_the_path():
    url, body = _request({**VLLM, "provider": "deepinfra", "base_url": "https://di"}, "q", ["a", "b"])

    assert url == "https://di/cohere/rerank-4-fast"
    assert body == {"queries": ["q", "q"], "documents": ["a", "b"]}


def test_a_vllm_payload_is_read_as_a_ranking():
    payload = {"results": [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]}

    assert _order_from("vllm", payload, 3) == [2, 0]


def test_a_deepinfra_payload_is_sorted_here():
    """It returns scores positionally rather than a ranking."""
    assert _order_from("deepinfra", {"scores": [0.1, 0.9, 0.5]}, 3) == [1, 2, 0]


def test_a_dashscope_payload_is_read_from_its_nested_results():
    payload = {"output": {"results": [{"index": 1}, {"index": 0}]}}

    assert _order_from("dashscope", payload, 2) == [1, 0]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": []},
        {"results": [{"index": 7}]},
        {"results": [{"index": -1}]},
        {"results": [{"score": 1.0}]},
    ],
)
def test_an_unusable_payload_yields_no_opinion(payload):
    """Out-of-range or missing indices would reorder into a KeyError or silently
    drop candidates; the caller keeps the vector order instead."""
    assert _order_from("vllm", payload, 2) is None


def test_a_deepinfra_score_list_of_the_wrong_length_is_rejected():
    assert _order_from("deepinfra", {"scores": [0.1]}, 3) is None


def test_nothing_is_sent_when_the_reranker_is_not_configured(monkeypatch):
    monkeypatch.setattr(raven_kb_rerank, "_everos_rerank", dict)

    assert asyncio.run(rerank("q", ["a", "b"])) is None


def test_a_single_candidate_is_never_sent(monkeypatch):
    """There is nothing to reorder, and the reranker charges per document."""
    sent = []
    monkeypatch.setattr(raven_kb_rerank, "_everos_rerank", lambda: VLLM)
    monkeypatch.setattr(raven_kb_rerank, "_request", lambda *a: sent.append(a) or ("u", {}))

    assert asyncio.run(rerank("q", ["only"])) is None
    assert sent == []


# ------------------------------------------------- send-and-parse, end to end
#
# `_request` and `_order_from` are covered above as units, and the two
# short-circuits below them. What was not covered is the glue between: the call,
# the status check and the handoff to `_order_from`. The module promises that
# "an unexpected payload" leaves the order alone, and that promise lives here.


class _Response:
    def __init__(self, body, status: int = 200):
        self._body = body
        self.status_code = status
        self.text = repr(body)

    def json(self):
        return self._body


def _client_returning(response):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, headers):
            return response

    return lambda *a, **kw: _Client()


def _rerank_against(body, monkeypatch, status: int = 200):
    monkeypatch.setattr(raven_kb_rerank, "_everos_rerank", lambda: VLLM)
    # `rerank` imports httpx inside the function, so the real module is what
    # has to be patched, not an attribute on this one.
    monkeypatch.setattr(httpx, "AsyncClient", _client_returning(_Response(body, status)))
    return asyncio.run(rerank("q", ["a", "b"]))


def test_a_usable_reply_is_read_through_the_whole_call(monkeypatch):
    """The happy path through the glue, so the guards below are not the only
    thing this exercises."""
    body = {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]}

    assert _rerank_against(body, monkeypatch) == [1, 0]


def test_a_top_level_array_body_yields_no_opinion(monkeypatch):
    """A 200 whose body is a JSON array -- from the endpoint or a proxy in front
    of it -- used to reach `payload.get` unguarded and raise out of rerank,
    unwinding past the stage that keeps the vector order."""
    assert _rerank_against([{"index": 0}], monkeypatch) is None


def test_a_dashscope_null_output_yields_no_opinion(monkeypatch):
    """`{"output": null}` is the other shape that gets there: the chained
    `.get("results")` lands on None."""
    monkeypatch.setattr(raven_kb_rerank, "_everos_rerank", lambda: {**VLLM, "provider": "dashscope"})
    monkeypatch.setattr(httpx, "AsyncClient", _client_returning(_Response({"output": None})))

    assert asyncio.run(rerank("q", ["a", "b"])) is None


def test_a_non_200_yields_no_opinion(monkeypatch):
    assert _rerank_against({"results": []}, monkeypatch, status=503) is None
