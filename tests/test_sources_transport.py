"""The request goes out only after the registry has agreed to it.

The point of the transport is the order of the two things: the rule is
consulted first, and the request is made second. A test that only checks the
refusal would still pass if the request were made first and refused after,
so the client here is one that fails the test if it is ever touched.
"""

from __future__ import annotations

import pytest

from raven.sources import PolicyClient, SourceRefusedError, refusal_text, send
from raven.sources.policy import SOURCE_POLICIES


class _Untouched:
    """A client that fails the test if anything is asked of it."""

    def __init__(self) -> None:
        self.called = False

    async def get(self, url, **kwargs):
        self.called = True
        raise AssertionError("the request was made before the registry agreed to it")

    async def post(self, url, **kwargs):
        self.called = True
        raise AssertionError("the request was made before the registry agreed to it")


@pytest.mark.asyncio
async def test_a_refused_source_never_reaches_the_network() -> None:
    client = _Untouched()

    with pytest.raises(SourceRefusedError):
        await send(client, "GET", "https://api.example.test/search", source="linkedin", use="call")

    assert client.called is False


@pytest.mark.asyncio
async def test_a_source_nobody_has_looked_at_never_reaches_the_network() -> None:
    client = _Untouched()

    with pytest.raises(SourceRefusedError):
        await send(client, "GET", "https://api.example.test/search", source="not_a_source", use="call")

    assert client.called is False


@pytest.mark.asyncio
async def test_a_registered_source_is_allowed_through() -> None:
    class _Answering:
        async def get(self, url, **kwargs):
            return "answered"

    answer = await send(_Answering(), "GET", "https://api.tavily.com/search", source="tavily", use="call")
    assert answer == "answered"


@pytest.mark.asyncio
async def test_the_client_carries_the_source_with_it() -> None:
    client = _Untouched()

    with pytest.raises(SourceRefusedError):
        await PolicyClient(client, "linkedin").get("https://api.example.test/x")

    assert client.called is False


def test_the_refusal_names_the_rule_and_the_remedy() -> None:
    from raven.sources import consult

    try:
        consult("linkedin", use="call")
    except SourceRefusedError as refused:
        error, detail = refusal_text(refused)
    else:  # pragma: no cover - the call above always refuses
        raise AssertionError("linkedin was allowed without an opt-in")

    assert error == "linkedin refused by source policy"
    assert "optin" in detail
    assert "linkedin" in detail


def test_every_registered_source_is_reachable_by_its_own_name() -> None:
    for source in SOURCE_POLICIES:
        assert source == source.strip().lower()
        assert " " not in source
