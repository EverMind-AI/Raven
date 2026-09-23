"""Start a vendor's device-code sign-in without a console.

The CLI runs each vendor's flow to completion in the terminal that asked for
it. A page cannot: it needs the verification URL and the user code the moment
the vendor hands them out, and the polling has to go on after the reply is
sent. :func:`start` runs the vendor's own flow in a thread, hands the pair back
as soon as it exists and leaves the thread polling until the token lands or
the code expires. ``model.options`` reports the outcome the way it always did:
``authenticated`` flips once the credential file exists.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from loguru import logger

Resolve = Callable[[str, str, int], None]
"""(verification_uri, user_code, expires_in seconds) -> None; idempotent."""

#: Device codes outlive a page's patience but not a session's: the vendors that
#: do not say how long theirs last get this.
DEFAULT_TTL_S = 900

#: The one attempt per provider that may be in flight. A second start while the
#: first is polling would hand out a second code the first poller never sees.
_PENDING: dict[str, asyncio.Task[Any]] = {}

#: The handoff wait: how long the vendor gets to answer the device-code request
#: before the page is told to try again.
_HANDOFF_TIMEOUT_S = 60


def _minimax(region: str) -> Callable[[Resolve], None]:
    def run(resolve: Resolve) -> None:
        from raven.providers.minimax_oauth import login

        def on_device(uri: str, code: str, deadline_ms: int) -> None:
            resolve(uri, code, max(1, (deadline_ms - int(time.time() * 1000)) // 1000))

        login(region, print_fn=lambda _m: None, open_browser=False, on_device=on_device)

    return run


def _openai_codex(resolve: Resolve) -> None:
    from raven.providers.chatgpt_token import clear_abandoned_device_code
    from raven.providers.litellm_setup import import_litellm

    import_litellm()
    from litellm.llms.chatgpt.authenticator import Authenticator
    from litellm.llms.chatgpt.common_utils import CHATGPT_DEVICE_VERIFY_URL

    # Otherwise the driver waits for an earlier, abandoned code instead of
    # requesting one, and nothing ever reaches the page.
    clear_abandoned_device_code()
    auth = Authenticator()
    request = auth._request_device_code

    def capture() -> dict[str, str]:
        device = request()
        resolve(CHATGPT_DEVICE_VERIFY_URL, str(device.get("user_code") or ""), DEFAULT_TTL_S)
        return device

    # Per instance: the driver's own flow does the polling and the writing, this
    # only listens in on the one step that mints the code.
    auth._request_device_code = capture  # type: ignore[method-assign]
    auth._login_device_code()


def _github_copilot(resolve: Resolve) -> None:
    from raven.providers.litellm_setup import import_litellm

    import_litellm()
    from litellm.llms.github_copilot.authenticator import Authenticator

    auth = Authenticator()
    device = auth._get_device_code()
    ttl = int(device.get("expires_in") or DEFAULT_TTL_S)
    resolve(str(device["verification_uri"]), str(device["user_code"]), ttl)
    # The driver's poll gives up after a minute, which is less than a person
    # takes to reach for a phone; keep asking until the code itself expires.
    deadline = time.time() + ttl
    token = ""
    while time.time() < deadline:
        try:
            token = auth._poll_for_access_token(str(device["device_code"]))
            break
        except Exception as exc:  # noqa: BLE001 -- the driver's timeout is one of these
            logger.debug("copilot device poll: {}", exc)
    if not token:
        raise RuntimeError("GitHub device code expired before it was entered")
    auth._ensure_token_dir()
    with open(auth.access_token_file, "w", encoding="utf-8") as fh:
        fh.write(token)
    auth.get_api_key()


_STARTERS: dict[str, Callable[[Resolve], None]] = {
    "minimax_global": _minimax("global"),
    "minimax_cn": _minimax("cn"),
    "openai_codex": _openai_codex,
    "github_copilot": _github_copilot,
}


def supports(slug: str) -> bool:
    return slug in _STARTERS


def pending() -> dict[str, asyncio.Task[Any]]:
    return {k: t for k, t in _PENDING.items() if not t.done()}


async def start(slug: str) -> dict[str, Any]:
    """Begin ``slug``'s device flow; answer with the pair once the vendor has it.

    Raises ``LookupError`` for a provider with no device flow, ``RuntimeError``
    when one is already in flight for it, and whatever the vendor raised when
    the code could not be requested.
    """
    starter = _STARTERS.get(slug)
    if starter is None:
        raise LookupError(f"{slug} has no device-code sign-in")
    live = _PENDING.get(slug)
    if live is not None and not live.done():
        raise RuntimeError(f"a sign-in for {slug} is already waiting for its code to be entered")

    loop = asyncio.get_running_loop()
    handoff: asyncio.Future[dict[str, Any]] = loop.create_future()

    def resolve(uri: str, code: str, ttl: int) -> None:
        def _set() -> None:
            if not handoff.done():
                handoff.set_result({"verification_uri": uri, "user_code": code, "expires_in": int(ttl)})

        loop.call_soon_threadsafe(_set)

    def run() -> None:
        try:
            starter(resolve)
        except BaseException as exc:  # noqa: BLE001 -- carried to the waiter, logged after it
            err = exc
            if not handoff.done():

                def _fail(e: BaseException = err) -> None:
                    if not handoff.done():
                        handoff.set_exception(e)

                loop.call_soon_threadsafe(_fail)
                return
            logger.info("device sign-in for {} ended without a token: {}", slug, err)

    task = asyncio.create_task(asyncio.to_thread(run))
    _PENDING[slug] = task
    task.add_done_callback(lambda _t: _PENDING.pop(slug, None))
    return await asyncio.wait_for(handoff, _HANDOFF_TIMEOUT_S)


__all__ = ["DEFAULT_TTL_S", "pending", "start", "supports"]
