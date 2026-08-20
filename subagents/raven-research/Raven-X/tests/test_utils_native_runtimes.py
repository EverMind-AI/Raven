"""Unit tests for the native-runtime registry behind the CLI exit guard.

The registry decides whether a process may finalize normally. Its failure
modes are both silent: a runtime it does not know about segfaults the exit
(139 replacing the real code), and a stop hook it holds strongly pins the
object graph that hook belongs to.
"""

from __future__ import annotations

import threading

import pytest

from raven.utils import native_runtimes


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """An empty registry per test, so these assertions describe the mechanism
    rather than the host.

    Emptied rather than reset to the shipped seed: the predicate reads live
    threads, and a full-suite run can genuinely be hosting a real lancedb
    thread that an earlier test left running -- which made a seeded registry
    answer True for reasons that have nothing to do with the case under test.
    """
    monkeypatch.setattr(native_runtimes, "_RUNTIMES", [])


class _FakeRuntime:
    """Stands in for a watcher: a named live thread plus a stop hook."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._stop = threading.Event()
        self.stop_calls = 0
        self.thread = threading.Thread(target=self._stop.wait, name=name, daemon=True)

    def start(self) -> None:
        native_runtimes.register_native_runtime_thread(self.name, stop=self.stop)
        self.thread.start()

    def stop(self) -> None:
        self.stop_calls += 1
        self._stop.set()
        self.thread.join(timeout=2)


def test_an_unregistered_thread_is_not_a_hazard() -> None:
    """The whitelist is the whole mechanism: an unnamed thread cannot be one."""
    runtime = _FakeRuntime("SomethingNobodyDeclared")
    runtime.thread.start()
    try:
        assert native_runtimes.native_finalization_hazard() is False
    finally:
        runtime.stop()


def test_a_registered_live_thread_is_a_hazard_until_it_is_stopped() -> None:
    runtime = _FakeRuntime("FakeNotifyRuntime")
    runtime.start()
    try:
        assert native_runtimes.native_finalization_hazard() is True

        assert native_runtimes.shutdown_native_runtimes() == 1
        assert runtime.stop_calls == 1
        # The point of a stop hook: nothing is left to skip finalization for.
        assert native_runtimes.native_finalization_hazard() is False
    finally:
        runtime.stop()


def test_a_runtime_with_no_stop_hook_stays_a_hazard() -> None:
    """lancedb's shape -- known by name, no hook -- must survive shutdown as a
    hazard, because a hard exit is the only thing left that works for it."""
    runtime = _FakeRuntime("HookLessRuntime")
    native_runtimes.register_native_runtime_thread(runtime.name, stop=None)
    runtime.thread.start()
    try:
        assert native_runtimes.shutdown_native_runtimes() == 0
        assert native_runtimes.native_finalization_hazard() is True
    finally:
        runtime.stop()


def test_lancedb_is_seeded_hookless_because_it_cannot_self_register() -> None:
    """The one runtime raven does not start itself: its thread begins inside the
    third-party client, so nothing is there to call the registration."""
    seeded = {r.thread_name: r for r in native_runtimes._SEED}

    assert "LanceDBBackgroundEventLoop" in seeded
    assert seeded["LanceDBBackgroundEventLoop"].stop_hook() is None


def test_shutdown_survives_a_stop_hook_that_raises() -> None:
    """This runs on the exit path, where a raising hook would replace the
    process's real outcome with a traceback from cleanup."""

    class _Angry(_FakeRuntime):
        def stop(self) -> None:
            self.stop_calls += 1
            self._stop.set()
            raise RuntimeError("shutdown refused")

    runtime = _Angry("AngryRuntime")
    runtime.start()
    try:
        assert native_runtimes.shutdown_native_runtimes() == 0
        assert runtime.stop_calls == 1
    finally:
        runtime._stop.set()
        runtime.thread.join(timeout=2)


def test_the_registry_does_not_pin_the_object_that_owns_the_stop_hook() -> None:
    """A strongly held bound method would keep the watcher -- and through its
    on_change the whole skill catalog -- alive for the life of the process,
    which is the leak LocalSkillCatalog documents for subagent-spawning
    parents. Registering must not reintroduce it."""
    import gc
    import weakref

    runtime = _FakeRuntime("ShortLivedRuntime")
    runtime.start()
    runtime.stop()  # thread is gone; nothing else should hold the object
    ref = weakref.ref(runtime)
    del runtime
    gc.collect()

    assert ref() is None, "the registry kept the runtime alive"

    # And the stale entry is pruned rather than accumulating per start().
    native_runtimes.register_native_runtime_thread("Another", stop=None)
    assert all(not r.is_stale() for r in native_runtimes._RUNTIMES)


def test_restarting_one_runtime_supersedes_its_entry_instead_of_adding_one() -> None:
    """A consumer may stop and restart its watcher (the catalog exposes both).
    The object stays alive across that, so its entry is never stale -- without
    superseding, the registry would grow a row per restart."""
    runtime = _FakeRuntime("RestartedRuntime")
    for _ in range(3):
        native_runtimes.register_native_runtime_thread(runtime.name, stop=runtime.stop)

    assert len(native_runtimes._RUNTIMES) == 1


def test_two_distinct_runtimes_sharing_a_name_both_stay_registered() -> None:
    """Subagents can each own a watcher, and they share a thread name. Dropping
    either one would stop only one of two live runtimes and cost the process the
    normal finalization the surviving one still blocks."""
    first = _FakeRuntime("SharedName")
    second = _FakeRuntime("SharedName")
    first.start()
    second.start()
    try:
        assert len(native_runtimes._RUNTIMES) == 2
        assert native_runtimes.shutdown_native_runtimes() == 2
        assert first.stop_calls == 1
        assert second.stop_calls == 1
    finally:
        first.stop()
        second.stop()
