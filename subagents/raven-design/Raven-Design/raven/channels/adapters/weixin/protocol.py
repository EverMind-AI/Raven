"""WeChat (iLink) wire-protocol constants and stateless helpers.

iLink is Tencent's bot gateway for personal WeChat (ilinkai.weixin.qq.com),
reached over plain HTTP long-polling. These are protocol facts (item type
codes, header names, the packed client-version int) shared by any client
that speaks iLink; the stateful HTTP calls live on the adapter.
"""

from __future__ import annotations

import base64
import os
from typing import Any

# MessageItemType
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

# MessageType (1 = inbound from user, 2 = outbound from bot)
MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2

# getuploadurl media-type codes
UPLOAD_IMAGE = 1
UPLOAD_VIDEO = 2
UPLOAD_FILE = 3
UPLOAD_VOICE = 4

ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.1.1"
MAX_MESSAGE_LEN = 4000

ERRCODE_SESSION_EXPIRED = -14
SESSION_PAUSE_DURATION_S = 60 * 60
MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_DELAY_S = 30
RETRY_DELAY_S = 2
MAX_QR_REFRESH_COUNT = 3
TYPING_STATUS_TYPING = 1
TYPING_STATUS_CANCEL = 2
TYPING_TICKET_TTL_S = 24 * 60 * 60
TYPING_KEEPALIVE_INTERVAL_S = 5
CONFIG_CACHE_INITIAL_RETRY_S = 2
CONFIG_CACHE_MAX_RETRY_S = 60 * 60
DEFAULT_LONG_POLL_TIMEOUT_S = 35

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".ico", ".svg"}
_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv"}
_VOICE_EXTS = {".mp3", ".wav", ".amr", ".silk", ".ogg", ".m4a", ".aac", ".flac"}


def build_client_version(version: str) -> int:
    """Pack ``major.minor.patch`` into ``0x00MMNNPP``."""
    parts = (version.split(".") + ["0", "0", "0"])[:3]

    def octet(s: str) -> int:
        try:
            return int(s) & 0xFF
        except ValueError:
            return 0

    return (octet(parts[0]) << 16) | (octet(parts[1]) << 8) | octet(parts[2])


CLIENT_VERSION = build_client_version(CHANNEL_VERSION)
BASE_INFO: dict[str, str] = {"channel_version": CHANNEL_VERSION}


def random_wechat_uin() -> str:
    """X-WECHAT-UIN: a fresh random uint32 as a base64'd decimal string."""
    return base64.b64encode(str(int.from_bytes(os.urandom(4), "big")).encode()).decode()


def build_headers(token: str = "", route_tag: Any = None) -> dict[str, str]:
    """Per-request iLink headers (a fresh UIN per call). Bearer set when *token* is given."""
    headers = {
        "X-WECHAT-UIN": random_wechat_uin(),
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if route_tag is not None and str(route_tag).strip():
        headers["SKRouteTag"] = str(route_tag).strip()
    return headers


def has_downloadable_media_locator(media: dict[str, Any] | None) -> bool:
    if not isinstance(media, dict):
        return False
    return bool(str(media.get("encrypt_query_param", "") or "") or str(media.get("full_url", "") or "").strip())


def ext_for_type(media_type: str) -> str:
    return {"image": ".jpg", "voice": ".silk", "video": ".mp4", "file": ""}.get(media_type, "")
