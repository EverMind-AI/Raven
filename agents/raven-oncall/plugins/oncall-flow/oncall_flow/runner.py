"""Runner helpers: prepare a remote host and run a tuning campaign in one call.

Turns the manual steps (sync the trial code, pull the image, drive a campaign)
into reusable, testable functions. File sync and command execution are injected
(real = rsync / ssh; tests = fakes), so the wiring is unit-testable without a
host. ``run_grid`` / ``run_adaptive`` build the campaign, drive it via a proposer,
and return the best record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from oncall_flow.campaign import Campaign
from oncall_flow.docker_backend import CommandRunner
from oncall_flow.ledger import JobRecord, Ledger
from oncall_flow.proposer import GridProposer, Proposer
from oncall_flow.tuning import tune

SyncRunner = Callable[[str, str], tuple[int, str]]


def ensure_image(run: CommandRunner, image: str) -> bool:
    """Pull ``image`` if the host does not already have it. Returns True if pulled."""
    rc, out = run(f"docker images -q {image}")
    if rc == 0 and out.strip():
        return False
    run(f"docker pull {image}")
    return True


def prepare_remote(
    run: CommandRunner,
    sync: SyncRunner,
    *,
    image: str,
    app_local: str,
    app_remote: str,
) -> None:
    """Sync the app dir (trial code + data) to the host and ensure the image is present."""
    sync(app_local, app_remote)
    ensure_image(run, image)


def make_ssh_sync(host: str, port: int, key: str, *, user: str = "root", connect_timeout: int = 15) -> SyncRunner:
    import subprocess

    def sync(local: str, remote: str) -> tuple[int, str]:
        ssh = (
            f"ssh -i {key} -p {port} -o BatchMode=yes "
            f"-o ConnectTimeout={connect_timeout} -o StrictHostKeyChecking=accept-new"
        )
        argv = ["rsync", "-az", "-e", ssh, local, f"{user}@{host}:{remote}"]
        proc = subprocess.run(argv, capture_output=True, text=True)
        tail = f"\n{proc.stderr}" if proc.stderr and proc.returncode != 0 else ""
        return proc.returncode, proc.stdout + tail

    return sync


async def run_grid(
    name: str,
    grid: list[dict[str, Any]],
    backend: Any,
    ledger: Ledger,
    *,
    metric: str = "score",
    goal: str = "max",
) -> JobRecord | None:
    campaign = Campaign(name, [], backend, ledger, metric=metric, goal=goal)
    return await tune(campaign, GridProposer(grid), max_rounds=2)


async def run_adaptive(
    name: str,
    proposer: Proposer,
    backend: Any,
    ledger: Ledger,
    *,
    metric: str = "score",
    goal: str = "max",
    max_rounds: int = 8,
) -> JobRecord | None:
    campaign = Campaign(name, [], backend, ledger, metric=metric, goal=goal)
    return await tune(campaign, proposer, max_rounds=max_rounds)


async def run_adaptive_polling(
    name: str,
    proposer: Proposer,
    backend: Any,
    ledger: Ledger,
    *,
    metric: str = "score",
    goal: str = "max",
    max_rounds: int = 8,
    interval: float = 3.0,
) -> JobRecord | None:
    """Adaptive tuning against a real backend: poll with a delay between passes.

    Like ``run_adaptive`` but each round is driven by ``drive_polling`` (which
    sleeps ``interval`` between reconciliation passes) instead of ``campaign.run``
    (which busy-polls with no delay). Use this for a real host so a round doesn't
    hammer the backend with back-to-back ``docker inspect`` calls; ``run_adaptive``
    stays the choice for an instant (mock) backend.
    """
    campaign = Campaign(name, [], backend, ledger, metric=metric, goal=goal)
    for round_index in range(max_rounds):
        batch = await proposer.propose(campaign.history(), round_index)
        if not batch or campaign.add_trials(batch) == 0:
            break
        await drive_polling(campaign, interval=interval)
    return campaign.best()


async def drive_polling(
    campaign: Campaign,
    *,
    interval: float = 3.0,
    max_ticks: int = 1800,
) -> JobRecord | None:
    """Reconcile the campaign until done, sleeping ``interval`` between passes.

    A plain poll loop for a one-shot run (e.g. the ``python -m oncall_flow.tune`` entry): it is
    resumable via the ledger if the process dies. The event-driven, always-on
    variant (driver + heartbeat wake) is the gateway integration.
    """
    ticks = 0
    while not campaign.is_done():
        if ticks >= max_ticks:
            raise TimeoutError(f"campaign {campaign.name!r} not done after {max_ticks} ticks")
        await campaign.step()
        if campaign.is_done():
            break
        await asyncio.sleep(interval)
        ticks += 1
    return campaign.best()
