"""An elided context is lossy in the body and lossless in the address.

``_emergency_shrink`` replaces the ``content`` of older ``role == "tool"`` messages
with a placeholder. It does not touch the assistant message that issued the call,
so the ``tool_calls`` entry carrying the url survives, and ``_ALLOWED_MSG_KEYS``
passes ``tool_calls`` / ``tool_call_id`` through to the wire. Everything the run
dropped can therefore be fetched again - which is exactly what the DR contract
tells the model to do when it meets the elision placeholder.

That property has never been asserted anywhere. It is a side effect of one
predicate, and widening that predicate by a single role would delete every address
in the context **with no symptom at all**: no exception, no counter, no changed
field. The run would simply lose the ability to re-open what it dropped, and the
only trace would be a quieter fetch rate that looks like the model choosing not to
re-read.

So the invariant gets a test rather than a comment alone. Both directions are fed:
a body that must disappear, and an address that must not.
"""

from __future__ import annotations

from raven.agent.harness_text import TOOL_OUTPUT_ELIDED
from raven.agent.loop.main import AgentLoop
from raven.providers.litellm_provider import _ALLOWED_MSG_KEYS


def _turn(n_fetches: int) -> list[dict]:
    """A DR-shaped turn: system, question, then n (assistant tool_call -> tool result)."""
    msgs: list[dict] = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "who is David Lander?"},
    ]
    for i in range(n_fetches):
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": "web_fetch",
                            "arguments": {
                                "url": f"https://example.com/page-{i}",
                                "info_to_extract": "the diagnosis year",
                            },
                        },
                    }
                ],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "name": "web_fetch",
                "content": f"BODY-{i} " + "x" * 4000,
            }
        )
    return msgs


def _urls(messages: list[dict]) -> list[str]:
    out = []
    for m in messages:
        for tc in m.get("tool_calls") or []:
            args = (tc.get("function") or {}).get("arguments") or {}
            if isinstance(args, dict) and args.get("url"):
                out.append(args["url"])
    return out


def test_a_shrink_deletes_bodies_and_keeps_every_address():
    msgs = _turn(10)
    before = _urls(msgs)
    assert len(before) == 10

    shrunk, elided = AgentLoop._emergency_shrink(msgs)

    # The half that must be lost.
    assert elided > 0, "nothing was elided - the test would pass vacuously"
    bodies = [m for m in shrunk if m.get("role") == "tool" and m.get("content") == TOOL_OUTPUT_ELIDED]
    assert len(bodies) == elided

    # The half that must NOT be lost. This is the whole point of the file.
    assert _urls(shrunk) == before, "an elided context lost the addresses it dropped"

    # And the pairing that makes an address usable: every surviving tool result still
    # names the call it answers, so a reader can tell which url produced which hole.
    ids_called = {tc["id"] for m in shrunk for tc in (m.get("tool_calls") or [])}
    ids_answered = {m["tool_call_id"] for m in shrunk if m.get("role") == "tool"}
    assert ids_answered == ids_called


def test_the_assistant_message_is_returned_byte_identical():
    """Not merely "the url is still findable" - the issuing message is untouched.

    A weaker assertion would pass if a future shrink rebuilt the assistant message
    and happened to preserve the url while dropping, say, ``tool_call_id``.
    """
    msgs = _turn(10)
    originals = [m for m in msgs if m.get("role") == "assistant"]
    shrunk, _ = AgentLoop._emergency_shrink(msgs)
    survived = [m for m in shrunk if m.get("role") == "assistant"]
    assert survived == originals


def test_the_wire_filter_passes_what_the_shrink_kept():
    """The address surviving in ``messages`` is worthless if the provider drops it.

    Two components, one property: the shrink keeps ``tool_calls`` and the request
    sanitizer must let them through. Asserted together because separating them is
    how a property ends up true in each half and false end-to-end.
    """
    assert "tool_calls" in _ALLOWED_MSG_KEYS
    assert "tool_call_id" in _ALLOWED_MSG_KEYS


def test_shrink_leaves_a_short_turn_alone():
    """Opposite direction: below the keep-recent floor nothing is touched at all.

    Without this, the assertions above would still pass if ``_emergency_shrink``
    had been changed to elide everything unconditionally.
    """
    msgs = _turn(AgentLoop._SHRINK_KEEP_RECENT_TOOL_RESULTS)
    shrunk, elided = AgentLoop._emergency_shrink(msgs)
    assert elided == 0
    assert shrunk == msgs


def test_the_check_catches_a_widened_predicate():
    """The failure this file exists to prevent, simulated.

    Everything above passes today, which proves nothing on its own: a check that
    cannot fail is indistinguishable from one that is merely lucky. So here the
    predicate is widened by exactly one role - the change someone would plausibly
    make while "also shrinking the assistant turns" - and the address assertion
    must catch it.
    """
    msgs = _turn(10)
    before = _urls(msgs)

    # A shrink that also rewrites assistant messages. One word wider than the real one.
    keep = AgentLoop._SHRINK_KEEP_RECENT_TOOL_RESULTS
    idxs = [i for i, m in enumerate(msgs) if m.get("role") in ("tool", "assistant")]
    elide = set(idxs[:-keep])
    widened = [
        {**m, "content": TOOL_OUTPUT_ELIDED, "tool_calls": []} if i in elide else m
        for i, m in enumerate(msgs)
    ]

    assert _urls(widened) != before, (
        "the widened shrink did not actually drop any address - this test is not "
        "exercising the failure it claims to guard against"
    )
    # ...and the real one still does not.
    assert _urls(AgentLoop._emergency_shrink(msgs)[0]) == before
