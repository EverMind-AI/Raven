"""Run a block that binds the EverOS roles into this process, and put the process back after it.

``bind_roles_here`` (:mod:`raven_everos.config`) is reached by
``EverosBackend.start()`` and by the plugin's tool factory
(:mod:`raven_everos.tools`), so by any runtime built with the memory plugin
on. It writes the four roles into ``os.environ`` directly, records them in
``_BOUND_HERE`` and publishes that record as ``RAVEN_EVEROS_BOUND`` so it
survives ``os.execv``; ``configure_everos_env`` sets ``EVEROS_ROOT`` beside
them. ``monkeypatch`` undoes none
of it: it records only what it changed itself, and ``delenv(..., raising=False)``
on a name that is not there records nothing. So a binding made in one test was
still in the process when the next began, and which test paid depended on how
the suite was dealt into shards -- a settings test reading a saved endpoint was
told the environment outranks it (``role_is_env_managed``) and refused the
write, in one shard and not in another.

Three things go back together, because the readers read them together: the
``EVEROS_*`` variables (the binding, and ``EVEROS_ROOT``), the provenance
variable, and the *contents* of the provenance set. The contents rather than
the object: a test that swapped the object with ``monkeypatch`` has the
module's own put back before an autouse fixture's teardown runs, and that is
the one whose contents then matter.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import pytest

from raven_everos import config as cf


def is_binding(name: str) -> bool:
    """Whether the environment variable ``name`` is part of what a binding writes."""
    return name.startswith("EVEROS_") or name == cf.PROVENANCE_ENV


@contextlib.contextmanager
def confined_everos_binding() -> Iterator[None]:
    """Everything a binding changes, as it was when the block began, once the block ends -- exception or not."""
    before = {key: value for key, value in os.environ.items() if is_binding(key)}
    bound = set(cf._BOUND_HERE)
    try:
        yield
    finally:
        for key in [key for key in os.environ if is_binding(key) and key not in before]:
            del os.environ[key]
        os.environ.update(before)
        cf._BOUND_HERE.clear()
        cf._BOUND_HERE.update(bound)


def forget_everos_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start a test with no role exported and no provenance remembered, and have it put back when the test ends.

    For a reader of ``role_is_env_managed``: an operator's export in the
    developer's own shell, or a binding a test elsewhere left behind, would
    turn a case that assumes nothing is exported into a different case --
    silently, and only on that machine or in that shard. The set is swapped
    rather than cleared, so what the test's own binding puts in it goes with
    the swap.
    """
    for key in [key for key in os.environ if is_binding(key)]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(cf, "_BOUND_HERE", set())
