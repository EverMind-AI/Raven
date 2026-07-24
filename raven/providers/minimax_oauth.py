"""MiniMax Token Plan OAuth device flow and token storage."""

from __future__ import annotations

import json
import os
import tempfile
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from secrets import token_bytes
from typing import Callable, Iterator
from urllib.parse import urlparse

import httpx
import portalocker
from platformdirs import user_data_dir

CLIENT_ID = "coding-plan-cli"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
OAUTH_SCOPE = "openid profile coding_plan"
REFRESH_BUFFER_MS = 5 * 60 * 1000
OAUTH_STORAGE_DIR_ENV = "MINIMAX_OAUTH_TOKEN_DIR"


@dataclass(frozen=True)
class MiniMaxOAuthConfig:
    provider: str
    auth_base_url: str
    default_resource_url: str


@dataclass(frozen=True)
class MiniMaxOAuthToken:
    access: str
    refresh: str
    expires: int
    resource_url: str


CONFIGS = {
    "global": MiniMaxOAuthConfig(
        provider="minimax_global",
        auth_base_url="https://account.minimax.io",
        default_resource_url="https://api.minimax.io/anthropic/v1",
    ),
    "cn": MiniMaxOAuthConfig(
        provider="minimax_cn",
        auth_base_url="https://account.minimaxi.com",
        default_resource_url="https://api.minimaxi.com/anthropic/v1",
    ),
}


def oauth_config(region: str) -> MiniMaxOAuthConfig:
    try:
        return CONFIGS[region]
    except KeyError as exc:
        raise ValueError(f"Unsupported MiniMax region: {region}") from exc


def _validated_url(value: str, expected_url: str, field: str) -> str:
    parsed = urlparse(value)
    expected = urlparse(expected_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected.hostname
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RuntimeError(f"MiniMax returned an invalid {field}")
    return value


def token_path(region: str) -> Path:
    config = oauth_config(region)
    base_dir = os.environ.get(OAUTH_STORAGE_DIR_ENV)
    auth_dir = Path(base_dir) if base_dir else Path(user_data_dir("oauth-cli-kit", appauthor=False)) / "auth"
    return auth_dir / f"{config.provider}.json"


def _normalize_expiry(value: object, now_ms: int | None = None) -> int:
    try:
        raw = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("MiniMax returned an invalid expiry") from exc
    if raw <= 0:
        raise RuntimeError("MiniMax returned an invalid expiry")
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if raw < 1_000_000_000:
        return now_ms + raw * 1000
    if raw < 1_000_000_000_000:
        return raw * 1000
    return raw


def load_token(region: str) -> MiniMaxOAuthToken | None:
    path = token_path(region)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        config = oauth_config(region)
        resource_url = _validated_url(str(data["resource_url"]), config.default_resource_url, "resource URL")
        return MiniMaxOAuthToken(
            access=str(data["access"]),
            refresh=str(data["refresh"]),
            expires=int(data["expires"]),
            resource_url=resource_url,
        )
    except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError, OSError):
        return None


def save_token(region: str, token: MiniMaxOAuthToken) -> None:
    path = token_path(region)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(token), handle, ensure_ascii=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


@contextmanager
def _token_lock(region: str) -> Iterator[None]:
    path = token_path(region).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(path, mode="a+", timeout=600):
        yield


def _base64url(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _request_refresh(
    config: MiniMaxOAuthConfig,
    refresh_token: str,
    *,
    client: httpx.Client,
    sleep_fn: Callable[[float], None],
) -> MiniMaxOAuthToken:
    last_error: Exception | None = None
    for attempt in range(3):
        if attempt:
            sleep_fn(0.5 * attempt)
        try:
            response = client.post(
                f"{config.auth_base_url}/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": refresh_token,
                },
            )
        except httpx.TransportError as exc:
            last_error = exc
            continue
        if 400 <= response.status_code < 500:
            raise RuntimeError(f"MiniMax refresh token rejected: HTTP {response.status_code}")
        if response.status_code >= 500:
            last_error = RuntimeError(f"MiniMax refresh failed: HTTP {response.status_code}")
            continue
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success" or not payload.get("access_token"):
            raise RuntimeError("MiniMax refresh returned an invalid response")
        expires = _normalize_expiry(payload.get("expired_in"))
        if expires <= int(time.time() * 1000):
            raise RuntimeError("MiniMax refresh returned an invalid expiry")
        resource_url = _validated_url(
            str(payload.get("resource_url") or config.default_resource_url),
            config.default_resource_url,
            "resource URL",
        )
        return MiniMaxOAuthToken(
            access=str(payload["access_token"]),
            refresh=str(payload.get("refresh_token") or refresh_token),
            expires=expires,
            resource_url=resource_url,
        )
    raise RuntimeError("MiniMax refresh failed after transient retries") from last_error


