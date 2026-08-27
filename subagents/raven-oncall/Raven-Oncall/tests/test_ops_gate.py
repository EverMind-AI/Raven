"""The on-call surface is present when the owner says so, and not otherwise.

Two shapes, and the whole point is that they differ. An owner who never runs
work on another machine should see the tool surface upstream ships: measured
against upstream's own tests, thirteen unconditional tools took the room for
history from 130000 tokens to 127933, and the work-to-watch judgement consumed a
scripted provider reply that a logging test was counting on.

The switch is ``tools.oncall.enabled``, and it is off until the owner turns it
on. Inferring it from the connection registry was tried first: registering a
machine says an installation COULD run work somewhere, never that its owner
wants the apparatus in front of them today, and a condition that is a side
effect of setup leaves nothing to switch off.
"""

import pytest

from raven.agent.tools.shell import ExecTool
from raven.ops.gate import on_call_available


@pytest.fixture
def config_says(monkeypatch):
    """Set ``tools.oncall.enabled`` as the loader would report it.

    Also puts the real gate function back: the suite pins it off (conftest's
    ``_on_call_off_unless_asked``) so no test depends on the developer's own
    config, and this file is the one place that has to exercise the reading
    itself rather than a stand-in for it.
    """
    from raven.ops import gate

    monkeypatch.setattr(gate, "on_call_available", on_call_available)

    def _set(enabled: bool) -> None:
        from types import SimpleNamespace

        from raven.config import loader

        cfg = SimpleNamespace(
            tools=SimpleNamespace(oncall=SimpleNamespace(enabled=enabled))
        )
        monkeypatch.setattr(loader, "load_config", lambda: cfg)

    return _set


def test_off_is_the_default(config_says) -> None:
    config_says(False)

    assert on_call_available() is False


def test_the_owner_turning_it_on_is_what_puts_it_there(config_says) -> None:
    config_says(True)

    assert on_call_available() is True


def test_an_unreadable_config_does_not_decide_by_crashing(monkeypatch) -> None:
    """This sits in front of tool registration and of every path-touching tool
    result, so a half-written config must leave ordinary work running."""
    from raven.config import loader

    def _boom():
        raise RuntimeError("config is mid-write")

    monkeypatch.setattr(loader, "load_config", _boom)

    assert on_call_available() is False


def test_exec_hides_the_machine_parameter_until_it_is_on(config_says) -> None:
    """The parameter names ops_connections, so offering it with the surface off
    would point at a tool this instance does not have."""
    config_says(False)
    assert "machine" not in ExecTool().parameters["properties"]

    config_says(True)
    assert "machine" in ExecTool().parameters["properties"]


def test_exec_description_says_only_what_applies(config_says) -> None:
    config_says(False)
    plain = ExecTool().description
    assert "ops_connections" not in plain and "ops_submit" not in plain

    config_says(True)
    opened = ExecTool().description
    assert "ops_connections" in opened and "ops_submit" in opened
    assert opened.startswith(plain.rstrip()), "the plain sentence stays, the rest is added"


def test_the_schema_follows_a_switch_thrown_mid_session(config_says) -> None:
    """Read per call, not fixed at construction: the loop rebuilds the tool schema
    every turn, and an owner who turns it on now should not have to restart."""
    config_says(False)
    tool = ExecTool()
    assert "machine" not in tool.parameters["properties"]

    config_says(True)

    assert "machine" in tool.parameters["properties"], "same tool object, new answer"


def test_connecting_a_machine_does_not_turn_it_on(config_says, tmp_path, monkeypatch) -> None:
    """Registering machines, and even having campaigns on disk, decides nothing.

    An earlier version inferred the switch from the connection registry, so
    connecting a simulation platform silently turned the whole apparatus on and
    left the owner no way to say no. The switch is the only thing that decides;
    this pins that, because inference is an easy thing to reintroduce as a
    convenience.
    """
    from raven.config import paths as config_paths
    from raven.ops import connections

    store = connections.store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        '[{"id": "conn_fluent", "name": "the solver box", "host": "h", '
        '"port": 22, "user": "root"}]',
        encoding="utf-8",
    )
    ops_home = tmp_path / "ops"
    (ops_home / "a-campaign").mkdir(parents=True)
    (ops_home / "a-campaign" / "meta.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config_paths, "get_ops_home", lambda: ops_home)

    config_says(False)

    assert on_call_available() is False
    assert "machine" not in ExecTool().parameters["properties"]

    store.unlink(missing_ok=True)


def test_the_switch_is_off_in_a_stock_config() -> None:
    """The default in the schema, not just in this file's fixtures.

    A default of on would put the cost in front of every raven that never runs
    work on another machine, which is the thing this exists to prevent.
    """
    from raven.config.schema import ToolsConfig

    assert ToolsConfig().oncall.enabled is False
