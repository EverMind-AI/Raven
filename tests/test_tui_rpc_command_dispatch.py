"""Tests for ``command.dispatch`` -- the slash fallback ui-tui reaches for last.

The client only calls this after ``slash.exec`` rejects, and then renders
whatever comes back. Every branch therefore has to return a shape
``asCommandDispatch`` accepts, or the user sees "invalid response".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.tui_rpc.errors import NotDispatchCompatibleError
from raven.tui_rpc.methods import command_dispatch as mod
from raven.tui_rpc.methods.command_dispatch import command_dispatch


class _Meta:
    def __init__(self, name: str) -> None:
        self.name = name
        self.path = Path(f"/skills/{name}/SKILL.md")


class _Registry:
    def __init__(self, bodies: dict[str, str]) -> None:
        self._bodies = bodies

    def list_all(self) -> list[_Meta]:
        return [_Meta(n) for n in self._bodies]

    def get(self, name: str) -> _Meta | None:
        return _Meta(name) if name in self._bodies else None

    def get_body(self, name: str) -> str | None:
        return self._bodies.get(name)


def _factory(bodies: dict[str, str] | None = None):
    registry = _Registry(bodies) if bodies is not None else None
    skills = type("_S", (), {"registry": registry})()
    context = type("_C", (), {"skills": skills})()
    loop = type("_Loop", (), {"context": context})()
    return lambda: loop


@pytest.fixture
def _no_cli(monkeypatch) -> None:
    """Every CLI verb is unknown, so the skill branch is the only one that can fire."""

    async def _raise(*_args, **_kwargs):
        raise NotDispatchCompatibleError("not a verb")

    monkeypatch.setattr(mod, "cli_dispatch", _raise)


async def test_a_skill_name_resolves_to_a_skill_load(_no_cli) -> None:
    result = await command_dispatch(
        {"name": "triage", "arg": ""},
        agent_loop_factory=_factory({"triage": "Triage the inbox."}),
    )
    assert result == {"type": "skill", "name": "triage", "message": "Triage the inbox."}


async def test_the_argument_is_carried_into_the_skill_message(_no_cli) -> None:
    result = await command_dispatch(
        {"name": "triage", "arg": "the auth module"},
        agent_loop_factory=_factory({"triage": "Triage the inbox."}),
    )
    assert result["message"] == "Triage the inbox.\n\nthe auth module"


async def test_an_unknown_name_reports_it_rather_than_rejecting(_no_cli) -> None:
    result = await command_dispatch({"name": "nonsense"}, agent_loop_factory=_factory({}))
    assert result == {"type": "exec", "output": "unknown command: /nonsense"}


async def test_a_cli_verb_comes_back_as_exec_output(monkeypatch) -> None:
    async def _ok(params, **_kwargs):
        assert params["argv"] == ["status", "--json"]
        return {"stdout": "all good", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(mod, "cli_dispatch", _ok)
    result = await command_dispatch({"name": "status", "arg": "--json"}, agent_loop_factory=_factory({}))
    assert result == {"type": "exec", "output": "all good"}


async def test_a_failing_cli_verb_includes_its_stderr(monkeypatch) -> None:
    async def _fail(_params, **_kwargs):
        return {"stdout": "partial", "stderr": "boom", "exit_code": 1}

    monkeypatch.setattr(mod, "cli_dispatch", _fail)
    result = await command_dispatch({"name": "status"}, agent_loop_factory=_factory({}))
    assert result["output"] == "partial\nboom"


async def test_an_empty_name_never_reaches_the_cli(_no_cli) -> None:
    assert await command_dispatch({"name": "  "}, agent_loop_factory=_factory({})) == {
        "type": "exec",
        "output": "(empty command)",
    }


async def test_an_unparseable_argument_is_reported_not_raised(_no_cli) -> None:
    result = await command_dispatch({"name": "status", "arg": 'unbalanced "'}, agent_loop_factory=_factory({}))
    assert result["type"] == "exec"
    assert "could not parse" in result["output"]


async def test_it_still_answers_with_no_live_loop(_no_cli) -> None:
    result = await command_dispatch({"name": "whatever"}, agent_loop_factory=None)
    assert result == {"type": "exec", "output": "unknown command: /whatever"}
