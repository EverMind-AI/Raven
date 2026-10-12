"""A Bug Report Receiver running on a background thread, for synchronous CLI tests."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from raven.trajectory import receiver as rcv

PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


@contextmanager
def receiver_thread(save_dir: Path) -> Iterator[str]:
    """Serve a receiver on an ephemeral port until the block exits; yields its base URL."""
    ready = threading.Event()
    address: dict[str, object] = {}
    control: dict[str, object] = {}

    def _run() -> None:
        async def _main() -> None:
            control["loop"] = asyncio.get_running_loop()
            stop = asyncio.Event()
            control["stop"] = stop

            def _ready(host: str, port: int) -> None:
                address.update(host=host, port=port)
                ready.set()

            await rcv.serve(rcv.ReceiverConfig(save_dir=save_dir, port=0), ready=_ready, stop=stop)

        try:
            asyncio.run(_main())
        finally:
            ready.set()

    thread = threading.Thread(target=_run, name="bug-report-receiver", daemon=True)
    thread.start()
    if not ready.wait(15) or "port" not in address:
        raise RuntimeError("the receiver thread did not come up")
    try:
        yield f"http://{address['host']}:{address['port']}"
    finally:
        loop = control.get("loop")
        stop = control.get("stop")
        if loop is not None and stop is not None:
            loop.call_soon_threadsafe(stop.set)  # type: ignore[union-attr]
        thread.join(15)
