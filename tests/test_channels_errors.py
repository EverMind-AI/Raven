"""Tests for raven.channels.errors — send-error classification feeding the
manager retry layer (transient → re-raise, permanent → swallow)."""

import httpx
import pytest
from websockets.exceptions import WebSocketException

from raven.channels.errors import retryable_http, transient_network


def test_transient_network_builtins():
    assert transient_network(TimeoutError()) is True
    assert transient_network(ConnectionError()) is True
    assert transient_network(ConnectionResetError()) is True   # subclass


def test_transient_network_websockets():
    assert transient_network(WebSocketException()) is True


def test_transient_network_rejects_business_errors():
    assert transient_network(RuntimeError("errcode=1")) is False
    assert transient_network(ValueError("bad payload")) is False


def test_retryable_http_timeouts_and_transport():
    assert retryable_http(httpx.ConnectTimeout("t")) is True
    assert retryable_http(httpx.ConnectError("boom")) is True


def _status_error(code: int) -> httpx.HTTPStatusError:
    resp = httpx.Response(code, request=httpx.Request("GET", "http://x"))
    return httpx.HTTPStatusError("e", request=resp.request, response=resp)


def test_retryable_http_5xx_yes_4xx_no():
    assert retryable_http(_status_error(503)) is True
    assert retryable_http(_status_error(404)) is False
    assert retryable_http(RuntimeError()) is False


@pytest.mark.parametrize("code,expected", [(429, True), (500, True), (403, False)])
def test_slack_transient_classifier(code, expected):
    from types import SimpleNamespace

    from slack_sdk.errors import SlackApiError

    from raven.channels.adapters.slack.channel import _transient_slack

    err = SlackApiError("e", SimpleNamespace(status_code=code))
    assert _transient_slack(err) is expected
