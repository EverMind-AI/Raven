"""The agent card declares exactly what this build does, never optimistically."""

from raven import __version__ as raven_version
from raven.a2a.card import CARD_PATH, JSONRPC_BINDING, build_agent_card, build_extended_agent_card
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


class _Agent:
    """A roster row as the card reads it: a name and a sentence, nothing else."""

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description


def test_the_public_card_does_not_promise_an_extended_one_by_default():
    """`extended_available` reports what this process can actually answer, not
    what the build implements. A process with no roster must say false, or it
    sends a caller to a method that raises ExtendedAgentCardNotConfiguredError.
    """
    assert build_agent_card(A2aConfig(), base_url="/a2a").capabilities.extended_agent_card is False


def test_the_public_card_promises_one_when_a_roster_can_be_derived():
    card = build_agent_card(A2aConfig(), base_url="/a2a", extended_available=True)

    assert card.capabilities.extended_agent_card is True


def test_the_extended_card_names_the_sub_agents_as_skills():
    agents = [_Agent("Raven-Code", "Writes and edits code."), _Agent("Raven-Design", "Makes visual decks.")]

    card = build_extended_agent_card(A2aConfig(), base_url="/a2a", agents=agents)

    named = {s.name for s in card.skills}
    assert {"Raven-Code", "Raven-Design"} <= named
    by_name = {s.name: s for s in card.skills}
    assert by_name["Raven-Code"].description == "Writes and edits code."
    assert by_name["Raven-Code"].id == "subagent:Raven-Code"
    assert "sub-agent" in by_name["Raven-Code"].tags


def test_the_public_card_names_no_sub_agent_whatever_the_roster_holds():
    """The whole reason the two cards are separate.

    The public card answers an unauthenticated GET, so the host's installed
    agents must not be derivable from it. This is the assertion that fails if
    someone later folds the derived skills back into `build_agent_card`.
    """
    agents = [_Agent("Raven-Code", "Writes and edits code.")]
    build_extended_agent_card(A2aConfig(), base_url="/a2a", agents=agents)

    public = build_agent_card(A2aConfig(), base_url="/a2a")

    assert all("Raven-Code" not in s.name for s in public.skills)
    assert all("subagent:" not in s.id for s in public.skills)


def test_the_extended_card_keeps_the_general_skill():
    """A host that can dispatch is still an agent in its own right, so the
    general entry stays beside the derived ones rather than being replaced."""
    card = build_extended_agent_card(A2aConfig(), base_url="/a2a", agents=[_Agent("X", "y")])

    assert any(s.id == "general" for s in card.skills)

