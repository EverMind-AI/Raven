"""The agent card declares exactly what this build does, never optimistically."""

from raven import __version__ as raven_version
from raven.a2a.card import CARD_PATH, JSONRPC_BINDING, build_agent_card
from raven.config.schema import A2aConfig


def test_card_path_is_the_1_0_well_known():
    assert CARD_PATH == "/.well-known/agent-card.json"


def test_card_declares_one_jsonrpc_interface_at_1_0():
    card = build_agent_card(A2aConfig(), base_url="https://host.example.com/a2a")
    assert len(card.supported_interfaces) == 1
    iface = card.supported_interfaces[0]
    assert iface.url == "https://host.example.com/a2a"
    assert iface.protocol_version == "1.0"
    # The literal, not JSONRPC_BINDING: the SDK types protocol_binding as a free
    # string with no enum behind it, so nothing but this assertion would notice a
    # wrong binding name going out on the card.
    assert iface.protocol_binding == "JSONRPC"
    assert JSONRPC_BINDING == "JSONRPC"


def test_card_version_is_this_build_not_the_protocol_version():
    card = build_agent_card(A2aConfig(), base_url="https://h/a2a")
    assert card.version == raven_version


def test_capabilities_match_what_is_implemented():
    caps = build_agent_card(A2aConfig(), base_url="https://h/a2a").capabilities
    assert caps.streaming is True
    assert caps.push_notifications is False


def test_card_advertises_at_least_one_skill():
    assert len(build_agent_card(A2aConfig(), base_url="https://h/a2a").skills) >= 1


def test_card_declares_the_enforced_bearer_scheme():
    card = build_agent_card(A2aConfig(), base_url="https://h/a2a")
    assert "http_auth" in card.security_schemes
    scheme = card.security_schemes["http_auth"]
    assert scheme.WhichOneof("scheme") == "http_auth_security_scheme"
    assert scheme.http_auth_security_scheme.scheme == "bearer"
