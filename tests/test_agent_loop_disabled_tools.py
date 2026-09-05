"""Where the loop's off switches come from, and which of them can be reversed.

Two sources meet in ``AgentLoop._withheld_tool_names``: the config file, read live
so a switch flipped on the settings page takes effect on the next turn, and a list
handed to the constructor by an eval harness that has no config file at all. They
are composed in one place and nowhere else, and the composition is the part with a
failure mode neither half has -- a name that arrives through the constructor is
fixed for the life of the process, so copying the file's list in there would make
every switch that was on at launch permanent, whatever the file says afterwards.

The halves have their own files (``test_config_live.py`` for the read,
``test_tool_registry_withheld.py`` for what the registry does with the answer);
this one is only about the seam.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.agent.loop.main import AgentLoop
from tests._wiring import wire


class _StubProvider:
    """Construction reads the default model; no turn is ever run here."""

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs):  # pragma: no cover - never invoked
        raise NotImplementedError


def _write_switches(path: Path, names: list[str]) -> None:
    path.write_text(json.dumps({"tools": {"disabledTools": names}}), encoding="utf-8")


def _loop(tmp_path: Path, **kwargs) -> AgentLoop:
    return AgentLoop(provider=_StubProvider(), workspace=tmp_path / "ws", **wire(**kwargs))


def _offered(loop: AgentLoop) -> set[str]:
    return {d["function"]["name"] for d in loop.tools.get_definitions()}


class TestTheFileIsTheAuthority:
    """The direction that used to be impossible. Asserted through the assembled
    array rather than through ``_withheld_tool_names``, because the array is what a
    turn actually sends and the helper is an implementation detail of it."""

    def test_a_switch_flipped_after_startup_is_honoured(self, tmp_path: Path, monkeypatch) -> None:
        cfg = tmp_path / "config.json"
        _write_switches(cfg, [])
        monkeypatch.setattr("raven.home._current_config_path", cfg)
        loop = _loop(tmp_path)
        assert "grep" in _offered(loop)

        _write_switches(cfg, ["grep"])

        assert "grep" not in _offered(loop)

    def test_and_so_is_flipping_it_back(self, tmp_path: Path, monkeypatch) -> None:
        """The regression this file exists for: a tool that was off when the
        process started must be reachable again once the file stops saying so. The
        boot-time copy of the config's own list outvoted the live read, so the
        page could remove the name and nothing changed until the next restart --
        and then only until the next time it was switched off."""
        cfg = tmp_path / "config.json"
        _write_switches(cfg, ["grep"])
        monkeypatch.setattr("raven.home._current_config_path", cfg)
        loop = _loop(tmp_path)
        assert "grep" not in _offered(loop)

        _write_switches(cfg, [])

        assert "grep" in _offered(loop)

    def test_the_config_list_is_not_copied_at_construction(self, tmp_path: Path, monkeypatch) -> None:
        """The same fact stated where it broke, so a future caller re-adding
        ``disabled_tools=config.tools.disabled_tools`` at a CLI site is caught by
        name rather than by the symptom two layers away."""
        cfg = tmp_path / "config.json"
        _write_switches(cfg, ["grep"])
        monkeypatch.setattr("raven.home._current_config_path", cfg)

        loop = _loop(tmp_path)

        assert loop._disabled_tools == set()


class TestTheHarnessListStillHolds:
    """``benchmarks/appworld/agent_cli.py`` passes ten names directly and has no
    config file behind them, so the constructor path cannot simply be deleted."""

    def test_a_constructor_name_is_withheld_with_no_config_at_all(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")

        loop = _loop(tmp_path, disabled_tools=["grep"])

        assert "grep" not in _offered(loop)

    def test_it_survives_a_config_that_does_not_mention_it(self, tmp_path: Path, monkeypatch) -> None:
        """A union rather than a fallback: an empty list in the file is a real
        answer, and it must not hand the harness back a tool it excluded."""
        cfg = tmp_path / "config.json"
        _write_switches(cfg, [])
        monkeypatch.setattr("raven.home._current_config_path", cfg)

        loop = _loop(tmp_path, disabled_tools=["grep"])

        assert "grep" not in _offered(loop)

    def test_the_two_sources_add_up(self, tmp_path: Path, monkeypatch) -> None:
        cfg = tmp_path / "config.json"
        _write_switches(cfg, ["read_file"])
        monkeypatch.setattr("raven.home._current_config_path", cfg)

        loop = _loop(tmp_path, disabled_tools=["grep"])
        offered = _offered(loop)

        assert "grep" not in offered
        assert "read_file" not in offered
        assert "write_file" in offered
