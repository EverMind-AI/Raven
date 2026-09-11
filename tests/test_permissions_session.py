"""A conversation's own permission mode: memory first, its record second, the default last."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven.permissions import session as session_module
from raven.permissions.session import session_mode, set_session_mode, set_session_mode_restorer
from raven.session.manager import SessionManager


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(session_module, "_MODES", {})
    monkeypatch.setattr(session_module, "_LOOKED_UP", set())
    monkeypatch.setattr(session_module, "_RESTORE", None)


def test_without_a_restorer_only_memory_answers(fresh):
    assert session_mode("c-1") is None
    assert set_session_mode("c-1", "full") is None
    assert session_mode("c-1") == "full"
    assert set_session_mode("c-1", None) == "full"
    assert session_mode("c-1") is None


def test_an_unknown_conversation_is_looked_up_once_and_the_answer_kept(fresh):
    stored = {"c-1": "smart"}
    calls: list[str] = []

    def restore(cid: str) -> str | None:
        calls.append(cid)
        return stored.get(cid)

    set_session_mode_restorer(restore)
    assert session_mode("c-1") == "smart"
    assert session_mode("c-1") == "smart"
    assert session_mode("c-2") is None
    assert session_mode("c-2") is None
    assert calls == ["c-1", "c-2"]


def test_a_switch_outranks_the_record_and_reports_the_restored_value_as_previous(fresh):
    set_session_mode_restorer(lambda cid: "smart")
    assert set_session_mode("c-1", "ask") == "smart"
    assert session_mode("c-1") == "ask"
    # Returned to the default: the record is not consulted again in this process.
    assert set_session_mode("c-1", None) == "ask"
    assert session_mode("c-1") is None


def test_a_record_that_cannot_be_read_means_the_default(fresh):
    def broken(cid: str) -> str | None:
        raise OSError("disk")

    set_session_mode_restorer(broken)
    assert session_mode("c-1") is None


def test_the_loop_reads_the_mode_off_the_session_record(tmp_path):
    from raven.agent.loop.wiring import WiringMixin

    sessions = SessionManager(tmp_path)
    record = sessions.get_or_create("tui:saved")
    record.metadata["permissions_mode"] = "full"
    sessions.save(record)
    host = SimpleNamespace(sessions=SessionManager(tmp_path))

    assert WiringMixin.stored_session_permission_mode(host, "tui:saved") == "full"
    assert WiringMixin.stored_session_permission_mode(host, "tui:never") is None
    assert WiringMixin.stored_session_permission_mode(SimpleNamespace(), "tui:saved") is None


def test_an_agent_loop_registers_itself_as_the_reader(tmp_path, fresh):
    from raven.agent.loop.bundles import EngineWiring
    from raven.agent.loop.main import AgentLoop
    from raven.config.raven import ContextConfig, SkillForgeConfig
    from raven.providers.base import LLMResponse

    class Provider:
        provider_name = "vendor"

        def get_default_model(self) -> str:
            return "boot/default"

        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            return LLMResponse(content="ok", finish_reason="stop")

    loop = AgentLoop(
        provider=Provider(),
        workspace=tmp_path,
        model="boot/model",
        engine=EngineWiring(context_config=ContextConfig(), skill_forge_config=SkillForgeConfig()),
    )
    record = loop.sessions.get_or_create("tui:restart")
    record.metadata["permissions_mode"] = "smart"
    loop.sessions.save(record)

    # What a process that never saw the switch reads on the conversation's first tool call.
    assert session_mode("tui:restart") == "smart"
    assert session_mode("tui:fresh") is None


def test_a_session_grant_holds_only_when_every_key_was_granted():
    from raven.permissions.session import remember_allowed, session_allows

    session_module._GRANTS.clear()
    assert session_allows("c1", ["k1"]) is False
    remember_allowed("c1", ["k1", "k2"])
    assert session_allows("c1", ["k1"]) is True
    assert session_allows("c1", ["k1", "k2"]) is True
    assert session_allows("c1", ["k1", "k3"]) is False
    assert session_allows("c2", ["k1"]) is False
    assert session_allows("c1", []) is False
    session_module._GRANTS.clear()
    assert session_allows("c1", ["k1"]) is False
