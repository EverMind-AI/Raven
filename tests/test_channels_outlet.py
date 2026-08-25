from raven.channels.outlet import ChannelOutletAdapter
from raven.spine import (
    ChatType,
    MediaOut,
    Notice,
    NoticeKind,
    Reasoning,
    Source,
    StreamDelta,
    Text,
    ToolEvent,
    ToolPhase,
)
from raven.spine.delivery import Outlet
from raven.spine.message import Media

_SAID = (
    "A safety rule stopped this operation, so the turn ended here. "
    "Say the word and I will carry on with the parts that do not need it."
)


def _src(channel="telegram", chat_id="c1") -> Source:
    return Source(channel=channel, chat_id=chat_id, sender_id="user", chat_type=ChatType.DM)


class _FakeChannel:
    """Records every send — stands in for a real channel's uniform send."""

    def __init__(self, name="telegram") -> None:
        self.name = name
        self.sent: list[tuple[str, str, list[str] | None]] = []

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        self.sent.append((chat_id, content, media))


def test_adapter_satisfies_outlet_protocol():
    adapter = ChannelOutletAdapter(_FakeChannel())
    assert isinstance(adapter, Outlet)
    assert adapter.name == "telegram"
    assert adapter.capabilities.streaming is False  # non-streaming


async def test_deliver_text_calls_channel_send():
    ch = _FakeChannel()
    adapter = ChannelOutletAdapter(ch)
    await adapter.deliver(Text(content="hi there", source=_src("telegram", "c9")))
    assert len(ch.sent) == 1
    chat_id, content, media = ch.sent[0]
    assert chat_id == "c9" and content == "hi there" and media is None


async def test_deliver_media_out_sends_local_paths():
    ch = _FakeChannel()
    adapter = ChannelOutletAdapter(ch)
    media = (
        Media(path="/tmp/a.png", mime="image/png", kind="image"),
        Media(path="/tmp/b.png", mime="image/png", kind="image"),
    )
    await adapter.deliver(MediaOut(media=media, source=_src()))
    assert len(ch.sent) == 1
    # media carries the local file paths (channels handle them, the hub does not).
    assert ch.sent[0][2] == ["/tmp/a.png", "/tmp/b.png"]


async def test_deliver_eats_streaming_and_in_turn_events():
    ch = _FakeChannel()
    adapter = ChannelOutletAdapter(ch)
    src = _src()
    await adapter.deliver(StreamDelta(delta="tok", source=src))
    await adapter.deliver(Reasoning(content="think", source=src))
    await adapter.deliver(ToolEvent(phase=ToolPhase.START, tool_call_id="t1", name="grep", source=src))
    await adapter.deliver(Notice(kind=NoticeKind.PROGRESS, detail="working", source=src))
    assert ch.sent == []  # all eaten — a non-streaming channel renders only the final reply


async def test_a_blocked_action_is_not_eaten() -> None:
    """The one notice kind that replaces the answer instead of accompanying it.
    The runtime ends the turn on a safety decision and the reply it would have
    sent *is* this notice, so eating it -- as this outlet does with every other
    kind -- ends the turn in silence: the user asked for something, the runtime
    refused, and nothing arrives. Measured through a real denial before this: the
    turn ended normally and the channel had sent nothing at all."""
    ch = _FakeChannel()
    adapter = ChannelOutletAdapter(ch)

    await adapter.deliver(
        Notice(kind=NoticeKind.ACTION_BLOCKED, detail="Error: command requires approval", source=_src("telegram", "c3"))
    )

    assert len(ch.sent) == 1
    chat_id, content, media = ch.sent[0]
    assert chat_id == "c3" and media is None
    # Both halves. The detail alone reduces the turn to the tool's error line, so
    # the person never learns that no alternative will be attempted and never
    # gets the offer to continue with the parts that do not need it.
    assert content == _SAID + "\nError: command requires approval"


async def test_a_blocked_action_with_no_detail_still_says_something() -> None:
    """``detail`` is optional on purpose: a tool can abort with no readable line
    at all. Sending whitespace would put an empty message in front of a person as
    if it were an explanation, and sending nothing is the silence this exists to
    prevent."""
    ch = _FakeChannel()
    adapter = ChannelOutletAdapter(ch)

    await adapter.deliver(Notice(kind=NoticeKind.ACTION_BLOCKED, detail=None, source=_src()))
    await adapter.deliver(Notice(kind=NoticeKind.ACTION_BLOCKED, detail="   \n ", source=_src()))

    assert [content for _chat, content, _media in ch.sent] == [_SAID, _SAID]
    assert not any(c.endswith("\n") for _chat, c, _media in ch.sent), "no dangling second half"
