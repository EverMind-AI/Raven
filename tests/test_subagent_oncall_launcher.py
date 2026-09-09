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


# ---- the rendered config's two lifetimes (per-pid for acp, fixed for cli) ----


@pytest.fixture
def rooted(launcher, tmp_path: Path, monkeypatch):
    """The launcher with STATE_ROOT pointed at tmp_path and a stub credential.

    `render_config` refuses to write a config it knows starts a child that
    cannot answer, so it exits when no key is reachable -- correct, and it made
    these two cases pass on a developer box (which has one) and fail in CI
    (which does not). The key is stubbed rather than the check bypassed: what
    these assert is the file's NAME and lifetime, and a real merge is the path
    that produces it.
    """
    monkeypatch.setattr(launcher, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "RENDERED_CONFIG", tmp_path / "config.json")
    monkeypatch.setenv("ONCALL_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(launcher, "HOST_CONFIG", tmp_path / "no-host-config.json")
    return launcher


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "source.json"
    src.write_text(json.dumps({"agents": {"defaults": {}}}), encoding="utf-8")
    return src


def test_the_cli_render_keeps_the_fixed_name(rooted, tmp_path: Path) -> None:
    """The fixed name is the CLI path's live contract: the resident wake shell
    re-reads exactly that path between runs. Not legacy, and never renamed."""
    out = rooted.render_config(_source(tmp_path))
    assert out == tmp_path / "config.json"
    assert out.is_file()


def test_an_acp_render_is_its_own_file_and_leaves_the_fixed_name_alone(rooted, tmp_path: Path) -> None:
    """Two windows mean two servers on one STATE_ROOT. Each renders its own
    per-pid copy, so one exit's unlink can no longer pull the config out from
    under the other -- the failure that dropped a running server onto shipped
    defaults, silently."""
    import os

    fixed = tmp_path / "config.json"
    fixed.write_text("{}", encoding="utf-8")
    dest = tmp_path / f"config.acp.{os.getpid()}.json"
    out = rooted.render_config(_source(tmp_path), dest=dest)
    assert out == dest and out.is_file()
    assert fixed.read_text(encoding="utf-8") == "{}"  # untouched


def test_the_sweep_takes_dead_pids_and_leaves_live_ones_and_the_fixed_name(rooted, tmp_path: Path) -> None:
    """Liveness, not age -- a server serves as long as its sessions do. And the
    glob must never reach `config.json`: deleting that strands a cli-hosted
    campaign, the exact opposite mistake of the one the sweep cleans up after."""
    import os

    live = tmp_path / f"config.acp.{os.getpid()}.json"  # this test's own pid: alive
    dead = tmp_path / "config.acp.999999999.json"  # beyond pid_max everywhere
    junk = tmp_path / "config.acp.notapid.json"  # unparseable -> treated as dead
    fixed = tmp_path / "config.json"  # the cli contract
    for f in (live, dead, junk, fixed):
        f.write_text("{}", encoding="utf-8")

    rooted.sweep_stale_acp_renders()

    assert live.is_file(), "a live server's config was swept"
    assert not dead.exists(), "a dead launcher's config survived the sweep"
    assert not junk.exists(), "an unparseable pid should read as dead"
    assert fixed.is_file(), "the sweep reached the cli path's fixed name"
