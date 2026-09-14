"""Peer lookup is by origin, and a credential never travels to an unlisted host."""

import pytest

from raven.a2a_client.peers import auth_headers, resolve_peer
from raven.config.schema import A2aConfig

CONFIG = A2aConfig.model_validate(
    {"peers": [{"origin": "https://peer.example.com", "authScheme": "bearer", "credential": "sekrit"}]}
)


def test_known_origin_resolves_regardless_of_card_path():
    peer = resolve_peer(CONFIG, "https://peer.example.com/.well-known/agent-card.json")
    assert peer is not None and peer.credential == "sekrit"


def test_unknown_origin_resolves_to_none():
    assert resolve_peer(CONFIG, "https://evil.example.com/.well-known/agent-card.json") is None


def test_a_different_port_is_a_different_origin():
    assert resolve_peer(CONFIG, "https://peer.example.com:8443/agent-card.json") is None


def test_headers_carry_the_credential_for_a_known_peer():
    peer = resolve_peer(CONFIG, "https://peer.example.com/x")
    assert auth_headers(peer) == {"Authorization": "Bearer sekrit"}


def test_headers_are_empty_for_an_unknown_peer():
    assert auth_headers(None) == {}


@pytest.mark.parametrize("url", ["not-a-url", "", "file:///etc/passwd"])
def test_unparseable_or_non_http_urls_resolve_to_none(url):
    assert resolve_peer(CONFIG, url) is None
