"""Unit tests for the Gate0 subject-endpoint probe (benchmarks.appworld.evolve.precheck).

The probe is what stands between `run`/`check` and burning trials against a
dead or degraded endpoint; each failure mode must map to its own actionable
message (unreachable vs unhealthy vs degraded), because the operator acts on
that text.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

httpx = pytest.importorskip("httpx")

from benchmarks.appworld.evolve.precheck import (  # noqa: E402
    _endpoint_problem,
    _subject_endpoint,
)


class TestSubjectEndpoint:
    def test_reads_api_base_and_model(self, tmp_path):
        cfg = tmp_path / "subject.json"
        cfg.write_text(
            '{"providers": {"custom": {"api_base": "http://h/v1"}},'
            '"agents": {"defaults": {"provider": "custom", "model": "m1"}}}'
        )
        assert _subject_endpoint(cfg) == ("http://h/v1", "m1", None)

    def test_missing_model_is_a_problem(self, tmp_path):
        cfg = tmp_path / "subject.json"
        cfg.write_text(
            '{"providers": {"custom": {"api_base": "http://h/v1"}},"agents": {"defaults": {"provider": "custom"}}}'
        )
        _, _, problem = _subject_endpoint(cfg)
        assert "missing provider api_base/model" in problem

    def test_unreadable_config_is_a_problem(self, tmp_path):
        cfg = tmp_path / "subject.json"
        cfg.write_text("{not json")
        _, _, problem = _subject_endpoint(cfg)
        assert "unreadable" in problem


def _response(status: int = 200, tokens: int = 300):
    return SimpleNamespace(
        status_code=status,
        text="body",
        json=lambda: {"usage": {"completion_tokens": tokens}},
    )


def _probe(monkeypatch, *, post=None, seconds_per_call: float = 1.0, min_tok_per_s: float = 12.0):
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += seconds_per_call
        return clock["t"]

    monkeypatch.setattr("time.monotonic", monotonic)
    monkeypatch.setattr(httpx, "post", post)
    return _endpoint_problem("http://h/v1", "m1", 60.0, min_tok_per_s)


class TestEndpointProblem:
    def test_healthy_endpoint_is_none(self, monkeypatch):
        assert _probe(monkeypatch, post=lambda *a, **k: _response()) is None

    def test_timeout_is_degraded(self, monkeypatch):
        def post(*a, **k):
            raise httpx.TimeoutException("slow")

        problem = _probe(monkeypatch, post=post)
        assert "degraded" in problem and "no 300-token completion" in problem

    def test_connection_error_is_unreachable(self, monkeypatch):
        def post(*a, **k):
            raise httpx.ConnectError("nodename nor servname")

        problem = _probe(monkeypatch, post=post)
        assert "unreachable" in problem and "ConnectError" in problem

    def test_http_error_status_is_unhealthy(self, monkeypatch):
        problem = _probe(monkeypatch, post=lambda *a, **k: _response(status=503))
        assert "unhealthy" in problem and "HTTP 503" in problem

    def test_empty_generation_is_unhealthy(self, monkeypatch):
        problem = _probe(monkeypatch, post=lambda *a, **k: _response(tokens=0))
        assert "empty generation" in problem

    def test_slow_decode_trips_the_throughput_floor(self, monkeypatch):
        # 300 tokens in 30s = 10 tok/s, below the 12 tok/s SOP health bar.
        problem = _probe(monkeypatch, post=lambda *a, **k: _response(), seconds_per_call=30.0)
        assert "degraded" in problem and "tok/s floor" in problem
