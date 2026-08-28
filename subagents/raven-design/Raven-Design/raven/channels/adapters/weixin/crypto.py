"""AES-128-ECB media crypto for the WeChat (iLink) protocol.

iLink encrypts CDN media with AES-128-ECB + PKCS7. The key arrives base64-
encoded in one of two flavours — raw 16 bytes (images) or 32 hex chars
(file / voice / video) — so it must be normalized before use. This module
isolates that protocol-mandated crypto from the adapter; the cipher backend
is whichever of pycryptodome / cryptography is installed.
"""

from __future__ import annotations

import base64
import re
from contextlib import suppress

from loguru import logger

_BLOCK = 16


def parse_aes_key(key_b64: str) -> bytes:
    """Normalize an iLink AES key to raw 16 bytes.

    base64 → 16 raw bytes (images), or base64 → 32 hex chars → 16 bytes
    (file / voice / video).
    """
    raw = base64.b64decode(key_b64)
    if len(raw) == _BLOCK:
        return raw
    if len(raw) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", raw):
        return bytes.fromhex(raw.decode("ascii"))
    raise ValueError(f"aes_key must be 16 raw bytes or 32 hex chars, got {len(raw)}")


def _run_ecb(key: bytes, data: bytes, *, encrypt: bool) -> bytes | None:
    """AES-128-ECB via whichever backend is available; None if neither is."""
    with suppress(ImportError):
        from Crypto.Cipher import AES

        cipher = AES.new(key, AES.MODE_ECB)
        return cipher.encrypt(data) if encrypt else cipher.decrypt(data)
    with suppress(ImportError):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        worker = Cipher(algorithms.AES(key), modes.ECB())
        op = worker.encryptor() if encrypt else worker.decryptor()
        return op.update(data) + op.finalize()
    logger.warning("No AES backend available (install pycryptodome or cryptography)")
    return None


def encrypt(data: bytes, key_b64: str) -> bytes:
    """Encrypt media for CDN upload.

    Raises on a bad key or a missing crypto backend instead of falling back to
    plaintext: the caller advertises the AES key alongside the upload, so
    sending raw bytes would make the receiver decrypt garbage — silently
    corrupted media. (The download side keeps its lenient fallback; there the
    raw bytes are still the best available result.)
    """
    key = parse_aes_key(key_b64)
    padding = _BLOCK - len(data) % _BLOCK
    out = _run_ecb(key, data + bytes([padding]) * padding, encrypt=True)
    if out is None:
        raise RuntimeError("no AES backend available (install pycryptodome or cryptography)")
    return out


def decrypt(data: bytes, key_b64: str) -> bytes:
    """Decrypt downloaded media. Returns *data* unchanged on key/backend failure."""
    try:
        key = parse_aes_key(key_b64)
    except Exception as e:
        logger.warning("AES key parse failed, returning raw: {}", e)
        return data
    out = _run_ecb(key, data, encrypt=False)
    return unpad_pkcs7(out) if out is not None else data


def unpad_pkcs7(data: bytes, block: int = _BLOCK) -> bytes:
    """Strip PKCS7 padding when present and valid; otherwise return as-is."""
    if not data or len(data) % block != 0:
        return data
    pad = data[-1]
    if 1 <= pad <= block and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data
