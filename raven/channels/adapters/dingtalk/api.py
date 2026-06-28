"""Async client for the DingTalk robot REST API.

Owns the httpx session and the access-token cache, and exposes one coroutine
per remote operation (file download, remote fetch with SSRF guard, media
upload, message send). Pure transport — no channel/bus concerns. These are
live network flows, exercised by integration/manual testing.
"""

from __future__ import annotations

import json
import time

import httpx
from loguru import logger

_OAUTH = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_FILE_DOWNLOAD = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"
_GROUP_SEND = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
_OTO_SEND = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
_UPLOAD = "https://oapi.dingtalk.com/media/upload"

MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 3
_TOKEN_TTL_FALLBACK_S = 7200
_TOKEN_RENEW_MARGIN_S = 60


class DingTalkAPI:
    def __init__(self, robot_code: str, app_secret: str):
        self.robot_code = robot_code
        self.app_secret = app_secret
        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_deadline = 0.0

    async def open(self) -> None:
        self._http = httpx.AsyncClient()

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── auth ──────────────────────────────────────────────────────────

    async def access_token(self) -> str | None:
        if self._token and time.time() < self._token_deadline:
            return self._token
        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot refresh token")
            return None
        try:
            resp = await self._http.post(
                _OAUTH, json={"appKey": self.robot_code, "appSecret": self.app_secret}
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data.get("accessToken")
            ttl = int(data.get("expireIn", _TOKEN_TTL_FALLBACK_S))
            self._token_deadline = time.time() + ttl - _TOKEN_RENEW_MARGIN_S
            return self._token
        except Exception as e:
            logger.error("Failed to get DingTalk access token: {}", e)
            return None

    async def _auth_headers(self) -> dict[str, str] | None:
        token = await self.access_token()
        return {"x-acs-dingtalk-access-token": token} if token else None

    # ── inbound media ─────────────────────────────────────────────────

    async def download_file(self, download_code: str) -> bytes | None:
        """Trade a downloadCode for a temporary URL, then return its bytes."""
        headers = await self._auth_headers()
        if headers is None or not self._http:
            logger.error("dingtalk file download: no token or http client")
            return None
        try:
            meta = await self._http.post(
                _FILE_DOWNLOAD,
                json={"downloadCode": download_code, "robotCode": self.robot_code},
                headers={**headers, "Content-Type": "application/json"},
            )
            if meta.status_code != 200:
                logger.error("dingtalk get download URL failed: status={} body={}", meta.status_code, meta.text[:300])
                return None
            if not (url := meta.json().get("downloadUrl")):
                logger.error("dingtalk download URL missing in response")
                return None
            blob = await self._http.get(url, follow_redirects=True)
            if blob.status_code != 200:
                logger.error("dingtalk file fetch failed: status={}", blob.status_code)
                return None
            if len(blob.content) > MAX_MEDIA_BYTES:
                logger.warning("dingtalk media too large: {} bytes (cap 20MB)", len(blob.content))
                return None
            return blob.content
        except Exception as e:
            logger.error("dingtalk file download error: {}", e)
            return None

    async def fetch_remote(self, url: str) -> tuple[bytes | None, str | None]:
        """GET a remote URL, revalidating against SSRF on every redirect hop."""
        from raven.security.network import validate_resolved_url, validate_url_target

        if not self._http:
            return None, None
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            ok, err = (validate_url_target if hop == 0 else validate_resolved_url)(current)
            if not ok:
                logger.warning("dingtalk ssrf blocked: {} ({})", current, err)
                return None, None
            try:
                resp = await self._http.get(current, follow_redirects=False)
            except Exception as e:
                logger.error("dingtalk media fetch error ref={} err={}", current, e)
                return None, None
            if resp.status_code in (301, 302, 303, 307, 308):
                if not (nxt := resp.headers.get("location") or ""):
                    logger.warning("dingtalk redirect without location header: {}", current)
                    return None, None
                current = nxt
                continue
            if resp.status_code >= 400:
                logger.warning("dingtalk media download failed status={} ref={}", resp.status_code, current)
                return None, None
            if len(resp.content) > MAX_MEDIA_BYTES:
                logger.warning("dingtalk remote media too large: {} bytes (cap {})", len(resp.content), MAX_MEDIA_BYTES)
                return None, None
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
            return resp.content, content_type
        logger.warning("dingtalk too many redirects for {}", url)
        return None, None

    # ── outbound ──────────────────────────────────────────────────────

    async def upload_media(self, media_type: str, filename: str, data: bytes, mime: str) -> str | None:
        token = await self.access_token()
        if not token or not self._http:
            return None
        try:
            resp = await self._http.post(
                f"{_UPLOAD}?access_token={token}&type={media_type}",
                files={"media": (filename, data, mime)},
            )
            body = resp.text
            payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code >= 400 or payload.get("errcode", 0) != 0:
                logger.error("DingTalk media upload failed type={} status={} body={}", media_type, resp.status_code, body[:500])
                return None
            nested = payload.get("result") or {}
            media_id = (
                payload.get("media_id") or payload.get("mediaId")
                or nested.get("media_id") or nested.get("mediaId")
            )
            if not media_id:
                logger.error("DingTalk media upload missing media_id body={}", body[:500])
                return None
            return str(media_id)
        except Exception as e:
            logger.error("DingTalk media upload error type={} err={}", media_type, e)
            return None

    async def send(self, chat_id: str, msg_key: str, msg_param: dict) -> bool:
        headers = await self._auth_headers()
        if headers is None or not self._http:
            logger.warning("DingTalk cannot send: no token or http client")
            return False

        encoded = json.dumps(msg_param, ensure_ascii=False)
        if chat_id.startswith("group:"):
            url = _GROUP_SEND
            body = {"robotCode": self.robot_code, "openConversationId": chat_id[6:], "msgKey": msg_key, "msgParam": encoded}
        else:
            url = _OTO_SEND
            body = {"robotCode": self.robot_code, "userIds": [chat_id], "msgKey": msg_key, "msgParam": encoded}

        try:
            resp = await self._http.post(url, json=body, headers=headers)
            raw = resp.text
            if resp.status_code != 200:
                logger.error("DingTalk send failed msgKey={} status={} body={}", msg_key, resp.status_code, raw[:500])
                return False
            try:
                result = resp.json()
            except Exception:
                result = {}
            if result.get("errcode") not in (None, 0):
                logger.error("DingTalk send api error msgKey={} errcode={} body={}", msg_key, result.get("errcode"), raw[:500])
                return False
            logger.debug("DingTalk message sent to {} msgKey={}", chat_id, msg_key)
            return True
        except Exception as e:
            logger.error("Error sending DingTalk message msgKey={} err={}", msg_key, e)
            return False
