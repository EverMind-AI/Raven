"""Shared ground for the contributed ops tools: home, turn context, scheduling.

Three things every tool face needs and none should own alone:

- **the home.** The fork resolved its campaign root per call from the host
  config (``get_ops_home``); the plugin's root is the launcher-injected
  ``stateRoot`` (else a workspace default), handed to the factories once and
  held here. Module-global like the fork's config was: one activated
  oncall-flow per process, which is the plugin registry's own discipline
  (a duplicate id refuses activation).
- **the turn context.** Which window a call came from (session key), what
  task statement it carries, and where a wake scheduled now should land.
  The fork's loop pushed this per turn (``set_context``); the trunk loop
  pushes nothing, so the part-2c gate hook is the caller -- same face, new
  driver. Until that hook lands, an unnamed call resolves through the task
  tier and the one-live-campaign tier exactly as the fork's did with an
  empty session key.
- **the scheduling verb.** Every wake goes through the wake grant
  (``wakes.schedule_next_look``), and every schedule that knows a live route
  records it as ``wake_route`` in the campaign's meta -- these tools are the
  route's first writer; the resident watcher is its reader (2a design).
  A turn whose channel is the scheduler's own ("cron") is a cold start, not
  a window: its route comes from the campaign's declaration, never from the
  turn (the fork stored per-window addressing in the cron payload; the seam
  ruling retired that store).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oncall_flow import wakes
from raven.contracts.tool import Tool

if TYPE_CHECKING:
    from raven.contracts.scheduling import WakeScheduler

_HOME: Path | None = None


def set_home(path: Path | str) -> None:
    """Install the campaign root. The factories call this before any tool is
    constructed; a test drives it directly."""
    global _HOME
    _HOME = Path(path)


def ops_home() -> Path:
    """The campaign root for this activation.

    Raises rather than inventing a directory: a tool serving with no home is
    a wiring bug (the factory always sets one), and campaigns written to a
    guessed path would be invisible to the watcher reading the real one.
    """
    if _HOME is None:
        raise RuntimeError(
            "oncall-flow tools have no campaign root; the factory sets it from the plugin config slice (set_home)"
        )
    return _HOME


def _slug(text: str, limit: int = 24) -> str:
    from oncall_flow.instrument import campaign_slug

    return campaign_slug(text, limit)


# The channel a scheduler-fired turn arrives on. Not a window: a route taken
# from it would address the next wake to a place no front end subscribes to
# (the fork measured exactly this and kept a per-window store instead; the
# rebuilt shape reads the campaign's declared route).
_SCHEDULER_CHANNEL = "cron"


class _OpsScheduler(Tool):
    """Base for ops tools that schedule a self-wake; carries the session context.

    The fork's class of the same name held the CronService; this one holds the
    namespaced wake grant, granted late through ``bind_runtime`` (the plugin
    tools lane). A host that lends no scheduler leaves the grant ``None`` and
    every schedule answers with the fork's own "no scheduler" prose -- the
    tool stays on the table, because reading a campaign never needed a timer.
    """

    def __init__(self) -> None:
        self._scheduler: "WakeScheduler | None" = None
        self._channel = ""
        self._chat_id = ""
        self._session_key = ""
        self._task = ""

    def bind_runtime(self, handles: Any) -> None:
        self._scheduler = getattr(handles, "wake_scheduler", None)

    def set_context(self, channel: str, chat_id: str, session_key: str = "", task: str = "") -> None:
        self._channel = channel
        self._chat_id = chat_id
        # Which window this is. channel/chat_id are "tui"/"default" for every
        # window, so they cannot tell two apart; the session key can. The task
        # fingerprint outlives the window, which is how a fresh one recognises
        # the watch it is taking over.
        self._session_key = session_key or ""
        self._task = task or ""

    def _live_route(self) -> dict[str, str]:
        """The route this turn itself supplies, or {}.

        Only a window's own channel/chat_id count as live; a scheduler-fired
        turn supplies none and falls back to the campaign's declared route.
        """
        if self._channel and self._chat_id and self._channel != _SCHEDULER_CHANNEL:
            return {"channel": self._channel, "to": self._chat_id}
        return {}

    def schedule_look(self, campaign: str, cdir: Path, *, message: str, eta_seconds: int) -> str:
        """Schedule (or replace) the campaign's one wake; answer in prose.

        The route resolution is the D9 rebuilt addressing: this turn's own
        window first, else what the campaign's declaration recorded
        (``wake_route``, channel+to or direct_agent). Every schedule that used
        a live route writes it back into the meta -- the first-writer half of
        the 2a design, and what lets the cold resident watcher raise a wake
        for a campaign whose window is long gone.
        """
        route = self._live_route()
        recorded = False
        if not route:
            route = wakes.wake_route(campaign_meta(cdir))
            recorded = True
        note = wakes.schedule_next_look(
            self._scheduler,
            campaign=campaign,
            eta_seconds=eta_seconds,
            message=message,
            **route,
        )
        if note.startswith("Scheduled") and route and not recorded:
            record_wake_route(cdir, route)
        return note


def campaign_meta(cdir: Path) -> dict:
    """The campaign's declaration, or {}. Never raises."""
    try:
        return json.loads((cdir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record_wake_route(cdir: Path, route: dict[str, str]) -> None:
    """Write where this campaign's wakes land into its meta, once per change.

    A campaign fact, not a declaration fact: ``apparatus.meta_sha`` excludes
    the key, so recording a route never trips the declaration gate. Best
    effort -- the wake this rode on is already in the store, and losing the
    record costs the watcher a fallback, not the campaign its wake.
    """
    try:
        meta = campaign_meta(cdir)
        if meta.get("wake_route") == route:
            return
        meta["wake_route"] = dict(route)
        from oncall_flow.instrument import write_meta

        write_meta(cdir, meta)
    except OSError:
        pass
