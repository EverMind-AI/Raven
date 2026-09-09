from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.memory_engine.consolidate.consolidator import MemoryConsolidator
from raven.session.manager import Session
from raven.utils import helpers


class _FakeEncoding:
    def encode(self, payload: str) -> list[str]:
        return list(payload)


def test_estimate_prompt_tokens_counts_assistant_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        helpers.tiktoken,
        "get_encoding",
        lambda _name: _FakeEncoding(),
    )
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": "x" * 10_000,
                },
            },
        ],
    }

    prompt_tokens = helpers.estimate_prompt_tokens([msg])
    message_tokens = helpers.estimate_message_tokens(msg)

    assert prompt_tokens >= message_tokens
    assert prompt_tokens > 10_000


def test_estimate_counts_reasoning_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        helpers.tiktoken,
        "get_encoding",
        lambda _name: _FakeEncoding(),
    )
    base = {"role": "assistant", "content": "answer"}
    with_reasoning = {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "r" * 5_000,
        "thinking_blocks": [{"thinking": "t" * 5_000}],
    }

    assert helpers.estimate_message_tokens(with_reasoning) > helpers.estimate_message_tokens(base)
    assert helpers.estimate_prompt_tokens([with_reasoning]) > helpers.estimate_prompt_tokens([base])
    assert helpers.estimate_message_tokens(with_reasoning) > 10_000


def test_estimate_prompt_tokens_fallback_counts_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_name: str) -> object:
        raise RuntimeError("encoding unavailable")

    monkeypatch.setattr(helpers.tiktoken, "get_encoding", _raise)
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "arguments": "x" * 40_000,
                },
            },
        ],
    }

    assert helpers.estimate_prompt_tokens([msg]) > 10_000


def test_consolidator_prompt_estimate_counts_tool_calls_for_trigger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        helpers.tiktoken,
        "get_encoding",
        lambda _name: _FakeEncoding(),
    )
    session = Session(
        key="cli:default",
        messages=[
            {"role": "user", "content": "please write a large file"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": "x" * 70_000,
                        },
                    },
                ],
            },
        ],
    )

    consolidator = MemoryConsolidator(
        workspace=tmp_path,
        provider=object(),
        model="test-model",
        sessions=object(),
        context_window_tokens=65_536,
        build_messages=lambda **kwargs: [
            {"role": "system", "content": "system"},
            *kwargs["history"],
            {"role": "user", "content": kwargs["current_message"]},
        ],
        get_tool_definitions=lambda: [],
    )

    estimated, source = consolidator.estimate_session_prompt_tokens(session)

    assert source == "tiktoken"
    assert estimated > consolidator.context_window_tokens


def test_token_consolidation_triggers_on_tool_call_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        helpers.tiktoken,
        "get_encoding",
        lambda _name: _FakeEncoding(),
    )
    session = Session(
        key="cli:default",
        messages=[
            {"role": "user", "content": "please write a large file"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": "x" * 2_000,
                        },
                    },
                ],
            },
            {"role": "user", "content": "continue"},
        ],
    )
    sessions = MagicMock()
    consolidator = MemoryConsolidator(
        workspace=tmp_path,
        provider=object(),
        model="test-model",
        sessions=sessions,
        context_window_tokens=1_000,
        build_messages=lambda **kwargs: [
            {"role": "system", "content": "system"},
            *kwargs["history"],
            {"role": "user", "content": kwargs["current_message"]},
        ],
        get_tool_definitions=lambda: [],
    )
    consolidator.consolidate_messages = AsyncMock(return_value=True)
    consolidator.maybe_refresh_hot_tags = AsyncMock(return_value=0)

    asyncio.run(consolidator.maybe_consolidate_by_tokens(session))

    assert session.last_consolidated == 2
    consolidator.consolidate_messages.assert_awaited_once()
    sessions.save.assert_called_once_with(session)


def _png(width: int, height: int, *, compress: int = 0) -> bytes:
    """Minimal valid PNG. ``compress=0`` keeps the payload incompressible, which
    is what makes a real photo's data URI dwarf its token cost."""
    import hashlib
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    noise = hashlib.sha256(b"raven").digest()
    blob = noise * ((width * 3 * height) // len(noise) + 2)
    raw = b"".join(b"\x00" + blob[y * width * 3 : (y + 1) * width * 3] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, compress))
        + chunk(b"IEND", b"")
    )


