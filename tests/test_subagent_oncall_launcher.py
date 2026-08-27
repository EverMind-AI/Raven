"""Unit tests for the host-side Raven-Oncall launcher's watch footer.

The launcher (subagents/raven-oncall/run.py) appends a "watch state" block to
every reply: whether anything will actually come back for this campaign, which
is the one thing the agent cannot say about itself -- it has no view of the cron
store its own turn just wrote into.

That makes the footer load-bearing in a way the reply is not. An operator who
reads "no wake is scheduled: nothing will come back on its own" stops waiting.
"""

import importlib.util
import json
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO_ROOT / "subagents" / "raven-oncall" / "run.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("raven_oncall_launcher", _LAUNCHER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _install(tmp_path: Path, state: dict) -> Path:
    """A config whose cron store holds one job with the given state block."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({}), encoding="utf-8")
    store = tmp_path / "cron" / "jobs.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "j1",
                        "name": "ops:beam-limit-load:r2",
                        "enabled": True,
                        "schedule": {"kind": "at", "atMs": state.get("nextRunAtMs")},
                        "payload": {"message": "go", "channel": "tui", "to": "direct"},
                        "state": state,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return config


def test_a_scheduled_wake_is_reported(launcher, tmp_path: Path) -> None:
    """The store serialises ``nextRunAtMs``; read as ``next_run_at_ms`` this
    matched nothing, so every job fell through and the footer announced "no wake
    is scheduled" on every turn that had just scheduled one.

    Measured 2026-08-26: the operator was told the loop was dead while the wake
    it had armed fired on time, twice.
    """
    due = int(time.time() * 1000) + 300_000
    config = _install(tmp_path, {"nextRunAtMs": due, "claimedByPid": None})

    wakes = launcher.pending_wakes(config)

    assert len(wakes) == 1
    assert "ops:beam-limit-load:r2" in wakes[0]
    assert "(in 4m" in wakes[0] or "(in 5m" in wakes[0]


def test_the_dataclass_spelling_is_read_too(launcher, tmp_path: Path) -> None:
    """A store written from the python field names must not read as empty."""
    due = int(time.time() * 1000) + 60_000
    config = _install(tmp_path, {"next_run_at_ms": due})

    assert len(launcher.pending_wakes(config)) == 1


def test_no_wake_really_means_none(launcher, tmp_path: Path) -> None:
    """The warning has to stay reachable: a job with no next run is not a wake,
    and a footer that never warned would be as useless as one that always did."""
    config = _install(tmp_path, {"nextRunAtMs": None})

    assert launcher.pending_wakes(config) == []


def test_an_overdue_wake_is_reported_as_overdue(launcher, tmp_path: Path) -> None:
    """Due-and-unfired is a different state from not-scheduled, and the operator
    acts differently on each: one waits, the other goes looking for the shell."""
    config = _install(tmp_path, {"nextRunAtMs": int(time.time() * 1000) - 120_000})

    wakes = launcher.pending_wakes(config)

    assert len(wakes) == 1
    assert "overdue" in wakes[0]
