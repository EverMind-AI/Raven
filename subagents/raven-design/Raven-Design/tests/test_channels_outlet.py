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
