"""``raven.gateway.submit_router``: a runtime turn lands on the spine that owns
its conversation -- the page's for ``tui:`` sessions, the gateway's otherwise."""

from __future__ import annotations

from raven.gateway.submit_router import route_submit
from raven.spine import ChatType, Origin, Scheduler, Source, TurnRequest, conversation_id


def _relay(conversation: str | None, channel: str = "tui", chat_id: str = "abc") -> TurnRequest:
    return TurnRequest(
        origin=Origin.SUBAGENT,
        source=Source(channel=channel, chat_id=chat_id, sender_id="subagent", chat_type=ChatType.DM),
        text="[Subagent returned]",
        conversation=conversation,
    )


def test_a_page_session_relay_goes_to_the_page_spine_and_a_channel_one_stays():
    page, channel = [], []
    submit = route_submit(page=page.append, channel=channel.append)
    submit(_relay("tui:20260908_072151_5315e2"))
    submit(_relay("telegram:8281248569", channel="telegram", chat_id="8281248569"))
    assert [r.conversation for r in page] == ["tui:20260908_072151_5315e2"]
    assert [r.conversation for r in channel] == ["telegram:8281248569"]


def test_the_lane_key_is_derived_the_way_the_scheduler_derives_it():
    """The router decides page-vs-channel by the key the scheduler lanes on; the
    two are one function, and the scheduler's own method is that function."""
    import raven.gateway.submit_router as router

    assert router.conversation_id is conversation_id
    for req in (
        _relay(None, channel="tui", chat_id="abc"),
        _relay("weixin:room"),
        _relay(None, channel="x", chat_id="1"),
    ):
        assert Scheduler._conversation_id(None, req) == conversation_id(req)  # type: ignore[arg-type]
    assert conversation_id(_relay(None, channel="tui", chat_id="abc")) == "tui:abc"
    page, channel = [], []
    route_submit(page=page.append, channel=channel.append)(_relay(None, channel="tui", chat_id="abc"))
    assert len(page) == 1 and not channel


def test_without_a_page_submit_everything_stays_on_the_channel_spine():
    channel = []

    def to_channel(req):
        channel.append(req)

    submit = route_submit(page=None, channel=to_channel)
    assert submit is to_channel, "no page spine: the gateway's submit is handed back as it was"
    submit(_relay("tui:abc"))
    assert len(channel) == 1


def test_the_routed_submit_returns_what_the_spine_returned():
    submit = route_submit(
        page=lambda req: ("page", req.conversation), channel=lambda req: ("channel", req.conversation)
    )
    assert submit(_relay("tui:abc")) == ("page", "tui:abc")
    assert submit(_relay("slack:c1")) == ("channel", "slack:c1")


def test_the_page_prefix_is_the_one_the_question_router_and_the_session_minter_use():
    """Two routers split page sessions from channel ones by the same prefix, and
    the sessions themselves are minted with it (``raven.rpc.methods.session``);
    the layer contract keeps ``raven.gateway`` from importing ``raven.rpc``, so
    the spellings are held equal here rather than by one importing the other."""
    import inspect

    from raven.gateway.submit_router import PAGE_PREFIX
    from raven.rpc.question_broker import RoutingQuestionBroker

    assert inspect.signature(RoutingQuestionBroker.__init__).parameters["page_prefix"].default == PAGE_PREFIX
    assert PAGE_PREFIX == "tui:"
