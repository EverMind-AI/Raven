"""Where the span log goes, and why ``--config`` has to reach it.

Everything else about an instance is derived from where its config file sits:
cron store, logs, ops ledgers. Tracing was not -- ``state_dir()`` read two
environment variables and then hard-coded ``~/.raven/traces``, so two instances
started with different ``--config`` appended to one span log.

That file is where token counts come from: ``raven-arm-metrics.py`` sums every
``llm.usage.*`` span in it. Two arms sharing it makes the sum the sum of both,
which is not a number anyone can use, and nothing says so at the time. The
workaround was an environment variable typed on every launch, and forgetting it
on one of two terminals is silent.

Deriving the default from the config path costs nothing in compatibility: with
no ``--config``, ``get_data_dir()`` is ``~/.raven``, so the derived value is
byte-identical to the constant it replaces. Both environment variables keep
winning, in the order they already did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.tracing import config as tracing_config


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("RAVEN_TRACING_DIR", raising=False)
    monkeypatch.delenv("RAVEN_HOME", raising=False)


def _config_at(monkeypatch, path: Path) -> None:
    monkeypatch.setattr("raven.config.paths.get_config_path", lambda: path)


def test_it_follows_the_config_file(monkeypatch, tmp_path):
    inst = tmp_path / ".raven-m3"
    inst.mkdir()
    _config_at(monkeypatch, inst / "config.json")
    assert tracing_config.state_dir() == inst / "traces"


def test_two_instances_do_not_share_a_span_log(monkeypatch, tmp_path):
    """The whole point: the token metric sums one file per arm."""
    seen = set()
    for name in ("m3", "m9"):
        inst = tmp_path / f".raven-{name}"
        inst.mkdir()
        _config_at(monkeypatch, inst / "config.json")
        seen.add(tracing_config.state_dir())
    assert len(seen) == 2


def test_the_default_location_does_not_move(monkeypatch, tmp_path):
    """A user who never passes --config must keep the directory they have."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _config_at(monkeypatch, tmp_path / ".raven" / "config.json")
    assert tracing_config.state_dir() == tmp_path / ".raven" / "traces"


def test_the_env_override_still_wins(monkeypatch, tmp_path):
    inst = tmp_path / ".raven-m3"
    inst.mkdir()
    _config_at(monkeypatch, inst / "config.json")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path / "elsewhere"))
    assert tracing_config.state_dir() == tmp_path / "elsewhere"


def test_raven_home_still_wins_over_the_derived_default(monkeypatch, tmp_path):
    """Ordering is unchanged, so an existing RAVEN_HOME setup behaves as before."""
    inst = tmp_path / ".raven-m3"
    inst.mkdir()
    _config_at(monkeypatch, inst / "config.json")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    assert tracing_config.state_dir() == tmp_path / "home" / "traces"


def test_an_unreadable_config_path_falls_back_rather_than_raising(monkeypatch, tmp_path):
    """Tracing must never be the reason a run cannot start."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    def _boom():
        raise RuntimeError("no config path in this process")

    monkeypatch.setattr("raven.config.paths.get_config_path", _boom)
    assert tracing_config.state_dir() == tmp_path / ".raven" / "traces"
