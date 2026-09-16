"""The agent card declares exactly what this build does, never optimistically."""

import json

from google.protobuf.json_format import MessageToDict

from raven import __version__ as raven_version
from raven.a2a.card import (
    CARD_PATH,
    JSONRPC_BINDING,
    SUBAGENT_EXTENSION_URI,
    build_agent_card,
    build_extended_agent_card,
)
from raven.config.agent_names import GENERIC_AGENT, LEGACY_AGENT_ALIASES
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


def _roster_of(card) -> list[dict]:
    """The sub-agent rows a reader of the extension gets, as it would read them."""
    (extension,) = [e for e in card.capabilities.extensions if e.uri == SUBAGENT_EXTENSION_URI]
    return [dict(row) for row in extension.params["agents"]]


def test_the_extended_card_carries_the_roster_as_a_capability_extension():
    """`AgentExtension` is the only place a conformant card may carry a payload
    the spec does not define -- `AgentCard` is a closed set of protobuf fields,
    so a custom top-level key is rejected outright or silently dropped."""
    agents = [_Agent("Raven-Code", "Writes and edits code."), _Agent("Raven-Design", "Makes visual decks.")]

    card = build_extended_agent_card(A2aConfig(), base_url="/a2a", agents=agents)

    assert _roster_of(card) == [
        {"name": "Raven-Code", "description": "Writes and edits code."},
        {"name": "Raven-Design", "description": "Makes visual decks."},
    ]


def test_the_roster_extension_is_optional_for_a_reader_that_does_not_know_it():
    """`required` false is what lets an ordinary A2A client ignore the entry and
    still talk to this host; true would tell it to refuse instead."""
    card = build_extended_agent_card(A2aConfig(), base_url="/a2a", agents=[_Agent("Raven-Code", "Writes code.")])

    (extension,) = card.capabilities.extensions
    assert extension.uri == SUBAGENT_EXTENSION_URI
    assert extension.required is False


def test_no_sub_agent_is_offered_as_something_to_call():
    """A skill is the protocol's word for what this agent can be *asked to do*,
    and a peer cannot ask for one named sub-agent -- it can only send a message
    to this host. One orchestration skill is what it can actually request.
    """
    agents = [_Agent("Raven-Code", "Writes and edits code."), _Agent("Raven-Design", "Makes visual decks.")]

    card = build_extended_agent_card(A2aConfig(), base_url="/a2a", agents=agents)

    assert [s.id for s in card.skills] == ["general", "subagent-orchestration"]
    assert not any("Raven-Code" in s.name or "Raven-Code" in s.id for s in card.skills)


def test_a_host_with_nothing_to_dispatch_to_claims_neither():
    """An optimistic card is worse than a narrow one: a gateway whose loop has
    not started yet has an empty roster and nothing to orchestrate."""
    card = build_extended_agent_card(A2aConfig(), base_url="/a2a", agents=[])

    assert [s.id for s in card.skills] == ["general"]
    assert list(card.capabilities.extensions) == []


def test_the_public_card_names_no_sub_agent_whatever_the_roster_holds():
    """The whole reason the two cards are separate.

    The public card answers an unauthenticated GET, so the host's installed
    agents must not be derivable from it. This is the assertion that fails if
    someone later folds the derived skills back into `build_agent_card`.
    """
    agents = [_Agent("Raven-Code", "Writes and edits code.")]
    build_extended_agent_card(A2aConfig(), base_url="/a2a", agents=agents)

    public = build_agent_card(A2aConfig(), base_url="/a2a")

    # The whole serialized card, not just its skills: the point is that no field
    # of the unauthenticated document names an installed agent, and an assertion
    # scoped to `skills` would pass again the moment the roster moved elsewhere.
    assert "Raven-Code" not in json.dumps(MessageToDict(public))
    assert list(public.capabilities.extensions) == []


def test_the_extended_card_keeps_the_general_skill():
    """A host that can dispatch is still an agent in its own right, so the
    general entry stays beside the derived ones rather than being replaced."""
    card = build_extended_agent_card(A2aConfig(), base_url="/a2a", agents=[_Agent("X", "y")])

    assert any(s.id == "general" for s in card.skills)


def test_the_extended_card_does_not_name_the_host_as_its_own_sub_agent():
    """The package seed is this host's in-process loop, not something it delegates to.

    A peer already reaches that loop by sending a message to the interface the
    card advertises; naming it in the roster offers a second route to the agent
    the caller is talking to, under a second name.
    """
    agents = [_Agent(GENERIC_AGENT, "Raven's own in-process sub-agent."), _Agent("Raven-Code", "Writes code.")]

    card = build_extended_agent_card(A2aConfig(), base_url="/a2a", agents=agents)

    assert [row["name"] for row in _roster_of(card)] == ["Raven-Code"]


def test_the_seed_is_dropped_under_its_legacy_spelling_too():
    """Matched with `is_builtin_agent_name`, not against `GENERIC_AGENT`.

    Direct-chat records and instance rows written before the row was capitalised
    spell it lowercase, so a roster built from one carries that name and the
    filter has to resolve the alias rather than compare strings.
    """
    (legacy,) = (name for name, seed in LEGACY_AGENT_ALIASES.items() if seed == GENERIC_AGENT)

    card = build_extended_agent_card(A2aConfig(), base_url="/a2a", agents=[_Agent(legacy, "The same row.")])

    assert [s.id for s in card.skills] == ["general"]
    assert list(card.capabilities.extensions) == []
