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
from raven.spine.delivery import Capabilities
from raven.spine.message import Media


def _src(channel="telegram", chat_id="c1") -> Source:
    return Source(channel=channel, chat_id=chat_id, sender_id="user", chat_type=ChatType.DM)


class _FakeChannel:
    """Records every send — stands in for a real channel's uniform send."""

    def __init__(self, name="telegram", *, file_attachments=False) -> None:
        self.name = name
        self.capabilities = Capabilities(file_attachments=file_attachments)
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


async def test_deliver_files_uses_native_channel_attachments():
    ch = _FakeChannel(file_attachments=True)
    adapter = ChannelOutletAdapter(ch)
    event = ToolEvent(
        phase=ToolPhase.COMPLETE,
        tool_call_id="t1",
        name="deliver_files",
        source=_src(),
        metadata={
            "raven_delivery": {
                "message": "Final files",
                "files": [{"name": "report.pdf", "path": "/tmp/report.pdf"}],
            }
        },
    )

    await adapter.deliver(event)

    assert ch.sent == [("c1", "Final files", ["/tmp/report.pdf"])]


async def test_deliver_files_falls_back_to_a_compact_list_without_attachments():
    ch = _FakeChannel(file_attachments=False)
    adapter = ChannelOutletAdapter(ch)
    event = ToolEvent(
        phase=ToolPhase.COMPLETE,
        tool_call_id="t1",
        name="deliver_files",
        source=_src(),
        metadata={
            "raven_delivery": {
                "message": "Final files",
                "files": [{"name": "report.pdf", "path": "/tmp/report.pdf"}],
            }
        },
    )

    await adapter.deliver(event)

    assert ch.sent == [(
        "c1",
        "Final files\nFiles ready: report.pdf. This channel cannot attach files; open the same session in Raven UI or TUI.",
        None,
    )]
