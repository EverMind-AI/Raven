"""What a sandbox network denial tells the model, and what it leaves on the turn's record."""

from __future__ import annotations

from raven.permissions.sandbox_network import handle_denials
from raven.permissions.turn import start_permission_turn
from raven.sandbox.denials import NetworkDenial

CURL = NetworkDenial("/usr/bin/curl", "example.com", 443, "transparent_tcp_policy_denied")


async def test_a_blocked_target_is_named_in_the_tool_output():
    start_permission_turn(None, conversation_id="c", turn_id="t")
    text = await handle_denials(None, [CURL], command="curl https://example.com")
    assert text == (
        "\n\n[sandbox] The sandbox network policy blocked: /usr/bin/curl -> example.com:443. "
        "This is a policy block, not a network failure. Tell the user; it can be allowed under permissions.network."
    )


async def test_every_blocked_target_is_named_once():
    start_permission_turn(None, conversation_id="c", turn_id="t")
    other = NetworkDenial("/usr/bin/curl", "example.org", 443, "transparent_tcp_policy_denied")
    text = await handle_denials(None, [CURL, other], command="curl a b")
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
    assert await handle_denials(None, [], command="true") == ""
    assert turn.refusals == []