def get_token(
    region: str,
    *,
    min_ttl_ms: int = REFRESH_BUFFER_MS,
    client: httpx.Client | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> MiniMaxOAuthToken:
    token = load_token(region)
    if token is None:
        raise RuntimeError(f"MiniMax {region} credentials not found. Run raven provider login first.")
    if token.expires > int(time.time() * 1000) + min_ttl_ms:
        return token

    config = oauth_config(region)
    owns_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        with _token_lock(region):
            latest = load_token(region) or token
            if latest.expires > int(time.time() * 1000) + min_ttl_ms:
                return latest
            refreshed = _request_refresh(config, latest.refresh, client=client, sleep_fn=sleep_fn)
            save_token(region, refreshed)
            return refreshed
    finally:
        if owns_client:
            client.close()


def _login_locked(
    region: str,
    *,
    print_fn: Callable[[str], None] = print,
    open_browser: bool = True,
    client: httpx.Client | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> MiniMaxOAuthToken:
    config = oauth_config(region)
    verifier = _base64url(token_bytes(32))
    challenge = _base64url(sha256(verifier.encode("ascii")).digest())
    state = _base64url(token_bytes(16))
    owns_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        response = client.post(
            f"{config.auth_base_url}/oauth2/device/code",
            data={
                "client_id": CLIENT_ID,
                "scope": OAUTH_SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            },
        )
        response.raise_for_status()
        device = response.json()
        if device.get("state") != state:
            raise RuntimeError("MiniMax OAuth state mismatch")
        verification_uri = str(device.get("verification_uri") or "")
        user_code = str(device.get("user_code") or "")
        deadline = _normalize_expiry(device.get("expired_in"))
        interval_ms = max(2000, int(device.get("interval") or 5000))
        if not verification_uri or not user_code or deadline <= int(time.time() * 1000):
            raise RuntimeError("MiniMax device authorization returned an invalid response")
        verification_uri = _validated_url(verification_uri, config.auth_base_url, "verification URL")

        print_fn(f"Open {verification_uri}")
        print_fn(f"Enter code: {user_code}")
        if open_browser:
            try:
                webbrowser.open(verification_uri)
            except Exception:
                pass

        while int(time.time() * 1000) < deadline:
            sleep_fn((interval_ms + 1000) / 1000)
            token_response = client.post(
                f"{config.auth_base_url}/oauth2/token",
                data={
                    "grant_type": DEVICE_GRANT_TYPE,
                    "client_id": CLIENT_ID,
                    "user_code": user_code,
                    "code_verifier": verifier,
                },
            )
            try:
                payload = token_response.json()
            except json.JSONDecodeError:
                token_response.raise_for_status()
                raise RuntimeError("MiniMax token polling returned invalid JSON")
            status = payload.get("status")
            error = payload.get("error")
            if status == "pending" or error == "authorization_pending":
                continue
            if error == "slow_down":
                interval_ms += 5000
                continue
            token_response.raise_for_status()
            if status != "success" or not payload.get("access_token") or not payload.get("refresh_token"):
                raise RuntimeError("MiniMax authorization failed or returned incomplete credentials")
            resource_url = _validated_url(
                str(payload.get("resource_url") or config.default_resource_url),
                config.default_resource_url,
                "resource URL",
            )
            token = MiniMaxOAuthToken(
                access=str(payload["access_token"]),
                refresh=str(payload["refresh_token"]),
                expires=_normalize_expiry(payload.get("expired_in")),
                resource_url=resource_url,
            )
            if token.expires <= int(time.time() * 1000):
                raise RuntimeError("MiniMax authorization returned an invalid expiry")
            save_token(region, token)
            return token
        raise RuntimeError("MiniMax device authorization timed out")
    finally:
        if owns_client:
            client.close()


def login(
    region: str,
    *,
    print_fn: Callable[[str], None] = print,
    open_browser: bool = True,
    client: httpx.Client | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> MiniMaxOAuthToken:
    with _token_lock(region):
        return _login_locked(
            region,
            print_fn=print_fn,
            open_browser=open_browser,
            client=client,
            sleep_fn=sleep_fn,
        )


def delete_token(region: str) -> None:
    with _token_lock(region):
        token_path(region).unlink(missing_ok=True)
