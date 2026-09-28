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
import math
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

#: What each attempt in ``_PENDING`` answered, or will: its pair, and the
#: monotonic instant its code stops being valid. A second start answers with it.
_HANDOFFS: dict[str, asyncio.Future[tuple[dict[str, str], float]]] = {}

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

    A start while one is already polling answers with that one's pair and the
    time its code has left: a second click is a reader who lost the vendor's
    tab, and a refusal left the page showing a code it had stopped watching.

    Raises ``LookupError`` for a provider with no device flow, and whatever the
    vendor raised when the code could not be requested.
    """
    starter = _STARTERS.get(slug)
    if starter is None:
        raise LookupError(f"{slug} has no device-code sign-in")
    live = _PENDING.get(slug)
    if live is not None and not live.done() and _still_valid(_HANDOFFS[slug]):
        return await _answer(_HANDOFFS[slug])

    loop = asyncio.get_running_loop()
    handoff: asyncio.Future[tuple[dict[str, str], float]] = loop.create_future()

    def resolve(uri: str, code: str, ttl: int) -> None:
        def _set() -> None:
            if not handoff.done():
                handoff.set_result(({"verification_uri": uri, "user_code": code}, time.monotonic() + int(ttl)))

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
        else:
            # What `provider login` does after every handler: the drivers write
            # these files under the process umask.
            from raven.config.paths import restrict_to_owner
            from raven.config.update_providers import oauth_credential_files

            try:
                restrict_to_owner(*oauth_credential_files(slug))
            except OSError as exc:
                logger.warning("could not make the {} credential owner-only: {}", slug, exc)

    task = asyncio.create_task(asyncio.to_thread(run))
    _PENDING[slug] = task
    _HANDOFFS[slug] = handoff
    task.add_done_callback(lambda done: _forget(slug, done))
    return await _answer(handoff)


def _still_valid(handoff: asyncio.Future[tuple[dict[str, str], float]]) -> bool:
    # Unanswered, its code is as fresh as any. Answered, it can be dead while the
    # attempt still runs: the driver's poll outlives its code by up to one interval.
    if not handoff.done():
        return True
    if handoff.cancelled() or handoff.exception() is not None:
        return False
    return handoff.result()[1] > time.monotonic()


async def _answer(handoff: asyncio.Future[tuple[dict[str, str], float]]) -> dict[str, Any]:
    # Shielded: one caller giving up on a slow vendor must not cancel the answer
    # a second caller is still waiting for.
    pair, deadline = await asyncio.wait_for(asyncio.shield(handoff), _HANDOFF_TIMEOUT_S)
    return {**pair, "expires_in": max(1, math.ceil(deadline - time.monotonic()))}


def _forget(slug: str, task: asyncio.Task[Any]) -> None:
    # A done callback runs after the task has ended, and a new start may hold
    # the slot by then.
    if _PENDING.get(slug) is task:
        del _PENDING[slug]
        _HANDOFFS.pop(slug, None)


__all__ = ["DEFAULT_TTL_S", "pending", "start", "supports"]
