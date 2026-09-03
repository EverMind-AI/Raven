"""build_sentinel_stack, run for real rather than mocked.

Every other test that meets this builder replaces it: the CLI tests stub it to
hand back a mock runner, and the unit tests construct the sentinel's parts
directly from their own modules. So the one import path only the builder takes
-- the sentinel package FACE, inside the enabled branch -- ran in no test at
all, and a name the face forgot to export crashed the real gateway at startup
while 13,204 tests stayed green. These run the builder for real, both branches.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from raven.config.raven import RavenConfig
from raven.config.schema import Config
from raven.core.proactive_stack import build_sentinel_stack
from raven.session.manager import SessionManager


class _Provider:
    async def chat(self, **kwargs):
        return SimpleNamespace(content="ok", tool_calls=[])


def _base_config(tmp_path: Path) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    return config


def test_disabled_sentinel_builds_nothing() -> None:
    sentinel_cfg = RavenConfig().sentinel

    assert sentinel_cfg.enabled is False
    assert build_sentinel_stack(Config(), sentinel_cfg, None, None) == (None, None, None)


def test_an_enabled_stack_builds_for_real_and_carries_its_assembly(tmp_path: Path, monkeypatch) -> None:
    """The enabled branch does its heavy imports through the package face; a
    face that dropped a name fails HERE now, not at gateway startup."""
    monkeypatch.setattr("raven.config.paths.get_sentinel_dir", lambda: tmp_path / "sentinel")
    config = _base_config(tmp_path)
    sentinel_cfg = RavenConfig(sentinel={"enabled": True}).sentinel
    sessions = SessionManager(tmp_path / "ws")

    runner, response_modifier, on_user_inbound = build_sentinel_stack(
        config,
        sentinel_cfg,
        sessions,
        _Provider(),
        state_path=tmp_path / "sentinel" / "state.json",
    )

    assert runner is not None
    assert runner.assembly is not None, "the post-AgentLoop attach step rides on the runner"
    assert type(runner.assembly).__name__ == "SentinelAssembly"
    assert callable(on_user_inbound)
