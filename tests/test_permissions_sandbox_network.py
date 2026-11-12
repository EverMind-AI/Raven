"""What a sandbox network denial tells the model, and what it leaves on the turn's record."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from raven.config.schema import NetworkPermissions, NetworkRule, PermissionsConfig
from raven.contracts.permissions import ApprovalChoice, ApprovalOutcome
from raven.permissions import sandbox_network
from raven.permissions import session as session_module
from raven.permissions.builtin import BuiltinRulings
from raven.permissions.gate import PermissionGate
from raven.permissions.network_grants import GrantRegistry
from raven.permissions.runtime import tool_body
from raven.permissions.sandbox_network import handle_denials
from raven.permissions.turn import start_permission_turn
from raven.sandbox.denials import NetworkDenial
from raven.sandbox.network_policy import NetworkGrant

CURL = NetworkDenial("/usr/bin/curl", "example.com", 443, "transparent_tcp_policy_denied")
ORG = NetworkDenial("/usr/bin/curl", "example.org", 443, "transparent_tcp_policy_denied")
CURL_GRANT = NetworkGrant("/usr/bin/curl", "example.com", 443)
ORG_GRANT = NetworkGrant("/usr/bin/curl", "example.org", 443)


async def test_a_blocked_target_is_named_in_the_tool_output():
    start_permission_turn(None, conversation_id="c", turn_id="t")
    text = (await handle_denials(None, [CURL], command="curl https://example.com")).text
    assert text == (
        "\n\n[sandbox] The sandbox network policy blocked: /usr/bin/curl -> example.com:443. "
        "This is a policy block, not a network failure. Tell the user; it can be allowed under permissions.network."
    )


async def test_every_blocked_target_is_named_once():
    start_permission_turn(None, conversation_id="c", turn_id="t")
    other = NetworkDenial("/usr/bin/curl", "example.org", 443, "transparent_tcp_policy_denied")
    text = (await handle_denials(None, [CURL, other], command="curl a b")).text
    assert "/usr/bin/curl -> example.com:443, /usr/bin/curl -> example.org:443." in text


async def test_a_one_shot_run_lists_the_blocked_target_among_refusals():
    turn = start_permission_turn(None, conversation_id="c", turn_id="t")
    await handle_denials(None, [CURL], command="curl https://example.com")
    (refusal,) = turn.refusals
    assert (refusal.tool_name, refusal.action, refusal.source) == (
        "exec",
        "/usr/bin/curl -> example.com:443",
        "sandbox_network",
    )
    assert refusal.reason == "blocked by the sandbox network policy"


async def test_no_denials_leave_the_output_unchanged():
    turn = start_permission_turn(None, conversation_id="c", turn_id="t")
    assert (await handle_denials(None, [], command="true")).text == ""
    assert turn.refusals == []


async def test_a_flood_of_denials_names_ten_and_counts_the_rest():
    turn = start_permission_turn(None, conversation_id="c", turn_id="t")
    flood = [NetworkDenial("/usr/bin/curl", f"h{i}.example.com", 443, "x") for i in range(50)]
    text = (await handle_denials(None, flood, command="for h in ...; do curl $h; done")).text
    assert "h9.example.com:443" in text and "h10.example.com" not in text
    assert "and 40 more" in text
    assert len(text) < 2500
    assert len(turn.refusals) == 11
    assert turn.refusals[-1].action == "and 40 more blocked targets"


async def test_a_long_target_line_is_shortened():
    turn = start_permission_turn(None, conversation_id="c", turn_id="t")
    long_host = "a" * 60 + "." + "b" * 60 + "." + "c" * 60 + ".example.com"
    text = (await handle_denials(None, [NetworkDenial("/usr/bin/curl", long_host, 443, "x")], command="curl")).text
    (refusal,) = turn.refusals
    assert len(refusal.action) <= 160 and refusal.action.endswith("...")
    assert refusal.action in text


class FakeSandbox:
    def __init__(self):
        self.granted: list[list[NetworkGrant]] = []
        self.synced = 0

    async def grant_network(self, grants):
        self.granted.append(list(grants))
        return True

    async def sync_network(self):
        self.synced += 1
        return True


class Responder:
    def __init__(self, choice: ApprovalChoice, **outcome: Any):
        self.outcome = ApprovalOutcome(choice, **outcome)
        self.calls: list[dict[str, Any]] = []

    async def await_approval(self, **kwargs: Any) -> ApprovalOutcome:
        self.calls.append(kwargs)
        return self.outcome


@pytest.fixture
def grants(monkeypatch) -> GrantRegistry:
    registry = GrantRegistry()
    monkeypatch.setattr(sandbox_network, "GRANTS", registry)
    monkeypatch.setattr(session_module, "_GRANTS", {})
    return registry


async def _handle(responder, denials, sandbox, *, config=None, conversation=None, judge=None):
    turn = start_permission_turn(
        responder, conversation_id=f"conv-{uuid4().hex}" if conversation is None else conversation, turn_id="t"
    )
    gate = PermissionGate(
        config_source=lambda: config or PermissionsConfig(), builtin=BuiltinRulings(), judge_provider_for=judge
    )
    with tool_body(gate):
        notice = await handle_denials(sandbox, denials, command="curl a b")
    return notice, turn


async def test_one_prompt_covers_every_target_of_a_command(grants):
    responder, sandbox = Responder(ApprovalChoice.ALLOW), FakeSandbox()
    await _handle(responder, [CURL, ORG], sandbox)
    (call,) = responder.calls
    assert call["kind"] == "sandbox.network"
    assert [target["host"] for target in call["evidence"]["targets"]] == ["example.com", "example.org"]
    assert call["evidence"]["command"] == "curl a b"
    # No card offers a rule to save for a connection: its undo and its editor
    # are the exec pattern's, so the broader grant offered is the session.
    assert call["suggested_pattern"] == ""
    assert sandbox.granted == [[CURL_GRANT, ORG_GRANT]]


async def test_an_approved_target_tells_the_model_to_run_the_command_again(grants):
    notice, _ = await _handle(Responder(ApprovalChoice.ALLOW), [CURL], FakeSandbox())
    assert (
        notice.text
        == "\n\n[sandbox] Network access for /usr/bin/curl -> example.com:443 was approved; run the command again."
    )
    assert notice.stop is False


async def test_a_deny_rule_is_never_asked(grants):
    config = PermissionsConfig(network=NetworkPermissions(rules=[NetworkRule(host="example.com", tier="deny")]))
    responder = Responder(ApprovalChoice.ALLOW)
    notice, turn = await _handle(responder, [CURL, ORG], FakeSandbox(), config=config)
    (call,) = responder.calls
    assert [target["host"] for target in call["evidence"]["targets"]] == ["example.org"]
    assert "Your network rules refuse /usr/bin/curl -> example.com:443" in notice.text
    assert [(r.action, r.source) for r in turn.refusals] == [("/usr/bin/curl -> example.com:443", "user_deny")]


async def test_targets_every_one_refused_by_a_rule_ask_nothing(grants):
    config = PermissionsConfig(network=NetworkPermissions(rules=[NetworkRule(host="example.com", tier="deny")]))
    responder = Responder(ApprovalChoice.ALLOW)
    notice, _ = await _handle(responder, [CURL], FakeSandbox(), config=config)
    assert responder.calls == []
    assert "approved" not in notice.text


async def test_a_session_grant_reaches_every_sandbox_in_scope(grants):
    conversation = f"conv-{uuid4().hex}"
    trigger, shared, mine, other = FakeSandbox(), FakeSandbox(), FakeSandbox(), FakeSandbox()
    grants.register(shared, None)
    grants.register(mine, conversation)
    grants.register(other, "another-conversation")
    await _handle(Responder(ApprovalChoice.ALLOW_SESSION), [CURL], trigger, conversation=conversation)
    assert grants.grants_for(conversation) == frozenset({CURL_GRANT})
    assert trigger.granted == [[CURL_GRANT]]
    assert (shared.synced, mine.synced, other.synced) == (1, 1, 0)


async def test_allow_once_reaches_this_sandbox_only(grants):
    trigger, shared = FakeSandbox(), FakeSandbox()
    grants.register(shared, None)
    await _handle(Responder(ApprovalChoice.ALLOW), [CURL], trigger)
    assert trigger.granted == [[CURL_GRANT]]
    assert grants.grants_for(None) == frozenset() and shared.synced == 0


async def test_allow_always_writes_one_rule_per_target(grants, monkeypatch):
    written = []
    monkeypatch.setattr(
        sandbox_network, "allow_network_rule", lambda host, port, binary: written.append((host, port, binary)) or True
    )
    trigger = FakeSandbox()
    await _handle(Responder(ApprovalChoice.ALLOW_ALWAYS), [CURL, ORG], trigger)
    assert written == [("example.com", 443, "/usr/bin/curl"), ("example.org", 443, "/usr/bin/curl")]
    assert trigger.granted == [[CURL_GRANT, ORG_GRANT]]


async def test_a_rule_that_cannot_be_written_still_holds_for_the_session(grants, monkeypatch):
    def refuse(host, port, binary):
        raise ValueError("permissions.network.rules is not a list")

    monkeypatch.setattr(sandbox_network, "allow_network_rule", refuse)
    conversation = f"conv-{uuid4().hex}"
    await _handle(Responder(ApprovalChoice.ALLOW_ALWAYS), [CURL], FakeSandbox(), conversation=conversation)
    assert grants.grants_for(conversation) == frozenset({CURL_GRANT})


async def test_a_declined_target_tells_the_model_not_to_go_around(grants):
    sandbox = FakeSandbox()
    notice, _ = await _handle(Responder(ApprovalChoice.DENY, feedback="use the internal mirror"), [CURL], sandbox)
    assert notice.text == (
        "\n\n[sandbox] The user declined network access to /usr/bin/curl -> example.com:443; "
        'do not try another route to them. The user said: "use the internal mirror"'
    )
    assert sandbox.granted == [] and notice.stop is False


async def test_deny_and_stop_ends_the_turn(grants):
    notice, _ = await _handle(Responder(ApprovalChoice.DENY_STOP), [CURL], FakeSandbox())
    assert notice.stop is True
    assert "declined network access to /usr/bin/curl -> example.com:443 and asked to stop here" in notice.text


async def test_a_request_nobody_answered_is_not_told_as_a_refusal(grants):
    notice, _ = await _handle(Responder(ApprovalChoice.DENY, answered=False), [CURL], FakeSandbox())
    assert "expired with no answer" in notice.text and "declined" not in notice.text


async def test_an_unattended_turn_names_the_block_and_where_to_allow_it(grants):
    notice, turn = await _handle(None, [CURL], FakeSandbox())
    assert notice.text == (
        "\n\n[sandbox] The sandbox network policy blocked: /usr/bin/curl -> example.com:443. "
        "This is a policy block, not a network failure. Tell the user; it can be allowed under permissions.network."
    )
    assert [r.source for r in turn.refusals] == ["unattended"]


async def test_a_refusal_with_no_program_or_port_is_named_and_not_asked(grants):
    responder = Responder(ApprovalChoice.ALLOW)
    dns = NetworkDenial("", "example.net", None, "policy_dns_ineligible")
    notice, _ = await _handle(responder, [dns], FakeSandbox())
    assert responder.calls == []
    assert "The sandbox network policy blocked: example.net." in notice.text


async def test_full_mode_without_a_conversation_grants_this_sandbox(grants):
    trigger = FakeSandbox()
    await _handle(None, [CURL], trigger, config=PermissionsConfig(mode="full"), conversation="")
    assert trigger.granted == [[CURL_GRANT]]
    assert grants.grants_for(None) == frozenset()


async def test_an_address_given_as_an_ip_is_asked_even_in_full_mode(grants):
    metadata = NetworkDenial("/usr/bin/curl", "169.254.169.254", 80, "transparent_tcp_policy_denied")
    responder, sandbox = Responder(ApprovalChoice.DENY), FakeSandbox()
    await _handle(responder, [metadata], sandbox, config=PermissionsConfig(mode="full"))
    assert len(responder.calls) == 1 and sandbox.granted == []


async def test_an_address_given_as_an_ip_stays_blocked_with_nobody_to_ask(grants):
    metadata = NetworkDenial("/usr/bin/curl", "169.254.169.254", 80, "transparent_tcp_policy_denied")
    sandbox = FakeSandbox()
    notice, _ = await _handle(None, [metadata], sandbox, config=PermissionsConfig(mode="full"))
    assert sandbox.granted == []
    assert "The sandbox network policy blocked: /usr/bin/curl -> 169.254.169.254:80." in notice.text


async def test_the_prompt_says_when_the_sandbox_is_every_conversations(grants):
    shared, own = FakeSandbox(), FakeSandbox()
    grants.register(shared, None)
    responder = Responder(ApprovalChoice.DENY)
    await _handle(responder, [CURL], shared)
    await _handle(responder, [CURL], own)
    assert [call["evidence"]["shared"] for call in responder.calls] == [True, False]


async def test_smart_mode_lets_the_reviewer_open_what_the_command_needs(grants, monkeypatch):
    from raven.permissions.judge import JudgeOutcome

    seen: dict[str, Any] = {}

    async def fake_review(provider, *, targets, command, model, timeout_s):
        seen.update(targets=targets, command=command)
        return JudgeOutcome(allow=True, reason="the install needs its index")

    monkeypatch.setattr(sandbox_network, "review_network", fake_review)
    responder, sandbox = Responder(ApprovalChoice.DENY), FakeSandbox()
    await _handle(responder, [CURL], sandbox, config=PermissionsConfig(mode="smart"), judge=lambda: object())
    assert responder.calls == [] and sandbox.granted == [[CURL_GRANT]]
    assert seen["command"] == "curl a b" and seen["targets"][0]["host"] == "example.com"


async def test_smart_mode_with_no_reviewer_asks_the_person(grants):
    responder = Responder(ApprovalChoice.DENY)
    await _handle(responder, [CURL], FakeSandbox(), config=PermissionsConfig(mode="smart"))
    assert len(responder.calls) == 1


class HeldResponder:
    """Answers only when released, so a second request can arrive while the first is open."""

    def __init__(self, choice: ApprovalChoice, **outcome: Any):
        import asyncio

        self.outcome = ApprovalOutcome(choice, **outcome)
        self.calls: list[dict[str, Any]] = []
        self.release = asyncio.Event()

    async def await_approval(self, **kwargs: Any) -> ApprovalOutcome:
        self.calls.append(kwargs)
        await self.release.wait()
        return self.outcome


async def _concurrently(responder, first, second, *, conversation: str):
    """Two agents of one conversation meet blocked connections at about the same time."""
    import asyncio

    gate = PermissionGate(config_source=lambda: PermissionsConfig(), builtin=BuiltinRulings())

    async def run(sandbox, denials):
        start_permission_turn(responder, conversation_id=conversation, turn_id=f"t-{uuid4().hex}")
        with tool_body(gate):
            return await handle_denials(sandbox, denials, command="curl a b")

    one = asyncio.create_task(run(*first))
    while not responder.calls:
        await asyncio.sleep(0)
    two = asyncio.create_task(run(*second))
    for _ in range(5):
        await asyncio.sleep(0)
    responder.release.set()
    return await asyncio.wait_for(one, 5), await asyncio.wait_for(two, 5)


async def test_concurrent_requests_for_one_target_share_one_prompt(grants):
    conversation = f"conv-{uuid4().hex}"
    responder, a, b = HeldResponder(ApprovalChoice.ALLOW_SESSION), FakeSandbox(), FakeSandbox()
    first, second = await _concurrently(responder, (a, [CURL]), (b, [CURL]), conversation=conversation)
    assert len(responder.calls) == 1
    assert a.granted == [[CURL_GRANT]] and b.granted == [[CURL_GRANT]]
    assert "was approved; run the command again" in first.text and "was approved; run the command again" in second.text


async def test_a_new_target_beside_a_pending_one_gets_its_own_prompt(grants):
    conversation = f"conv-{uuid4().hex}"
    responder, a, b = HeldResponder(ApprovalChoice.ALLOW_SESSION), FakeSandbox(), FakeSandbox()
    await _concurrently(responder, (a, [CURL]), (b, [CURL, ORG]), conversation=conversation)
    assert [[t["host"] for t in call["evidence"]["targets"]] for call in responder.calls] == [
        ["example.com"],
        ["example.org"],
    ]
    assert sorted(b.granted) == [[CURL_GRANT], [ORG_GRANT]]


async def test_allow_once_for_another_sandbox_is_asked_again_for_this_one(grants):
    # "Once" was said about the sandbox the card described; another agent's
    # sandbox may be the one every conversation shares.
    conversation = f"conv-{uuid4().hex}"
    responder, a, b = HeldResponder(ApprovalChoice.ALLOW), FakeSandbox(), FakeSandbox()
    await _concurrently(responder, (a, [CURL]), (b, [CURL]), conversation=conversation)
    assert len(responder.calls) == 2
    assert a.granted == [[CURL_GRANT]] and b.granted == [[CURL_GRANT]]


async def test_allow_once_is_shared_by_commands_of_the_same_sandbox(grants):
    conversation = f"conv-{uuid4().hex}"
    responder, sandbox = HeldResponder(ApprovalChoice.ALLOW), FakeSandbox()
    await _concurrently(responder, (sandbox, [CURL]), (sandbox, [CURL]), conversation=conversation)
    assert len(responder.calls) == 1


async def test_a_stop_ends_only_the_turn_that_asked(grants):
    conversation = f"conv-{uuid4().hex}"
    responder = HeldResponder(ApprovalChoice.DENY_STOP)
    first, second = await _concurrently(
        responder, (FakeSandbox(), [CURL]), (FakeSandbox(), [CURL]), conversation=conversation
    )
    assert (first.stop, second.stop) == (True, False)
    assert "declined network access to /usr/bin/curl -> example.com:443; do not try another route" in second.text


async def test_a_prompt_whose_turn_is_torn_down_releases_its_waiters(grants):
    import asyncio

    conversation = f"conv-{uuid4().hex}"
    responder = HeldResponder(ApprovalChoice.ALLOW)
    gate = PermissionGate(config_source=lambda: PermissionsConfig(), builtin=BuiltinRulings())

    async def run(sandbox):
        start_permission_turn(responder, conversation_id=conversation, turn_id=f"t-{uuid4().hex}")
        with tool_body(gate):
            return await handle_denials(sandbox, [CURL], command="curl a b")

    one = asyncio.create_task(run(FakeSandbox()))
    while not responder.calls:
        await asyncio.sleep(0)
    waiter = FakeSandbox()
    two = asyncio.create_task(run(waiter))
    for _ in range(5):
        await asyncio.sleep(0)
    one.cancel()
    notice = await asyncio.wait_for(two, 2)
    assert "expired with no answer" in notice.text and waiter.granted == []
    assert len(responder.calls) == 1


@pytest.mark.parametrize(
    ("host", "person"),
    [
        ("pypi.org", False),
        ("files.pythonhosted.org", False),
        ("PyPI.org.", False),
        ("169.254.169.254", True),
        ("2852039166", True),
        ("0xa9fea9fe", True),
        ("169.254.43518", True),
        ("0251.0376.0251.0376", True),
        ("[fd00::1]", True),
        ("fd00::1", True),
        ("localhost", True),
        ("metadata", True),
        ("metadata.google.internal", True),
        ("printer.local", True),
        ("api.localhost", True),
        ("router.home.arpa", True),
    ],
)
def test_only_a_public_looking_name_is_left_to_the_mode_or_the_reviewer(host, person):
    assert sandbox_network._needs_person(host) is person


async def test_a_numeric_spelling_of_an_address_is_asked_even_in_full_mode(grants):
    decimal = NetworkDenial("/usr/bin/curl", "2852039166", 80, "transparent_tcp_policy_denied")
    responder, sandbox = Responder(ApprovalChoice.DENY), FakeSandbox()
    await _handle(responder, [decimal], sandbox, config=PermissionsConfig(mode="full"))
    assert len(responder.calls) == 1 and sandbox.granted == []
