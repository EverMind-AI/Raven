"""Which machine registry the on-call launcher hands its instance.

`raven.ops.connections` resolves its own path beside the config it was started
with, which for this install is the state root -- so an instance given no answer
sees no machines at all, and a task naming a path on the CPU box is read as
naming one here. Measured 2026-08-25.

The answer used to be a copy taken once and never refreshed. These pin the
pointer that replaced it, and the two properties that matter about it: every
raven this launcher starts gets the same one (a wake is exactly where nobody is
watching to notice a difference), and an install that already keeps its own list
is not stranded to make the point.
"""

import importlib.util
from pathlib import Path

import pytest

_LAUNCHER = Path(__file__).resolve().parents[1] / "subagents" / "raven-oncall" / "run.py"


def load(monkeypatch, *, raven_home: Path, state_root: Path):
    """A fresh launcher module: it reads both roots at import time."""
    monkeypatch.setenv("RAVEN_HOME", str(raven_home))
    monkeypatch.setenv("ONCALL_STATE_ROOT", str(state_root))
    spec = importlib.util.spec_from_file_location("raven_oncall_launcher", _LAUNCHER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def roots(tmp_path):
    host, state = tmp_path / "raven-home", tmp_path / "state"
    host.mkdir()
    state.mkdir()
    return host, state


def test_the_owners_registry_is_what_an_instance_is_pointed_at(monkeypatch, roots):
    """`raven ops connection add` writes here, and the host's graph check reads
    here. An instance reading anywhere else is a third answer to one question."""
    host, state = roots
    (host / "connections.json").write_text("{}", encoding="utf-8")

    mod = load(monkeypatch, raven_home=host, state_root=state)

    assert mod.connections_registry() == host / "connections.json"


def test_an_install_that_already_keeps_its_own_list_stays_on_it(monkeypatch, roots):
    host, state = roots
    (state / "connections.json").write_text("{}", encoding="utf-8")

    mod = load(monkeypatch, raven_home=host, state_root=state)

    assert mod.connections_registry() == state / "connections.json"


def test_with_neither_present_it_names_the_owners_path(monkeypatch, roots):
    """So the thing the owner is told to fix is the file they will actually add to,
    not one in a state directory they have never heard of."""
    host, state = roots

    mod = load(monkeypatch, raven_home=host, state_root=state)

    assert mod.connections_registry() == host / "connections.json"


def test_the_registry_is_not_copied_into_the_instance(monkeypatch, roots):
    """A copy is right once and wrong from the first machine the owner adds."""
    host, state = roots
    (host / "connections.json").write_text('{"connections": []}', encoding="utf-8")

    mod = load(monkeypatch, raven_home=host, state_root=state)
    mod.connections_registry()

    assert not (state / "connections.json").exists()


def test_an_explicit_pointer_from_the_caller_is_not_overridden(monkeypatch, roots):
    """Both env sites use setdefault, so a caller that already chose one keeps it --
    which is what makes a benchmark able to run against a registry of its own."""
    host, state = roots
    (host / "connections.json").write_text("{}", encoding="utf-8")
    mod = load(monkeypatch, raven_home=host, state_root=state)

    env = {"RAVEN_CONNECTIONS": "/chosen/by/the/caller.json"}
    env.setdefault("RAVEN_CONNECTIONS", str(mod.connections_registry()))

    assert env["RAVEN_CONNECTIONS"] == "/chosen/by/the/caller.json"
