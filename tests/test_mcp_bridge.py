"""Frame reading and relaying for the sub-agent bridge."""

import asyncio

import pytest

from raven.mcp.bridge import iter_raw_frames


async def _reader_from(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_yields_one_frame_per_line_undecoded():
    reader = await _reader_from(b'{"a":1}\n{"b":2}\n')
    assert [f async for f in iter_raw_frames(reader)] == [b'{"a":1}', b'{"b":2}']


@pytest.mark.asyncio
async def test_blank_lines_are_not_frames():
    reader = await _reader_from(b'\n\n{"a":1}\n')
    assert [f async for f in iter_raw_frames(reader)] == [b'{"a":1}']


@pytest.mark.asyncio
async def test_a_frame_far_past_the_stream_reader_default_limit_survives():
    # asyncio.StreamReader.readline caps a line at 64 KiB and raises rather than
    # growing its buffer. An MCP response carrying an image passes that in one
    # hop, so this is the case the hand-rolled buffer exists for.
    big = b'{"blob":"' + b"x" * (1024 * 1024) + b'"}'
    reader = await _reader_from(big + b"\n")
    frames = [f async for f in iter_raw_frames(reader)]
    assert frames == [big]


@pytest.mark.asyncio
async def test_a_frame_over_the_cap_raises_rather_than_buffering_forever():
    reader = await _reader_from(b"x" * 2048 + b"\n")
    with pytest.raises(ValueError, match="frame exceeds"):
        [f async for f in iter_raw_frames(reader, max_frame_bytes=1024)]


@pytest.mark.asyncio
async def test_a_truncated_final_line_is_not_a_frame():
    reader = await _reader_from(b'{"a":1}\n{"b":2')
    assert [f async for f in iter_raw_frames(reader)] == [b'{"a":1}']
