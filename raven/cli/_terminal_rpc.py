"""Authenticated local WebSocket requests and the terminal CLI result envelope."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import aiohttp
import typer

from raven.contracts.terminal import RuntimeInfo, error_envelope, success_envelope
from raven.home import raven_home


def state_path():
    return raven_home() / "serve.json"


class _TransportError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


async def _exchange(ws, method, params):
    request_id = str(uuid4())
    await ws.send_json({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    async for message in ws:
        if message.type == aiohttp.WSMsgType.BINARY:
            continue
        if message.type != aiohttp.WSMsgType.TEXT:
            raise _TransportError("runtime_unavailable", "Runtime closed the connection")
        try:
            frame = json.loads(message.data)
        except ValueError as exc:
            raise _TransportError("invalid_response", "Runtime returned invalid JSON") from exc
        if not isinstance(frame, dict):
            raise _TransportError("invalid_response", "Runtime returned a non-object response")
        if frame.get("id") is None and "method" in frame:
            continue
        if frame.get("id") != request_id or frame.get("jsonrpc") != "2.0":
            raise _TransportError("invalid_response", "Runtime response identity does not match")
        return frame
    raise _TransportError("runtime_unavailable", "Runtime closed before answering")


def _wrap(frame, runtime_id):
    runtime = RuntimeInfo(runtime_id=runtime_id)
    if frame.get("_meta", {}).get("runtimeId") != runtime_id:
        raise _TransportError("runtime_identity_changed", "Runtime identity changed during the request")
    if "error" in frame:
        error = frame["error"]
        if not isinstance(error, dict):
            raise _TransportError("invalid_response", "Invalid runtime error response")
        data = error.get("data")
        details = data if isinstance(data, dict) else {}
        code = details.get("code") or error.get("message", "rpc_error")
        message = details.get("message") or details.get("detail") or error.get("message", "RPC failed")
        return error_envelope(frame["id"], code, message, data, runtime)
    if not isinstance(frame.get("result"), dict):
        raise _TransportError("invalid_response", "Runtime response has no result")
    return success_envelope(frame["id"], frame["result"], runtime)


async def request(method, params, *, environment=None, timeout_ms=60000):
    runtime_id = None
    try:
        try:
            metadata = json.loads(state_path().read_text(encoding="utf-8"))
            port, token = metadata["port"], metadata["token"]
            if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str) or not token:
                raise ValueError("Invalid serve metadata")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise _TransportError("runtime_unavailable", "No usable serve.json; start raven serve first") from exc
        async with asyncio.timeout(timeout_ms / 1000):
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f"http://127.0.0.1:{port}/rpc", headers={"X-Raven-Token": token}) as ws:
                    status = await _exchange(ws, "runtime.status", {})
                    runtime = status.get("result", {}).get("runtime", {})
                    runtime_id = runtime.get("runtimeId")
                    if not isinstance(runtime_id, str) or not runtime_id:
                        raise _TransportError("runtime_unavailable", "Runtime status has no identity")
                    wrapped = _wrap(status, runtime_id)
                    if runtime.get("state") != "ready" or status["result"].get("graph", {}).get("state") != "ready":
                        raise _TransportError("runtime_unavailable", "Runtime is not ready")
                    if environment is not None and environment != runtime.get("environment", "local"):
                        raise _TransportError(
                            "environment_not_found", "Selected environment does not name this local runtime"
                        )
                    if method == "runtime.status":
                        return wrapped
                    return _wrap(await _exchange(ws, method, params), runtime_id)
    except _TransportError as exc:
        code, message = exc.code, str(exc)
    except TimeoutError:
        code, message = "timeout", "Runtime request timed out; delivery may be unverified"
    except (aiohttp.ClientError, OSError):
        code, message = "runtime_unavailable", "Cannot connect to the local Raven runtime"
    result = error_envelope("local", code, message, runtime=RuntimeInfo(runtime_id=runtime_id or ""))
    result["_meta"]["runtimeId"] = runtime_id
    return result


def run(method, params, *, environment=None, json_output=False, timeout_ms=60000):
    result = asyncio.run(request(method, params, environment=environment, timeout_ms=timeout_ms))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=None if json_output else 2))
    if not result.get("ok"):
        raise typer.Exit(1)
