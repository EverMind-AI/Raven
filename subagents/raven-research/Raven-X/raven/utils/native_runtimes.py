"""Native runtimes whose live threads make interpreter finalization unsafe.

Some dependencies host a Rust/tokio (or similar) runtime on a daemon thread.
Finalizing CPython while one is live can segfault (``Py_FinalizeEx``; SIGSEGV,
exit 139), masking the process's real exit code. One live runtime is enough on
its own: measured 3 of 3 runs each, a bare ``watchfiles.watch()`` daemon thread
with raven never imported segfaults, as does a live ``SkillFileWatcher`` with no
agent loop built. Stopping the runtime first exits cleanly. Nothing about the
agent loop is required, so any process that starts one of these is exposed.

Beware the negative arm when re-measuring: ``SkillFileWatcher.__init__`` drops
roots that fail ``.exists()`` and ``start`` then returns ``False``, so a
"no crash without X" run can be a process that never started a thread at all.
Assert the thread is live before believing an arm exits cleanly.

Two kinds of runtime, and the difference decides what the CLI does at exit:

* **stoppable** -- it has a shutdown hook, so the right move is to *use* it.
  ``SkillFileWatcher`` is one: stopping it before exit makes finalization
  clean (3 of 3 runs), which keeps normal interpreter shutdown and every
  ``atexit`` hook that rides on it.
* **unstoppable** -- no hook exists. lancedb's ``LanceDBBackgroundEventLoop``
  is the known one, started inside the third-party client. Nothing can be done
  but skip finalization entirely, which is what the CLI's hard exit is for.

Detection is a whitelist by construction: ``threading.enumerate`` only reports
Python-wrapper threads, and a name means nothing until something declares it.
The registration happening at the site that *starts* the thread is what makes
the whitelist sound rather than merely convenient -- registration precedes
``Thread.start`` in the same process, so "the thread is live" implies "its name
is registered". A central list cannot promise that, which is how the watcher
came to segfault exits on every host whose memory backend never opens lancedb.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable

from loguru import logger


class _Runtime:
    """One registered runtime: the thread name, and how to stop it if it can be.

    ``stop`` is held weakly. A bound method kept strongly here would pin the
    watcher -- and through its ``on_change`` the whole skill catalog -- for the
    life of the process, which is the exact leak
    ``LocalSkillCatalog.__init__`` warns about for long-lived parents that
    spawn many subagents. Weakly held costs nothing: a running thread already
    keeps its own target alive, so a dead weakref means the thread is gone too.
    """

    __slots__ = ("thread_name", "_stop_ref", "_stop_strong")

    def __init__(self, thread_name: str, stop: Callable[[], None] | None) -> None:
        self.thread_name = thread_name
        self._stop_ref: weakref.WeakMethod | None = None
        self._stop_strong: Callable[[], None] | None = None
        if stop is not None:
            try:
                self._stop_ref = weakref.WeakMethod(stop)  # type: ignore[arg-type]
            except TypeError:
                # Not a bound method (a plain function or a lambda): there is
                # no owner to release, so holding it strongly leaks nothing.
                self._stop_strong = stop

    def stop_hook(self) -> Callable[[], None] | None:
        if self._stop_ref is not None:
            return self._stop_ref()
        return self._stop_strong

    def is_stale(self) -> bool:
        """True when this entry named a stop hook whose owner is already gone."""
        return self._stop_ref is not None and self._stop_ref() is None

    def is_same_runtime(self, thread_name: str, stop: Callable[[], None] | None) -> bool:
        """True when a registration describes the runtime this entry already holds.

        Only a hook-bearing entry can be recognised: two hookless names are
        indistinguishable, and one of those is lancedb's seed, which must not be
        dropped by a later registration that happens to reuse its name.
        """
        if thread_name != self.thread_name or stop is None:
            return False
        return self.stop_hook() == stop


#: lancedb cannot self-register -- the thread starts inside the third-party
#: client with no hook to attach to -- so its name is seeded, with no way to
#: stop it. Everything raven starts registers itself at the start site.
_SEED: tuple[_Runtime, ...] = (_Runtime("LanceDBBackgroundEventLoop", None),)

_RUNTIMES: list[_Runtime] = list(_SEED)
_LOCK = threading.Lock()


def register_native_runtime_thread(thread_name: str, *, stop: Callable[[], None] | None = None) -> None:
    """Declare that a thread by this name hosts a native runtime.

    Call it immediately before starting the thread, so a live thread always
    has a registered name. ``stop`` is this runtime's shutdown hook when it has
    one; supplying it is what lets the process exit normally instead of having
    to skip finalization.

    Registering the same runtime again -- same name, same hook -- supersedes the
    earlier entry rather than adding one, so a consumer that stops and restarts
    its watcher does not grow the registry a row per restart. Two *distinct*
    runtimes sharing a thread name both stay: stopping only the newest would
    leave the other live and cost the process its normal finalization.
    """
    with _LOCK:
        _RUNTIMES[:] = [r for r in _RUNTIMES if not r.is_stale() and not r.is_same_runtime(thread_name, stop)]
        _RUNTIMES.append(_Runtime(thread_name, stop))


def _live_names() -> set[str]:
    return {t.name for t in threading.enumerate()}


def shutdown_native_runtimes() -> int:
    """Stop every live runtime that has a shutdown hook. Returns how many ran.

    Best effort by contract: this runs on the exit path, where a raising
    shutdown hook would replace the process's real outcome with a traceback
    from cleanup. Whatever remains live afterwards is what
    :func:`native_finalization_hazard` reports.
    """
    live = _live_names()
    with _LOCK:
        entries = list(_RUNTIMES)
    ran = 0
    for runtime in entries:
        if runtime.thread_name not in live:
            continue
        hook = runtime.stop_hook()
        if hook is None:
            continue
        try:
            hook()
            ran += 1
        except Exception as exc:  # never let cleanup decide the exit code
            logger.debug("native runtime {} refused to stop: {}", runtime.thread_name, exc)
    return ran


def native_finalization_hazard() -> bool:
    """Whether a registered native-runtime thread is still live.

    Asked after :func:`shutdown_native_runtimes`, so a True here means a
    runtime that cannot be stopped -- the only case worth skipping
    finalization for.
    """
    live = _live_names()
    with _LOCK:
        return any(r.thread_name in live for r in _RUNTIMES)
