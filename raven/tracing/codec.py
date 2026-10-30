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
from typing import BinaryIO

import zstandard

#: A zstd frame always opens with these four bytes. A JSON or text payload
#: cannot start with them (0xB5 is not a UTF-8 leading byte), which is what
#: makes the sniff safe for everything the store writes.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
COMPRESSION_LEVEL = 3

#: Ceiling on what one stored frame may decode to. The store's writer caps a
#: rotated log at 50 MiB and the largest captured payload observed is 32.9 MB,
#: so this is headroom rather than a routine limit. It exists because a trace
#: store can be imported from another machine: a small frame that expands to
#: gigabytes must be refused, not allocated.
MAX_PLAINTEXT_BYTES = 256 * 1024 * 1024


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


def read_plaintext(data: bytes, limit: int = MAX_PLAINTEXT_BYTES) -> bytes | None:
    """The payload's plaintext, or None when unreadable or larger than ``limit``.

    A compressed frame is decoded only up to ``limit + 1`` bytes, so a crafted
    frame is refused instead of allocated. Plain bytes were already read by the
    caller and are returned as they are.
    """
    if not is_compressed(data):
        return data
    try:
        plain = decompress_bytes(data, limit + 1)
    except zstandard.ZstdError:
        return None
    return None if len(plain) > limit else plain


def plaintext_size(path: Path) -> int | None:
    """The payload's uncompressed length, from the frame header when possible."""
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
            if not is_compressed(head):
                return path.stat().st_size
            handle.seek(0)
            try:
                size = zstandard.frame_content_size(handle.read(18))
            except zstandard.ZstdError:
                return None
            if size >= 0:
                return int(size)
            # A frame written without a content size is only measurable by
            # decoding it; the store's own writer always records the size.
            # `read(18)` above consumed the header, so rewind before decoding.
            handle.seek(0)
            data = handle.read()
    except OSError:
        return None
    plain = read_plaintext(data)
    return None if plain is None else len(plain)


def read_plaintext_file(path: Path, limit: int = MAX_PLAINTEXT_BYTES) -> bytes | None:
    """A stored file's plaintext, or None when it cannot be read or decoded."""
    try:
        return read_plaintext(path.read_bytes(), limit)
    except OSError:
        return None


def open_plaintext(path: Path, limit: int = MAX_PLAINTEXT_BYTES) -> BinaryIO:
    """Open a stored file as a plaintext byte stream.

    A caller that seeks or sizes the stream works in plaintext offsets, which
    is the only coordinate the store records (a span log's cursor, an
    artifact's range). A compressed payload is decompressed whole, up to
    ``limit`` bytes; a frame that expands past it is refused as unreadable.
    """
    handle = path.open("rb")
    try:
        head = handle.read(4)
        handle.seek(0)
        if not is_compressed(head):
            return handle
        data = handle.read()
    except BaseException:
        handle.close()
        raise
    handle.close()
    try:
        plain = decompress_bytes(data, limit + 1)
    except zstandard.ZstdError as exc:
        raise OSError(f"corrupt compressed payload: {path}") from exc
    if len(plain) > limit:
        raise OSError(f"compressed payload over {limit} bytes: {path}")
    return io.BytesIO(plain)


__all__ = [
    "COMPRESSION_LEVEL",
    "MAX_PLAINTEXT_BYTES",
    "ZSTD_MAGIC",
    "compress_bytes",
    "decompress_bytes",
    "is_compressed",
    "open_plaintext",
    "plaintext_size",
    "read_plaintext",
    "read_plaintext_file",
]
