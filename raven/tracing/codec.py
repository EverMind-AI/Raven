"""The zstd framing every stored payload shares.

A payload is written as a zstd frame under the path it always had, so a reader
tells the encoding from the frame's magic and never from the file name. That
is what keeps a store written before compression readable and a half-migrated
one coherent.

Only the encoding lives here: paths, dedupe and migration stay in
``store``/``compress``, so both can import this module without reaching back
into each other.
"""

from __future__ import annotations

import io
from pathlib import Path

import zstandard

#: A zstd frame always opens with these four bytes. A JSON or text payload
#: cannot start with them (0xB5 is not a UTF-8 leading byte), which is what
#: makes the sniff safe for everything the store writes.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
COMPRESSION_LEVEL = 3


def is_compressed(data: bytes) -> bool:
    return data[:4] == ZSTD_MAGIC


def compress_bytes(data: bytes) -> bytes:
    # A fresh compressor per call: ZstdCompressor instances are not documented
    # as thread safe, and the store writes from more than one worker thread.
    return zstandard.ZstdCompressor(level=COMPRESSION_LEVEL).compress(data)


def decompress_bytes(data: bytes, limit: int | None = None) -> bytes:
    """The plaintext in ``data``, at most ``limit`` bytes of it.

    ``limit`` bounds the output, not just the returned slice: a caller that
    only wants a preview must not pay for a whole multi-megabyte payload.
    """
    if not data:
        return b""
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=False) as reader:
        return reader.read() if limit is None else reader.read(limit)


def read_plaintext(data: bytes) -> bytes | None:
    """The payload's plaintext, or None when a compressed frame is unreadable."""
    try:
        return decompress_bytes(data) if is_compressed(data) else data
    except zstandard.ZstdError:
        return None


def plaintext_size(path: Path) -> int | None:
    """The payload's uncompressed length, from the frame header when possible."""
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
            if not is_compressed(head):
                return path.stat().st_size
            handle.seek(0)
            size = zstandard.frame_content_size(handle.read(18))
    except (OSError, zstandard.ZstdError):
        return None
    return None if size < 0 else int(size)


__all__ = [
    "COMPRESSION_LEVEL",
    "ZSTD_MAGIC",
    "compress_bytes",
    "decompress_bytes",
    "is_compressed",
    "plaintext_size",
    "read_plaintext",
]