def _data_uri(png: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(png).decode()


def test_estimate_image_tokens_matches_anthropic_published_values() -> None:
    # Published examples from Anthropic's vision docs (28x28 patches, cap 1568).
    assert helpers.estimate_image_tokens(1000, 1000) == 1296
    assert helpers.estimate_image_tokens(100, 100) == 16
    # Beyond the cap the charge saturates rather than growing with area.
    assert helpers.estimate_image_tokens(8000, 8000) == 1568


def test_image_pixel_size_survives_every_real_jpeg_variant() -> None:
    """The SOF walk must not desync on markers that carry no length field.

    Reading a standalone marker's payload as a segment length used to abandon the
    walk, which silently fell back to charging the per-image ceiling.
    """
    import io

    from PIL import Image

    def jpg(**kw) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (640, 480), (1, 2, 3)).save(buf, "JPEG", **kw)
        return buf.getvalue()

    plain = jpg()
    variants = {
        "plain": plain,
        "exif": jpg(exif=b"Exif\x00\x00" + b"\x00" * 64),
        "progressive": jpg(progressive=True),
        "restart_blocks": jpg(restart_marker_blocks=4),
        "optimized": jpg(optimize=True),
        # Standalone markers: TEM and RST0 have no length field.
        "tem_marker": plain[:2] + b"\xff\x01" + plain[2:],
        "rst_marker": plain[:2] + b"\xff\xd0" + plain[2:],
        # Fill bytes are legal before any marker.
        "fill_bytes": plain[:2] + b"\xff\xff\xff" + plain[2:],
    }
    for label, data in variants.items():
        assert helpers._image_pixel_size(data) == (640, 480), label


def test_image_pixel_size_returns_none_on_malformed_jpeg_without_raising() -> None:
    """Unparseable is fine -- the caller charges the ceiling, which over-estimates
    in the safe direction. Raising would not be fine."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (1, 2, 3)).save(buf, "JPEG")
    plain = buf.getvalue()

    for label, data in {
        "truncated": plain[:40],
        "garbage_tail": plain[:2] + b"\xff\xe0\x00\x02" + b"\x00" * 5,
        "zero_length_segment": plain[:2] + b"\xff\xe0\x00\x00" + plain[4:],
        "not_an_image": b"hello world",
    }.items():
        assert helpers._image_pixel_size(data) is None, label


def test_image_pixel_size_parses_png_gif_and_jpeg() -> None:
    assert helpers._image_pixel_size(_png(640, 480)) == (640, 480)

    gif = b"GIF89a" + (320).to_bytes(2, "little") + (240).to_bytes(2, "little")
    assert helpers._image_pixel_size(gif) == (320, 240)

    # SOF0 frame header: precision, height, width, components.
    sof = (
        b"\xff\xc0" + (11).to_bytes(2, "big") + b"\x08" + (200).to_bytes(2, "big") + (300).to_bytes(2, "big") + b"\x01"
    )
    assert helpers._image_pixel_size(b"\xff\xd8\xff" + b"\xe0\x00\x02" + sof) == (300, 200)

    assert helpers._image_pixel_size(b"not an image") is None


def test_estimate_content_part_tokens_ignores_text_parts() -> None:
    assert helpers.estimate_content_part_tokens({"type": "text", "text": "hi"}) is None
    assert helpers.estimate_content_part_tokens("not a dict") is None


def test_estimate_content_part_tokens_charges_ceiling_for_unparseable_image() -> None:
    remote = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
    assert helpers.estimate_content_part_tokens(remote) == helpers._IMAGE_TOKEN_CAP

    corrupt = {"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!!"}}
    assert helpers.estimate_content_part_tokens(corrupt) == helpers._IMAGE_TOKEN_CAP


def test_image_data_uri_is_billed_by_patch_area_not_base64_length() -> None:
    """Regression: counting the data URI as text charged ~2000x the real cost on
    a multi-megabyte image, starving the history budget."""
    png = _png(1000, 1000)
    block = {"type": "image_url", "image_url": {"url": _data_uri(png)}}
    message = {"role": "user", "content": [block, {"type": "text", "text": "describe"}]}

    # The data URI really is enormous -- the assertion below is not vacuous.
    assert len(block["image_url"]["url"]) > 3_000_000

    estimate = helpers.estimate_prompt_tokens([message])
    assert 1296 <= estimate < 1400
    assert helpers.estimate_message_tokens(message) == estimate


def test_image_tokens_survive_the_tiktoken_fallback_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Images are counted outside tiktoken, so a tokenizer failure must not drop
    them -- nor may an image-only message report zero."""
    monkeypatch.setattr(
        helpers.tiktoken,
        "get_encoding",
        lambda _name: (_ for _ in ()).throw(RuntimeError("no tokenizer")),
    )
    block = {"type": "image_url", "image_url": {"url": _data_uri(_png(280, 280))}}

    assert helpers.estimate_prompt_tokens([{"role": "user", "content": [block]}]) == 100
    assert helpers.estimate_message_tokens({"role": "user", "content": [block]}) == 100
