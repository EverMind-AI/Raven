"""stdout hygiene and inbound framing: the layer everything else stands on."""

import asyncio
import io
import os

from raven.acp import stdio


async def _collect(data: bytes, *, max_frame_bytes: int = 512):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    errors: list[dict] = []
    frames = [f async for f in stdio.read_frames(reader, errors.append, max_frame_bytes=max_frame_bytes)]
    return frames, errors


async def test_read_frames_roundtrip_and_blank_lines():
    frames, errors = await _collect(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n\n  \n{"id":2}\n')
    assert [f.get("id") for f in frames] == [1, 2]
    assert errors == []


async def test_read_frames_answers_non_json_with_null_id_parse_error():
    frames, errors = await _collect(b'not json\n{"id":1}\n')
    assert [f.get("id") for f in frames] == [1]
    assert len(errors) == 1
    assert errors[0]["id"] is None
    assert errors[0]["error"]["code"] == stdio.PARSE_ERROR


async def test_read_frames_answers_invalid_utf8():
    frames, errors = await _collect(b'\xff\xfe{"broken"\n{"id":3}\n')
    assert [f.get("id") for f in frames] == [3]
    assert errors and errors[0]["error"]["code"] == stdio.PARSE_ERROR


async def test_oversized_complete_line_is_reported_and_skipped():
    big = b'{"pad":"' + b"x" * 600 + b'"}'
    frames, errors = await _collect(big + b'\n{"id":4}\n')
    assert [f.get("id") for f in frames] == [4]
    assert errors and errors[0]["error"]["code"] == stdio.INVALID_REQUEST


async def test_oversized_incomplete_line_resynchronises_at_its_newline():
    # The oversized frame arrives in chunks with no newline in sight; it must be
    # reported once and every byte up to its newline dropped, so the next frame
    # decodes cleanly.
    reader = asyncio.StreamReader()
    errors: list[dict] = []

    async def _feed():
        reader.feed_data(b'{"pad":"' + b"x" * 700)
        await asyncio.sleep(0)
        reader.feed_data(b"y" * 100 + b'"}\n{"id":5}\n')
        reader.feed_eof()

    feeder = asyncio.ensure_future(_feed())
    frames = [f async for f in stdio.read_frames(reader, errors.append, max_frame_bytes=512)]
    await feeder
    assert [f.get("id") for f in frames] == [5]
    assert len(errors) == 1
    assert errors[0]["error"]["code"] == stdio.INVALID_REQUEST


async def test_trailing_bytes_without_newline_are_not_a_frame():
    frames, errors = await _collect(b'{"id":6}\n{"id":7')
    assert [f.get("id") for f in frames] == [6]
    assert errors == []


def test_write_frame_is_whole_and_flushed():
    buf = io.BytesIO()
    stdio.write_frame(buf, {"id": 1})
    stdio.write_frame(buf, {"id": 2})
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2


def test_claim_stdout_moves_fd1_to_stderr_and_keeps_the_wire_clean(tmp_path, capfd):
    wire = tmp_path / "wire.bin"
    with open(wire, "wb") as sink:
        saved = os.dup(1)
        os.dup2(sink.fileno(), 1)
        try:
            with stdio.claim_stdout() as out:
                # Any write to fd 1 by any route must land on stderr for the
                # duration of the block. (The sys.stdout object cannot be
                # asserted here: pytest's capture replaces it with an in-memory
                # buffer that never touches fd 1 -- the descriptor is the
                # mechanism claim_stdout moves, so the descriptor is what this
                # tests.)
                os.write(1, b"raw fd write\n")
                stdio.write_frame(out, {"id": 1})
            # Restored: fd 1 is the original stdout target again.
            os.write(1, b"after\n")
        finally:
            os.dup2(saved, 1)
            os.close(saved)
    data = wire.read_bytes()
    assert data == b'{"id": 1}\nafter\n'
    assert "raw fd write" in capfd.readouterr().err
