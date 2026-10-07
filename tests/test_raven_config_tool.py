"""``raven_config``: the agent reading and changing its own configuration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from raven.agent.tools.raven_config import RavenConfigTool
from raven.config import self_surface as surface
from raven.config.schema import PermissionsConfig
from raven.config.self_surface import Effect
from raven.contracts.permissions import Allow, ApprovalChoice, ApprovalOutcome, Deny, NeedsApproval
from raven.permissions.builtin import BuiltinRulings
from raven.permissions.gate import PermissionGate
from raven.permissions.turn import start_permission_turn


@pytest.fixture(autouse=True)
def _no_bound_turn():
    """A turn one test binds must not be the next test's conversation."""
    from raven.permissions import turn

    turn._TURN.set(None)
    yield
    turn._TURN.set(None)


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.json"
    path.write_text(json.dumps({"tools": {"exec": {"timeout": 60}}, "providers": {"openrouter": {"apiKey": "sk-x"}}}))
    monkeypatch.setenv("RAVEN_HOME", str(home))
    return path


class Calls:
    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.replies = replies or {}

    async def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        reply = self.replies.get(method, {"applied": True, "previous": None})
        if isinstance(reply, Exception):
            raise reply
        return reply


def _run(tool: RavenConfigTool, **kwargs: Any):
    return tool.execute(**kwargs)


@pytest.mark.asyncio
async def test_describe_is_one_index_of_every_setting_with_its_value(config_file):
    """The model used to walk describe -> describe <section> -> get -> set for a
    one-line change. One read now carries the path, the current value and when
    a change applies."""
    tool = RavenConfigTool()
    root = await _run(tool, action="describe")
    for section in ("[model]", "[tools]", "[channels]", "[subagents]", "[security]"):
        assert section in root
    assert "  tools.exec.timeout = 60 [int 5.., next turn] Seconds a shell command may run" in root
    assert "note: this process lent no settings writer" in root
    assert "sk-x" not in root
    tools = await _run(tool, action="describe", path="tools")
    assert "[tools]" in tools and "[model]" not in tools
    one = json.loads(await _run(tool, action="describe", path="tools.exec.timeout"))
    assert one["type"] == "int" and "next turn" in one["takes_effect"] and one["value"] == 60


def _pending(text: str) -> Any:
    line = next((x for x in text.splitlines() if x.startswith("pending: ")), None)
    return json.loads(line.removeprefix("pending: ")) if line else None


@pytest.mark.asyncio
async def test_get_reports_values_defaults_and_hides_secrets(config_file):
    tool = RavenConfigTool()
    got = json.loads(await _run(tool, action="get", path="tools.exec.timeout"))
    assert got == {"tools.exec.timeout": 60}
    got = json.loads(await _run(tool, action="get", path="agents.defaults.temperature"))
    assert got == {"agents.defaults.temperature": {"default": 0.1}}
    got = json.loads(await _run(tool, action="get", path="providers.openrouter.apiKey"))
    assert got == {"providers.openrouter.apiKey": "set"}
    got = json.loads(await _run(tool, action="get", path="providers.anthropic.apiKey"))
    assert got == {"providers.anthropic.apiKey": "not set"}
    section = await _run(tool, action="get", path="providers")
    assert "sk-x" not in section
    assert json.loads(section)["providers.openrouter.apiKey"] == "set"


@pytest.mark.asyncio
async def test_a_section_or_instance_read_expands_to_what_is_configured(config_file):
    """A wildcard entry is read once per configured instance; an empty answer reads as "none configured"."""
    tool = RavenConfigTool()
    instance = json.loads(await _run(tool, action="get", path="providers.openrouter"))
    assert instance["providers.openrouter.apiKey"] == "set"
    assert set(instance) >= {"providers.openrouter.apiBase", "providers.openrouter.models"}
    assert not any("anthropic" in p for p in json.loads(await _run(tool, action="get", path="providers")))
    below = json.loads(await _run(tool, action="get", path="tools.exec"))
    assert below["tools.exec.timeout"] == 60 and all(p.startswith("tools.exec.") for p in below)
    assert "is not a path" in await _run(tool, action="get", path="providers.nobody")


@pytest.mark.asyncio
async def test_a_raw_setting_is_written_and_a_reload_left_pending(config_file):
    tool = RavenConfigTool()
    reply = await _run(tool, action="set", path="agents.defaults.temperature", value="0.7")
    assert "reload" in reply and "Pending" in reply
    assert json.loads(config_file.read_text())["agents"]["defaults"]["temperature"] == 0.7
    assert _pending(await _run(tool, action="describe")) == {"reload": ["agents.defaults.temperature"]}


@pytest.mark.asyncio
async def test_restart_without_a_restarter_says_so_and_keeps_pending(config_file):
    tool = RavenConfigTool()
    await _run(tool, action="set", path="agents.defaults.temperature", value="0.7")
    reply = await _run(tool, action="restart")
    assert "cannot restart itself" in reply
    assert _pending(await _run(tool, action="describe"))


@pytest.mark.asyncio
async def test_restart_picks_the_strongest_pending_target_and_clears_it(config_file):
    asked: list[str] = []

    async def restart(target: str) -> str:
        asked.append(target)
        return f"scheduled {target}"

    tool = RavenConfigTool()
    tool.set_restarter(restart)
    await _run(tool, action="set", path="agents.defaults.temperature", value="0.7")
    await _run(tool, action="set", path="sentinel.enabled", value="true")
    assert await _run(tool, action="restart") == "scheduled restart"
    assert asked == ["restart"]
    assert _pending(await _run(tool, action="describe")) is None


@pytest.mark.asyncio
async def test_a_reload_keeps_what_only_a_restart_applies(config_file):
    async def restart(target: str) -> str:
        return target

    tool = RavenConfigTool()
    tool.set_restarter(restart)
    await _run(tool, action="set", path="agents.defaults.temperature", value="0.7")
    await _run(tool, action="set", path="sentinel.enabled", value="true")
    await _run(tool, action="restart", value="reload")
    assert _pending(await _run(tool, action="describe")) == {"restart": ["sentinel.enabled"]}


@pytest.mark.asyncio
async def test_a_settings_page_setting_goes_through_the_lent_caller(config_file):
    calls = Calls({"settings.set": {"applied": True, "previous": 60}})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    reply = await _run(tool, action="set", path="tools.exec.timeout", value="120")
    assert calls.calls == [("settings.set", {"key": "tools.exec.timeout", "value": 120})]
    assert "60 -> 120" in reply and "next turn" in reply


@pytest.mark.asyncio
async def test_without_a_caller_a_settings_page_setting_is_refused(config_file):
    tool = RavenConfigTool()
    reply = await _run(tool, action="set", path="tools.exec.timeout", value="120")
    assert reply.startswith("Error:") and "Settings" in reply
    assert json.loads(config_file.read_text())["tools"]["exec"]["timeout"] == 60


@pytest.mark.asyncio
async def test_the_default_model_needs_its_provider(config_file):
    calls = Calls()
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    reply = await _run(tool, action="set", path="agents.defaults.model", value='"x/y"')
    assert reply.startswith("Error:") and calls.calls == []
    await _run(tool, action="set", path="agents.defaults.model", value='{"provider": "openrouter", "model": "x/y"}')
    assert calls.calls == [("config.set", {"key": "model", "value": "x/y", "provider": "openrouter"})]


@pytest.mark.asyncio
async def test_secrets_and_inert_settings_are_not_written(config_file):
    tool = RavenConfigTool()
    reply = await _run(tool, action="set", path="providers.openrouter.apiKey", value='"sk-new"')
    assert "never goes through a tool call" in reply
    reply = await _run(tool, action="set", path="cron.defaultTimezone", value='"UTC"')
    assert "nothing was changed" in reply
    data = json.loads(config_file.read_text())
    assert data["providers"]["openrouter"]["apiKey"] == "sk-x" and "cron" not in data


@pytest.mark.asyncio
async def test_a_wrong_value_is_refused_before_anything_is_written(config_file):
    tool = RavenConfigTool()
    reply = await _run(tool, action="set", path="routing.profile", value='"cheap"')
    assert reply.startswith("Error:") and "choices" not in json.loads(config_file.read_text())


@pytest.mark.asyncio
async def test_unset_returns_a_raw_setting_to_its_default(config_file):
    tool = RavenConfigTool()
    await _run(tool, action="set", path="agents.defaults.temperature", value="0.7")
    await _run(tool, action="unset", path="agents.defaults.temperature")
    assert "temperature" not in json.loads(config_file.read_text())["agents"]["defaults"]


@pytest.mark.asyncio
async def test_a_running_channel_is_rebuilt_when_a_field_changes(config_file):
    data = json.loads(config_file.read_text())
    data["channels"] = {"telegram": {"enabled": True, "token": "t"}}
    config_file.write_text(json.dumps(data))
    calls = Calls({"channels.configure": {"applied": True, "outcome": "restarted"}})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    reply = await _run(tool, action="set", path="channels.telegram.allowFrom", value='["alice"]')
    assert calls.calls == [
        ("channels.configure", {"name": "telegram", "fields": {"allow_from": ["alice"]}, "enabled": True})
    ]
    assert "restarted" in reply
    reply = await _run(tool, action="set", path="channels.telegram.token", value='"new"')
    assert "secret" in reply and len(calls.calls) == 1


@pytest.mark.asyncio
async def test_a_channel_is_switched_through_the_gateway(config_file):
    calls = Calls({"channels.configure": {"applied": True, "outcome": "started"}})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    await _run(tool, action="set", path="channels.telegram.enabled", value="false")
    assert calls.calls == [("channels.configure", {"name": "telegram", "enabled": False})]


@pytest.mark.asyncio
async def test_sub_agents_go_through_the_roster_methods(config_file):
    rows = {
        "rows": [
            {
                "name": "codex",
                "kind": "acp",
                "enabled": True,
                "configured": True,
                "model_source": "agent",
                "model_choices": ["gpt-6"],
            },
            {"name": "Raven", "kind": "builtin", "enabled": True, "builtin": True, "model_source": "raven"},
        ]
    }
    calls = Calls({"subagents.list": rows, "subagents.update": {"updated": True}})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    described = json.loads(await _run(tool, action="describe", path="subagents.codex"))
    assert described["model_choices"] == ["gpt-6"]
    await _run(tool, action="set", path="subagents.codex.model", value='"gpt-6"')
    await _run(tool, action="set", path="subagents.Raven.model", value='{"provider": "openrouter", "model": "a/b"}')
    await _run(tool, action="set", path="subagents.codex.model", value="null")
    updates = [p for m, p in calls.calls if m == "subagents.update"]
    assert updates == [
        {"name": "codex", "model": "gpt-6"},
        {"name": "Raven", "model": "a/b", "provider": "openrouter"},
        {"name": "codex", "clear_model": True},
    ]


@pytest.mark.asyncio
async def test_a_refusal_from_the_writer_reaches_the_model(config_file):
    calls = Calls({"settings.set": RuntimeError("tools.exec.timeout must be at most 3600")})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    reply = await _run(tool, action="set", path="tools.exec.timeout", value="3000")
    assert reply.startswith("Error:") and "at most 3600" in reply


def test_the_description_points_at_the_guide_only_when_it_exists():
    assert "local/raven-self-config" in RavenConfigTool().description
    assert "Read skill" not in RavenConfigTool(guide_skill_id=None).description


# -- the gate ------------------------------------------------------------------


class _Responder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def await_approval(self, **kwargs: Any) -> ApprovalOutcome:
        self.calls.append(kwargs)
        return ApprovalOutcome(ApprovalChoice.ALLOW_SESSION)


def _gate(config: PermissionsConfig) -> PermissionGate:
    return PermissionGate(config_source=lambda: config, builtin=BuiltinRulings())


@pytest.mark.asyncio
async def test_reads_run_without_a_prompt_in_ask_mode():
    decision = await _gate(PermissionsConfig(mode="ask")).check("raven_config", {"action": "get", "path": "tools"})
    assert isinstance(decision, Allow)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask", "smart"])
async def test_every_change_asks_outside_full_access(mode):
    params = {"action": "set", "path": "permissions.mode", "value": '"full"'}
    decision = await _gate(PermissionsConfig(mode=mode)).check("raven_config", params)
    assert isinstance(decision, NeedsApproval)
    assert decision.session_keys == ()
    assert "permissions.mode" in decision.description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        PermissionsConfig(mode="full"),
        PermissionsConfig(mode="ask", tools={"raven_config": "allow"}),
    ],
)
async def test_full_access_and_an_allow_rule_let_a_change_through(config):
    """Seen live: with full access on, every config change still raised a card."""
    start_permission_turn(_Responder(), conversation_id="c-1", turn_id="t-1")
    params = {"action": "set", "path": "tools.exec.timeout", "value": "30"}
    assert isinstance(await _gate(config).check("raven_config", params), Allow)


def _reviewing(monkeypatch, allow: bool) -> list[dict[str, Any]]:
    """Smart mode's reviewer, answering ``allow``; returns what it was shown."""
    from raven.permissions import gate as gate_module
    from raven.permissions.judge import JudgeOutcome

    shown: list[dict[str, Any]] = []

    async def review(provider, **kwargs):
        shown.append(kwargs["params"])
        return JudgeOutcome(allow=allow, reason="r")

    monkeypatch.setattr(gate_module, "review", review)
    return shown


def _smart_gate() -> PermissionGate:
    return PermissionGate(
        config_source=lambda: PermissionsConfig(mode="smart"), builtin=BuiltinRulings(), judge_provider_for=object
    )


@pytest.mark.asyncio
async def test_smart_mode_lets_its_reviewer_approve_an_ordinary_change(monkeypatch):
    shown = _reviewing(monkeypatch, allow=True)
    start_permission_turn(_Responder(), conversation_id="c-1", turn_id="t-1")
    params = {"action": "set", "path": "tools.exec.timeout", "value": "30"}
    assert isinstance(await _smart_gate().check("raven_config", params), Allow)
    # The call as the model made it: prose written for the user, sent along
    # once, read to the reviewer as instructions embedded in the request.
    assert shown == [params]


@pytest.mark.asyncio
async def test_smart_mode_asks_the_user_when_the_reviewer_escalates(monkeypatch):
    _reviewing(monkeypatch, allow=False)
    start_permission_turn(_Responder(), conversation_id="c-1", turn_id="t-1")
    params = {"action": "set", "path": "tools.exec.timeout", "value": "30"}
    decision = await _smart_gate().check("raven_config", params)
    assert isinstance(decision, NeedsApproval) and decision.session_keys == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"action": "set", "path": "permissions.mode", "value": '"full"'},
        {"action": "set", "path": "channels.telegram.allowFrom", "value": '["*"]'},
        {"action": "set", "value": json.dumps({"tools.exec.timeout": 30, "tools.restrictToWorkspace": False})},
        # The key lent on an add rides in its value, not on a path.
        {"action": "add", "path": "subagents", "value": json.dumps({"preset": "pi", "lend_key": "openrouter"})},
        {
            "action": "add",
            "path": "subagents",
            "value": json.dumps([{"preset": "qwen_code"}, {"preset": "pi", "lend_key": "openrouter"}]),
        },
    ],
)
async def test_smart_mode_never_lets_its_reviewer_approve_a_sensitive_change(monkeypatch, params):
    """The reviewer sees the call, not the conversation: it cannot tell the user's
    request from an injected one, and these widen what Raven may do."""
    shown = _reviewing(monkeypatch, allow=True)
    start_permission_turn(_Responder(), conversation_id="c-1", turn_id="t-1")
    assert isinstance(await _smart_gate().check("raven_config", params), NeedsApproval)
    assert shown == []


@pytest.mark.asyncio
async def test_an_unattended_turn_gets_no_review(monkeypatch):
    shown = _reviewing(monkeypatch, allow=True)
    start_permission_turn(None, conversation_id="c-1", turn_id="t-1")
    params = {"action": "set", "path": "tools.exec.timeout", "value": "30"}
    assert isinstance(await _smart_gate().check("raven_config", params), NeedsApproval)
    assert shown == []


@pytest.mark.asyncio
async def test_a_grant_for_the_session_does_not_carry_the_next_change():
    gate = _gate(PermissionsConfig(mode="ask"))
    responder = _Responder()
    start_permission_turn(responder, conversation_id="c-1", turn_id="t-1")
    assert await gate.enforce("raven_config", {"action": "set", "path": "tools.exec.timeout", "value": "30"}) is None
    start_permission_turn(responder, conversation_id="c-1", turn_id="t-2")
    assert await gate.enforce("raven_config", {"action": "set", "path": "tools.exec.timeout", "value": "40"}) is None
    assert len(responder.calls) == 2


@pytest.mark.asyncio
async def test_an_unattended_turn_cannot_change_the_configuration():
    start_permission_turn(None, conversation_id="c-1", turn_id="t-1")
    refusal = await _gate(PermissionsConfig(mode="full")).enforce(
        "raven_config", {"action": "set", "path": "tools.exec.timeout", "value": "30"}
    )
    assert refusal is not None and "not interactive" in refusal.model_text


@pytest.mark.asyncio
async def test_a_user_deny_rule_still_blocks_reads():
    decision = await _gate(PermissionsConfig(tools={"raven_config": "deny"})).check(
        "raven_config", {"action": "get", "path": "tools"}
    )
    assert not isinstance(decision, Allow | NeedsApproval)


# -- what the entrances lend -----------------------------------------------------


class _Dispatcher:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.frames: list[dict[str, Any]] = []
        self.reply = reply

    async def dispatch(self, frame: dict[str, Any]) -> dict[str, Any]:
        self.frames.append(frame)
        return {"jsonrpc": "2.0", "id": frame["id"], **self.reply}


class _Tools:
    def __init__(self, tool: RavenConfigTool) -> None:
        self._tool = tool

    def get(self, name: str):
        return self._tool if name == "raven_config" else None


class _Loop:
    def __init__(self, tool: RavenConfigTool) -> None:
        self.tools = _Tools(tool)


@pytest.mark.asyncio
async def test_the_lent_caller_unwraps_results_and_errors():
    from raven.rpc.bootstrap import _lend_settings_writers

    tool = RavenConfigTool()
    ok = _Dispatcher({"result": {"applied": True}})
    _lend_settings_writers(_Loop(tool), ok)
    assert await tool._call("settings.set", {"key": "k", "value": 1}) == {"applied": True}
    assert ok.frames[0]["method"] == "settings.set"

    bad = _Dispatcher({"error": {"code": -32010, "message": "config_validation", "data": {"detail": "too big"}}})
    _lend_settings_writers(_Loop(tool), bad)
    with pytest.raises(RuntimeError, match="too big"):
        await tool._call("settings.set", {})


@pytest.mark.asyncio
async def test_the_lent_caller_reaches_only_the_settings_methods():
    from raven.rpc.bootstrap import _lend_settings_writers

    tool = RavenConfigTool()
    dispatcher = _Dispatcher({"result": {}})
    _lend_settings_writers(_Loop(tool), dispatcher)
    with pytest.raises(PermissionError):
        await tool._call("fs.read", {"path": "/etc/passwd"})
    assert dispatcher.frames == []


@pytest.mark.asyncio
async def test_the_gateway_restart_waits_for_idle():
    from raven.cli.gateway_commands import _await_idle

    states = iter([{"questions": 1}, {"subagents": 1}, None])
    naps: list[float] = []

    async def sleep(s: float) -> None:
        naps.append(s)

    assert await _await_idle(lambda: next(states), poll_s=1.0, limit_s=10.0, sleep=sleep) is True
    assert naps == [1.0, 1.0, 1.0]

    async def never(s: float) -> None:
        return None

    assert await _await_idle(lambda: {"questions": 1}, poll_s=1.0, limit_s=3.0, sleep=never) is False


@pytest.mark.asyncio
async def test_a_pin_read_shows_the_pin_and_never_the_block_around_it(config_file):
    data = json.loads(config_file.read_text())
    data["skillForge"] = {
        "llmGateModel": "m",
        "llmGateProvider": "p",
        "router": {"hub": {"apiKey": "hub-secret"}},
    }
    config_file.write_text(json.dumps(data))
    tool = RavenConfigTool()
    got = await _run(tool, action="get", path="skillForge")
    assert json.loads(got) == {"skillForge": {"llmGateModel": "m", "llmGateProvider": "p"}}
    assert "hub-secret" not in await _run(tool, action="get", path="skills")


@pytest.mark.asyncio
async def test_a_wildcard_write_does_not_invent_an_instance(config_file):
    tool = RavenConfigTool()
    reply = await _run(tool, action="set", path="tools.mcpServers.nope.enabled", value="false")
    assert reply.startswith("Error:") and "not configured" in reply
    assert "mcpServers" not in json.loads(config_file.read_text())["tools"]


@pytest.mark.asyncio
async def test_restart_with_nothing_pending_does_nothing(config_file):
    asked: list[str] = []

    async def restart(target: str) -> str:
        asked.append(target)
        return target

    tool = RavenConfigTool()
    tool.set_restarter(restart)
    assert "Nothing changed" in await _run(tool, action="restart")
    assert asked == []
    assert await _run(tool, action="restart", value="reload") == "reload"


def test_an_add_card_names_who_is_connected_and_what_is_lent(config_file):
    """An add is laid out as the agents it connects, not as the subagents table before and after.

    Seen live: the card showed the whole subagents block as the old value and
    the call's JSON as the new one, with no word that a key was being lent.
    """
    tool = RavenConfigTool()
    lend = tool.approval_evidence({"action": "add", "value": '{"preset": "pi", "lend_key": "openrouter"}'})
    assert lend["agents"] == [{"preset": "pi", "lend_key": "openrouter", "title": "Pi"}]
    assert "was" not in lend and "value" not in lend
    assert "billed to that key" in lend["sensitive"]
    assert "own quota" not in lend["change"] and "billed to that key" in lend["change"]
    plain = tool.approval_evidence(
        {"action": "add", "value": [{"preset": "codex", "name": "Codex2", "model": "gpt-5"}, {"preset": "pi"}]}
    )
    assert plain["agents"] == [
        {"name": "Codex2", "preset": "codex", "model": "gpt-5", "title": "Codex2"},
        {"preset": "pi", "title": "Pi"},
    ]
    assert "sensitive" not in plain and "own quota" in plain["change"]


def test_redaction_reaches_nested_credentials():
    from raven.config.self_surface import redacted

    assert redacted({"a": {"apiKey": "x", "botToken": "", "n": 1}, "l": [{"password": "p"}]}) == {
        "a": {"apiKey": "set", "botToken": "not set", "n": 1},
        "l": [{"password": "set"}],
    }


def test_the_approval_card_gets_the_change_laid_out(config_file):
    tool = RavenConfigTool()
    assert tool.approval_kind == "config.change"
    view = tool.approval_evidence({"action": "set", "path": "tools.exec.timeout", "value": "300"})
    assert view["setting"] == "tools.exec.timeout"
    assert (view["was"], view["value"], view["effect"]) == ("60", "300", "next_turn")
    assert "tools.exec.timeout" in view["change"]
    reset = tool.approval_evidence({"action": "unset", "path": "tools.exec.timeout"})
    assert "value" not in reset and reset["was"] == "60"
    restart = tool.approval_evidence({"action": "restart", "value": "restart"})
    assert restart["target"] == "restart"
    assert tool.approval_evidence({"action": "restart", "value": '"restart"'})["target"] == "restart"
    tool._pending["gateway.port"] = Effect.RESTART
    assert tool.approval_evidence({"action": "restart"})["target"] == "restart"
    tool._pending.clear()
    reload = tool.approval_evidence({"action": "restart", "value": '"reload"'})
    assert reload["target"] == "reload" and '"' not in reload["change"]
    unwritten = tool.approval_evidence({"action": "set", "path": "agents.defaults.temperature", "value": "0.3"})
    assert (unwritten["was"], unwritten["was_default"]) == ("0.1", True)
    sensitive = tool.approval_evidence({"action": "set", "path": "permissions.mode", "value": '"full"'})
    assert sensitive["sensitive"]


def test_a_prompt_never_prints_a_credential(config_file):
    """The gate asks before the tool refuses a secret, so the card must not carry one."""
    from raven.config.self_surface import change_line

    tool = RavenConfigTool()
    params = {"action": "set", "path": "providers.openrouter.apiKey", "value": '"sk-live-123"'}
    view = tool.approval_evidence(params)
    assert "sk-live-123" not in json.dumps(view) and "sk-live-123" not in change_line(params)
    assert "sk-x" not in json.dumps(view)
    nested = {"action": "add", "path": "subagents", "value": '{"name": "x", "botToken": "t-1"}'}
    assert "t-1" not in json.dumps(tool.approval_evidence(nested))


def test_a_count_of_tokens_is_not_taken_for_a_token(config_file):
    from raven.config.self_surface import redacted

    assert redacted({"maxTokens": 4096, "botToken": "t"}) == {"maxTokens": 4096, "botToken": "set"}
    view = RavenConfigTool().approval_evidence(
        {"action": "set", "path": "agents.defaults.contextWindowTokens", "value": "65536"}
    )
    assert view["value"] == "65536"


@pytest.mark.asyncio
async def test_describe_answers_a_prefix_with_what_sits_under_it(config_file):
    tool = RavenConfigTool()
    below = await _run(tool, action="describe", path="tools.exec")
    assert "  tools.exec.timeout = 60 " in below and "tools.web" not in below
    assert "is not a path" in await _run(tool, action="describe", path="tools.nothing")


@pytest.mark.asyncio
async def test_a_batch_is_checked_whole_before_anything_is_written(config_file):
    tool = RavenConfigTool()
    both = {"tools.exec.timeout": 300, "agents.defaults.maxToolIterations": 80}
    assert "nothing was changed" in await _run(tool, action="set", value=json.dumps(both))
    assert json.loads(config_file.read_text())["tools"]["exec"]["timeout"] == 60

    calls = Calls()
    tool.set_rpc_caller(calls)
    bad = json.dumps({"tools.exec.timeout": 300, "agents.defaults.maxToolIterations": "many"})
    assert (await _run(tool, action="set", value=bad)).startswith("Error")
    assert json.loads(config_file.read_text())["tools"]["exec"]["timeout"] == 60 and calls.calls == []

    reply = await _run(tool, action="set", value=json.dumps(both))
    assert calls.calls == [
        ("settings.set", {"key": "tools.exec.timeout", "value": 300}),
        ("settings.set", {"key": "agents.defaults.maxToolIterations", "value": 80}),
    ]
    assert reply.count("Set ") == 2


@pytest.mark.asyncio
async def test_a_secret_is_asked_for_on_the_card_and_reported_by_whether_it_is_set(config_file):
    """The card takes the key and the host writes it; the tool learns only saved or skipped."""
    from raven.contracts.asking import CredentialOutcome

    asked: list[object] = []

    class Card:
        def __init__(self, outcome: CredentialOutcome) -> None:
            self.outcome = outcome

        async def request_credential(self, *, conversation_id: str, turn_id: str, request) -> CredentialOutcome:
            asked.append((conversation_id, request))
            return self.outcome

    tool = RavenConfigTool()
    calls = Calls()
    tool.set_rpc_caller(calls)
    batch = json.dumps({"tools.web.search.provider": "tavily", "tools.web.providers.tavily.apiKey": None})

    start_permission_turn(None, conversation_id="c-1", turn_id="t-1")
    nowhere = await _run(tool, action="set", value=batch)
    assert "cannot be typed in here" in nowhere and not asked

    start_permission_turn(None, conversation_id="c-1", turn_id="t-2", credentials=Card(CredentialOutcome.SAVED))
    saved = await _run(tool, action="set", value=batch)
    conversation, request = asked[-1]
    assert conversation == "c-1" and request.target == "config:tools.web.providers.tavily.apiKey"
    assert "tools.web.providers.tavily.apiKey is set" in saved and "not shown" in saved
    assert ("settings.set", {"key": "tools.web.search.provider", "value": "tavily"}) in calls.calls

    start_permission_turn(None, conversation_id="c-1", turn_id="t-3", credentials=Card(CredentialOutcome.SKIPPED))
    skipped = await _run(tool, action="set", path="tools.web.providers.tavily.apiKey", value="null")
    assert "skipped" in skipped and "Settings" in skipped


@pytest.mark.asyncio
async def test_a_channel_secret_is_typed_on_the_card_before_the_channel_starts(config_file):
    """A channel's secret went to "enter it in Settings" while the vendor keys had a card."""
    from raven.contracts.asking import CredentialOutcome

    order: list[str] = []

    class Card:
        async def request_credential(self, *, conversation_id: str, turn_id: str, request) -> CredentialOutcome:
            order.append(request.target)
            return CredentialOutcome.SAVED

    class Configure(Calls):
        async def __call__(self, method: str, params: dict[str, Any]) -> Any:
            order.append(method)
            return await super().__call__(method, params)

    tool = RavenConfigTool()
    tool.set_rpc_caller(Configure({"channels.configure": {"outcome": "started"}, "channels.status": {"channels": []}}))
    start_permission_turn(None, conversation_id="c-1", turn_id="t-1", credentials=Card())
    reply = await _run(
        tool, action="set", path="channels.feishu", value='{"appId": "cli_1", "appSecret": null, "enabled": true}'
    )

    assert order[:2] == ["channel:feishu.app_secret", "channels.configure"]
    assert "channels.feishu.app_secret is set" in reply


def test_the_card_offers_a_field_only_where_the_page_can_save_it(config_file):
    from raven.config import self_surface as surface
    from raven.rpc.methods import console

    view = RavenConfigTool().approval_evidence(
        {
            "action": "set",
            "value": json.dumps({"tools.web.search.provider": "tavily", "tools.web.providers.tavily.apiKey": None}),
        }
    )
    provider, key = view["changes"]
    assert provider["value"] == "tavily" and "secret" not in provider
    assert key == {**key, "secret": True, "was": "not set", "enterable": True}
    assert "value" not in key
    assert surface.secret_input("providers.openrouter.apiKey") == {"via": "model.save_key", "slug": "openrouter"}
    for setting in surface.all_settings():
        field = surface.secret_input(setting.path)
        if field is not None and field["via"] == "settings.set":
            assert setting.path in console._SETTINGS_SIMPLE_KEYS, setting.path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"action": "set", "path": "tools.web.providers.tavily.apiKey", "value": '"tvly-pasted"'},
        {
            "action": "set",
            "value": json.dumps(
                {"tools.web.search.provider": "tavily", "tools.web.providers.tavily.apiKey": "tvly-pasted"}
            ),
        },
    ],
)
async def test_a_key_pasted_into_the_chat_is_refused_before_anyone_is_asked(params):
    from raven.contracts.permissions import Deny

    decision = await _gate(PermissionsConfig(mode="full")).check("raven_config", params)
    assert isinstance(decision, Deny) and "rotated" in decision.reason


@pytest.mark.asyncio
async def test_asking_only_for_a_key_goes_straight_to_the_credential_card():
    """Seen live: a key the user asked to enter raised a confirmation ("let a card
    ask you for this key?") before the card itself, which is where they decide."""
    ask = _gate(PermissionsConfig(mode="ask"))
    only_key = {"action": "set", "path": "providers.deepseek.apiKey", "value": "null"}
    with_more = {
        "action": "set",
        "value": json.dumps(
            {
                "agents.defaults.model": {"provider": "deepseek", "model": "deepseek-chat"},
                "providers.deepseek.apiKey": None,
            }
        ),
    }
    start_permission_turn(None, conversation_id="c-1", turn_id="t-1")
    assert isinstance(await ask.check("raven_config", only_key), NeedsApproval), "no card can show unattended"
    start_permission_turn(_Responder(), conversation_id="c-1", turn_id="t-2")
    assert isinstance(await ask.check("raven_config", only_key), Allow)
    decision = await ask.check("raven_config", with_more)
    assert isinstance(decision, NeedsApproval) and decision.session_keys == ()


@pytest.mark.asyncio
async def test_the_conversation_model_moves_only_this_conversation(config_file):
    seen: list[str] = []

    def session_model(key: str) -> tuple[str, bool]:
        seen.append(key)
        return "openrouter/deepseek/deepseek-v4.1-flash", False

    tool = RavenConfigTool(session_model=session_model)
    calls = Calls()
    tool.set_rpc_caller(calls)
    value = json.dumps({"provider": "openrouter", "model": "z-ai/glm-5.3"})
    assert "needs a conversation" in await _run(tool, action="set", path="session.model", value=value)

    start_permission_turn(None, conversation_id="tui:abc", turn_id="t-1")
    got = json.loads(await _run(tool, action="get", path="session.model"))
    assert got == {"session.model": "openrouter/deepseek/deepseek-v4.1-flash (the default)"}
    await _run(tool, action="set", path="session.model", value=value)
    assert calls.calls[-1] == (
        "config.set",
        {
            "key": "model",
            "value": "z-ai/glm-5.3",
            "provider": "openrouter",
            "scope": "session",
            "session_id": "tui:abc",
        },
    )
    view = tool.approval_evidence({"action": "set", "path": "session.model", "value": value})
    assert view["was"].endswith("(the default)") and view["value"] == "openrouter/z-ai/glm-5.3"
    assert set(seen) == {"tui:abc"}
    assert "session" not in json.loads(config_file.read_text())


def _with_memory_model(config_file: Path, pin: dict[str, str] | None) -> None:
    raw = json.loads(config_file.read_text())
    raw["agents"] = {"defaults": {"model": "deepseek-chat", "provider": "deepseek"}}
    raw.pop("plugins", None)
    if pin is not None:
        raw["plugins"] = {"config": {"everos-memory": {"llm": pin}}}
    config_file.write_text(json.dumps(raw))


def _roles(llm: dict[str, Any]) -> dict[str, Any]:
    return {"available": True, "sections": {"llm": llm}, "required": []}


@pytest.mark.asyncio
async def test_an_unset_memory_model_reads_as_following_the_main_model(config_file):
    """ "Follow the main model" is what a person asks for, and it is also what an
    unset memory model does. A read that said "not set" sent the model grepping
    the plugin's source for how to make it follow."""
    _with_memory_model(config_file, None)
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"settings.everos": _roles({"model": "", "provider": "", "follows_main": True})}))

    reply = json.loads(await _run(tool, action="get", path="memory.models.llm"))

    assert reply["memory.models.llm"] == {
        "follows": "the main model",
        "now": {"provider": "deepseek", "model": "deepseek-chat"},
    }
    described = json.loads(await _run(tool, action="describe", path="memory.models.llm"))
    assert described["when_unset"] == "it follows the main model"


@pytest.mark.asyncio
async def test_a_memory_model_the_main_model_cannot_stand_in_for_says_memory_is_off(config_file):
    _with_memory_model(config_file, None)
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"settings.everos": _roles({"model": "", "provider": "", "api_key_set": False})}))

    reply = json.loads(await _run(tool, action="get", path="memory"))

    assert "memory is off" in reply["memory.models.llm"]
    assert reply["memory.models.rerank"] == "not set (reranking is off)"


@pytest.mark.asyncio
async def test_a_memory_model_is_pinned_and_unpinned_through_the_memory_writer(config_file):
    _with_memory_model(config_file, {"model": "Qwen/Qwen3-32B", "provider": "deepinfra"})
    calls = Calls()
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)

    set_reply = await _run(
        tool, action="set", path="memory.models.llm", value='{"provider": "deepseek", "model": "deepseek-chat"}'
    )
    unset_reply = await _run(tool, action="unset", path="memory.models.llm")

    assert calls.calls == [
        ("settings.everos_set", {"section": "llm", "model": "deepseek-chat", "provider": "deepseek"}),
        ("settings.everos_set", {"section": "llm", "clear": True}),
    ]
    assert "Qwen/Qwen3-32B" in set_reply
    assert "follows the main model" in unset_reply


@pytest.mark.asyncio
async def test_unsetting_a_memory_model_that_is_already_unset_writes_nothing(config_file):
    _with_memory_model(config_file, None)
    calls = Calls()
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)

    reply = await _run(tool, action="unset", path="memory.models.llm")

    assert calls.calls == []
    assert "already unset" in reply and "follows the main model" in reply


def test_the_card_says_what_clearing_the_memory_model_means(config_file):
    _with_memory_model(config_file, {"model": "Qwen/Qwen3-32B", "provider": "deepinfra"})
    tool = RavenConfigTool()

    cleared = tool.approval_evidence({"action": "unset", "path": "memory.models.llm"})
    assert "follows the main model" in cleared["change"]
    assert (cleared["was"], cleared["unset_to"]) == ("deepinfra/Qwen/Qwen3-32B", "main_model")

    _with_memory_model(config_file, None)
    pinned = tool.approval_evidence(
        {"action": "set", "path": "memory.models.llm", "value": '{"provider": "deepinfra", "model": "m"}'}
    )
    assert pinned["was_unset"] is True and "was" not in pinned
    assert pinned["unset_to"] == "main_model"


@pytest.mark.asyncio
async def test_the_lent_caller_reaches_the_memory_roles():
    from raven.rpc.bootstrap import SELF_CONFIG_METHODS

    assert {"settings.everos", "settings.everos_set"} <= SELF_CONFIG_METHODS


@pytest.mark.asyncio
async def test_a_wrong_path_answers_with_the_closest_ones_and_says_where_to_stop(config_file):
    """Seen in the evals: tools.web.search.apiKey, channels.telegram.token,
    tools.media.image.apiKeyy -- invented paths, each one more call, and after a
    few of them the model went reading config files."""
    tool = RavenConfigTool()
    reply = await _run(tool, action="get", path="tools.media.image.apiKeyy")
    assert "tools.media.image.apiKey = not set" in reply
    assert "Do not look for it in Raven's source code" in reply
    searched = await _run(tool, action="describe", path="image generation")
    assert "tools.media.image.model = not set (there is no image generation tool" in searched
    assert "search matches the English words" in await _run(tool, action="describe", path="\u751f\u56fe")


@pytest.mark.asyncio
async def test_vendor_keys_read_as_one_line_of_which_are_set(config_file):
    """Asked why web search did not work, the agent spent seven gets checking one vendor key at a time."""
    raw = json.loads(config_file.read_text())
    raw["tools"]["web"] = {"providers": {"tavily": {"apiKey": "tv-secret"}}}
    config_file.write_text(json.dumps(raw))
    root = await _run(RavenConfigTool(), action="describe", path="tools")
    line = next(x for x in root.splitlines() if "tools.web.providers.<name>.apiKey" in x)
    assert "key set for: tavily;" in line and "serper" in line
    assert "tv-secret" not in root


def _roster() -> dict[str, Any]:
    return {
        "rows": [
            {
                "name": "Raven-Research",
                "kind": "acp",
                "enabled": True,
                "configured": True,
                "probe_status": "attention",
                "probe_detail": "the launcher exited before the handshake",
                "last_test_ok": False,
                "last_test_detail": "no answer to the test message",
                "needs_auth": True,
            },
            {"name": "OpenClaw", "preset": "openclaw", "kind": "acp", "enabled": False, "configured": False},
        ]
    }


@pytest.mark.asyncio
async def test_a_sub_agent_read_says_why_it_fails_and_whether_it_is_added(config_file):
    """Asked why a sub-agent failed, the agent read 24 to 44 log and launcher files:
    the roster knew the agent's health all along and the tool dropped it."""
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"subagents.list": _roster()}))

    research = json.loads(await _run(tool, action="describe", path="subagents.Raven-Research"))
    assert research["status"] == "attention" and "handshake" in research["status_detail"]
    assert research["last_test"] == {"ok": False, "detail": "no answer to the test message"}
    assert "credential" in research["needs_auth"]
    claw = json.loads(await _run(tool, action="describe", path="subagents.OpenClaw"))
    assert claw["added"] is False and '"preset": "openclaw"' in claw["next_step"]
    root = await _run(tool, action="describe")
    assert "subagents.OpenClaw: not added" in root
    assert "subagents.Raven-Research: on; acp; status attention" in root


@pytest.mark.asyncio
async def test_switching_on_a_preset_that_is_not_added_says_to_add_it(config_file):
    """Seen live: describe showed OpenClaw as enabled=false, so the model tried
    set enabled=true, which failed, before it thought of add."""
    calls = Calls({"subagents.list": _roster()})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    reply = await _run(tool, action="set", path="subagents.OpenClaw.enabled", value="true")
    assert 'add subagents {"preset": "openclaw"}' in reply
    assert not any(m == "subagents.toggle" for m, _ in calls.calls)


@pytest.mark.asyncio
async def test_a_provider_catalog_is_read_and_narrowed_through_the_page_method(config_file):
    """Seen live: asked for glm 5.3, the model curled OpenRouter's model list."""
    models = [
        {"id": "z-ai/glm-5.3", "label": "GLM 5.3", "kind": "chat", "added": False},
        {"id": "z-ai/glm-5.2", "label": "GLM 5.2", "kind": "chat", "added": True},
        {"id": "openai/gpt-6", "label": "GPT-6", "kind": "chat", "added": False},
    ]
    calls = Calls({"model.fetch_models": {"models": models, "status": "ok"}})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)

    reply = json.loads(await _run(tool, action="get", path="providers.openrouter.catalog", value="glm 5.3"))

    assert calls.calls == [("model.fetch_models", {"slug": "openrouter"})]
    assert [m["id"] for m in reply["models"]] == ["z-ai/glm-5.3"]
    assert '"provider": "openrouter"' in reply["use"]
    assert "providers.<name>.catalog" in await _run(tool, action="describe", path="providers")


@pytest.mark.asyncio
async def test_an_agent_that_does_not_answer_its_test_is_reported_with_what_to_do(config_file):
    """Seen live: add openclaw failed its test, and the model spent sixteen shell
    calls hand-writing ACP frames before finding its own provider key had lapsed."""
    refusal = RuntimeError(
        "sub-agent 'OpenClaw' did not answer a test message, so it was not added: acp agent 'OpenClaw' ended "
        "its turn with no content; stderr tail: [config] warnings: plugin disabled"
    )
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"subagents.add": refusal, "subagents.list": _roster()}))
    reply = await _run(tool, action="add", path="subagents", value='{"preset": "openclaw"}')
    assert reply.startswith("Not added:") and "Find out why yourself" in reply
    assert "`openclaw agent --agent main -m hi --json`" in reply and "scripting its protocol" in reply
    assert "come back redacted" in reply and "a sign-in" in reply
    assert (
        "name the agent's own command" in reply
        and "Never read, copy or test a key yourself" in reply
        and "three have not told you why" in reply
    )


class _Refused(RuntimeError):
    def __init__(self, text: str, remedy: dict[str, Any]) -> None:
        super().__init__(text)
        self.data = {"remedy": remedy}


@pytest.mark.parametrize(
    ("remedy", "raven_does", "user_does"),
    [
        ({"kind": "upgrade", "command": "npm i -g @qwen-code/qwen-code@latest"}, "Upgrade it with `npm i", None),
        ({"kind": "download", "command": "npx -y pi-acp@0.0.33"}, "Run `npx -y pi-acp@0.0.33` once", None),
        ({"kind": "runtime", "command": "brew upgrade node", "needs": "22", "found": "18.20"}, "needs 22", None),
        ({"kind": "model"}, "Pick another one it lists", None),
        ({"kind": "sign_in", "command": "codex login"}, None, "the user runs `codex login`"),
        ({"kind": "setup", "command": "qwen", "then": "/auth"}, None, "runs `qwen`, then types /auth"),
        ({"kind": "api_key"}, None, "enters it in this agent's settings"),
    ],
)
@pytest.mark.asyncio
async def test_a_refusal_says_whether_raven_or_the_user_fixes_it(config_file, remedy, raven_does, user_does):
    """The probe classifies every refusal and names its fix; the tool used to drop
    that and hand the model one paragraph telling it to send the user off."""
    refusal = _Refused("sub-agent 'X' did not answer a test message, so it was not added: ...", remedy)
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"subagents.add": refusal, "subagents.list": _roster()}))
    reply = await _run(tool, action="add", path="subagents", value='{"preset": "codex"}')
    if raven_does:
        assert raven_does in reply and "This one needs the user" not in reply
    if user_does:
        assert "This one needs the user" in reply and user_does in reply


@pytest.mark.asyncio
async def test_a_missing_agent_is_installed_rather_than_reported(config_file):
    refusal = RuntimeError(
        "sub-agent 'Kimi Code' did not answer a test message, so it was not added: kimi is not on the login "
        "shell PATH; install with uv tool install kimi-cli"
    )
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"subagents.add": refusal, "subagents.list": _roster()}))
    reply = await _run(tool, action="add", path="subagents", value='{"preset": "kimi_code"}')
    assert "It is not installed. Install it with exec" in reply


@pytest.mark.asyncio
async def test_only_a_preset_is_added(config_file):
    """A launch command comes from the preset table alone; an agent it does not list cannot be added here."""
    calls = Calls()
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    reply = await _run(tool, action="add", path="subagents", value='{"name": "Gemini", "command": "gemini --acp"}')
    assert "describe list" in reply
    assert not any(m == "subagents.add" for m, _ in calls.calls)
    assert "cannot be connected from here" in await _run(tool, action="describe")


@pytest.mark.asyncio
async def test_an_agent_whose_model_is_refused_can_be_added_on_another_it_lists(config_file):
    """Seen live: Qwen Code's pinned free model was withdrawn, and the reply sent
    the user to /auth although the agent listed two other models."""
    refusal = RuntimeError(
        "sub-agent 'Qwen Code' did not answer a test message, so it was not added: 404 This model is "
        "unavailable for free"
    )
    roster = {
        "rows": [
            {
                "name": "Qwen Code",
                "preset": "qwen_code",
                "kind": "acp",
                "configured": False,
                "model_choices": [{"value": "z-ai/glm-4.5-air:free"}, {"value": "GPT-5.5"}],
            }
        ]
    }
    calls = Calls({"subagents.add": refusal, "subagents.list": roster})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)

    reply = await _run(tool, action="add", path="subagents", value='{"preset": "qwen_code"}')
    assert "`qwen hi`" in reply and "z-ai/glm-4.5-air:free, GPT-5.5" in reply
    assert '{"preset": "qwen_code", "model": "<one>"}' in reply and "costs money" in reply

    calls.replies["subagents.add"] = {"added": True, "name": "Qwen Code"}
    await _run(tool, action="add", path="subagents", value='{"preset": "qwen_code", "model": "GPT-5.5"}')
    assert [c for c in calls.calls if c[0] == "subagents.add"][-1] == (
        "subagents.add",
        {"preset": "qwen_code", "model": "GPT-5.5"},
    )


@pytest.mark.asyncio
async def test_switching_on_a_scan_channel_says_where_the_code_is(config_file):
    """Seen live: after enabling WeChat the model grepped code and read logs --
    another Raven's, too -- to find the QR, which the Channels settings showed."""
    status = {
        "gateway_running": True,
        "channels": [{"name": "weixin", "enabled": True, "running": True, "qr_login": True, "fields": []}],
    }
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"channels.status": status, "channels.configure": {"outcome": "started"}}))

    reply = await _run(tool, action="set", path="channels.weixin.enabled", value="true")
    described = json.loads(await _run(tool, action="describe", path="channels.weixin"))

    assert "Settings > Channels > weixin" in reply and "scan" in reply
    assert described["state"] == "running, not connected yet" and "QR" in described["login"]


@pytest.mark.asyncio
async def test_a_channel_read_carries_its_values_and_how_it_logs_in(config_file):
    """A silent Telegram was read with describe and then get for the same channel, and
    a WeChat setup asked the user for a token that the QR scan fills in."""
    raw = json.loads(config_file.read_text())
    raw["channels"] = {"telegram": {"enabled": True, "token": "tg-secret", "allowFrom": ["someone_else"]}}
    config_file.write_text(json.dumps(raw))
    tool = RavenConfigTool()

    telegram = json.loads(await _run(tool, action="describe", path="channels.telegram"))
    values = {f["path"]: f["value"] for f in telegram["fields"]}
    assert values["channels.telegram.allow_from"] == ["someone_else"] and values["channels.telegram.token"] == "set"
    assert "tg-secret" not in json.dumps(telegram)
    assert "developer console" in telegram["login"] and "token" in telegram["login"]
    weixin = json.loads(await _run(tool, action="describe", path="channels.weixin"))
    assert "QR code" in weixin["login"] and "leave it" in weixin["login"]


@pytest.mark.asyncio
async def test_several_fields_of_a_channel_go_in_one_write(config_file):
    """Setting up WeChat sent {"enabled": true, "token": null} to channels.weixin and
    a batch of channel paths; both were refused, one call each."""
    calls = Calls({"channels.configure": {"outcome": "started"}})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)

    one = await _run(
        tool, action="set", path="channels.feishu", value='{"appId": "cli_1", "enabled": true, "appSecret": null}'
    )
    batch = await _run(tool, action="set", value='{"channels.feishu.appId": "cli_2", "tools.exec.timeout": 90}')

    configures = [p for m, p in calls.calls if m == "channels.configure"]
    assert configures[0] == {"name": "feishu", "fields": {"app_id": "cli_1"}, "enabled": True}
    assert configures[1]["fields"] == {"app_id": "cli_2"}
    assert "app_secret is a secret" in one and "Settings > Channels > feishu" in one
    assert "tools.exec.timeout" in batch and "channels.feishu.app_id" in batch


@pytest.mark.asyncio
async def test_a_sub_agent_can_be_tested_and_the_verdict_comes_back(config_file):
    """Told "run a test" by an agent's status, the model searched for a way to run
    one; the page's Test button was the only door."""
    tested = {
        "rows": [
            {
                **_roster()["rows"][0],
                "last_test_ok": True,
                "last_test_detail": "answered",
                "probe_status": "ready",
                "needs_auth": False,
            }
        ]
    }
    replies = iter([_roster(), tested])

    class Roster(Calls):
        async def __call__(self, method: str, params: dict[str, Any]) -> Any:
            self.calls.append((method, params))
            if method == "subagents.list":
                return next(replies)
            return {"ok": True, "detail": "answered in 2.1s"}

    calls = Roster()
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)

    reply = json.loads(await _run(tool, action="test", path="subagents.Raven-Research"))

    assert ("subagents.test", {"name": "Raven-Research", "source": "config"}) in calls.calls
    assert reply == {"subagent": "Raven-Research", "ok": True, "detail": "answered in 2.1s", "status": "ready"}
    card = tool.approval_evidence({"action": "test", "path": "subagents.Raven-Research"})
    assert card["action"] == "test" and "quota" in card["change"]


@pytest.mark.asyncio
async def test_testing_a_preset_that_is_not_added_points_at_add(config_file):
    calls = Calls({"subagents.list": _roster()})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    reply = await _run(tool, action="test", path="subagents.OpenClaw")
    assert 'add subagents {"preset": "openclaw"}' in reply
    assert not any(m == "subagents.test" for m, _ in calls.calls)


@pytest.mark.asyncio
async def test_the_disabled_tools_list_names_the_tools_there_are(config_file):
    """Asked to turn the browser tools off, the agent guessed eight browser_* names
    and then went looking for a tool list to check them against."""
    tool = RavenConfigTool(tool_names=lambda: ["browser_navigate", "exec", "read_file"])
    described = json.loads(await _run(tool, action="describe", path="tools.disabledTools"))
    assert described["tool_names"] == ["browser_navigate", "exec", "read_file"]
    tool.set_rpc_caller(Calls({"settings.set": {"applied": True, "previous": []}}))
    reply = await _run(tool, action="set", path="tools.disabledTools", value='["browser_navigate", "browser_fly"]')
    assert "['browser_fly']" in reply and "browser_navigate'" not in reply.split("Not a tool")[1]


@pytest.mark.asyncio
async def test_a_channel_named_the_way_people_say_it_answers_with_what_it_can_mean(config_file):
    """channels.wechat was an error, then one describe per candidate."""
    reply = json.loads(await _run(RavenConfigTool(), action="describe", path="channels.wechat"))
    by_channel = {v["channel"]: v for v in reply["means_one_of"]}
    assert "QR code" in by_channel["channels.weixin"]["login"]
    assert by_channel["channels.wecom"]["required"] == ["channels.wecom.bot_id", "channels.wecom.secret"]
    assert "`weixin`" in await _run(RavenConfigTool(), action="set", path="channels.wechat.enabled", value="true")


@pytest.mark.asyncio
async def test_naming_a_provider_reads_it_and_points_at_its_catalog(config_file):
    reply = await _run(RavenConfigTool(), action="describe", path="openrouter")
    assert "providers.openrouter.apiKey = set" in reply and "providers.openrouter.catalog" in reply


@pytest.mark.asyncio
async def test_a_batch_spelled_as_a_python_dict_is_still_a_batch(config_file):
    """Seen live: {'tools.web.search.provider': 'tavily', '...apiKey': None}
    was refused with "set needs a path", and the model fell back to two cards."""
    calls = Calls({"settings.set": {"applied": True, "previous": None}})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    reply = await _run(tool, action="set", value="{'tools.exec.timeout': 90, 'tools.disabledTools': ['exec']}")
    assert [p["key"] for m, p in calls.calls if m == "settings.set"] == ["tools.exec.timeout", "tools.disabledTools"]
    assert "90" in reply
    card = tool.approval_evidence({"action": "set", "value": "{'tools.exec.timeout': 90, 'tools.disabledTools': []}"})
    assert [row["setting"] for row in card["changes"]] == ["tools.exec.timeout", "tools.disabledTools"]


@pytest.mark.asyncio
async def test_naming_a_channel_or_an_agent_reads_it(config_file):
    """describe telegram and describe openclaw answered "no setting matches"."""
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"subagents.list": _roster()}))
    telegram = await _run(tool, action="describe", path="telegram")
    assert "is the channel channels.telegram" in telegram and '"fields"' in telegram
    claw = await _run(tool, action="describe", path="openclaw")
    assert "is the sub-agent subagents.OpenClaw" in claw and '"added": false' in claw


@pytest.mark.asyncio
async def test_a_huge_catalog_read_without_a_filter_asks_for_one(config_file):
    models = [{"id": f"vendor/model-{i}", "kind": "chat", "added": i == 7} for i in range(300)]
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"model.fetch_models": {"models": models, "status": "ok"}}))
    reply = json.loads(await _run(tool, action="get", path="providers.openrouter.catalog"))
    assert reply["count"] == 300 and reply["added"] == ["vendor/model-7"] and "value" in reply["narrow"]


@pytest.mark.asyncio
async def test_what_an_unset_media_model_still_needs_follows_the_keys_on_file(config_file, monkeypatch):
    """ "Setting a model is enough" was written as a fixed sentence, true only
    where a usable key happened to be set; without one the model told the user
    to pick a model and nothing more."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    line = lambda text: next(x for x in text.splitlines() if "tools.media.image.model =" in x)  # noqa: E731
    with_key = line(await _run(RavenConfigTool(), action="describe", path="tools"))
    assert "a model is all it lacks" in with_key

    raw = json.loads(config_file.read_text())
    raw["providers"] = {}
    config_file.write_text(json.dumps(raw))
    without = line(await _run(RavenConfigTool(), action="describe", path="tools"))
    assert "it also needs a key: tools.media.image.apiKey" in without and "all it lacks" not in without

    # The tool also takes OPENROUTER_API_KEY while it calls OpenRouter.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
    from_env = line(await _run(RavenConfigTool(), action="describe", path="tools"))
    assert "a model is all it lacks" in from_env


@pytest.mark.asyncio
async def test_what_an_unset_media_model_needs_asks_the_provider_it_would_borrow_from(config_file, monkeypatch):
    """The OpenRouter key on file is borrowed by no section pointed elsewhere, and a
    section on a named provider is paid by that provider's key alone."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    line = lambda text: next(x for x in text.splitlines() if "tools.media.image.model =" in x)  # noqa: E731
    raw = json.loads(config_file.read_text())
    raw["providers"] = {"openrouter": {"apiKey": "sk-or"}}
    raw.setdefault("tools", {})["media"] = {"image": {"apiBase": "https://relay.test/v1"}}
    config_file.write_text(json.dumps(raw))
    elsewhere = line(await _run(RavenConfigTool(), action="describe", path="tools"))
    assert "it also needs a key: tools.media.image.apiKey" in elsewhere and "all it lacks" not in elsewhere

    raw["tools"]["media"] = {"image": {"provider": "openai"}}
    config_file.write_text(json.dumps(raw))
    unpaid = line(await _run(RavenConfigTool(), action="describe", path="tools"))
    assert "it also needs a key: one under providers.openai" in unpaid and "all it lacks" not in unpaid

    raw["providers"]["openai"] = {"apiKey": "sk-openai"}
    config_file.write_text(json.dumps(raw))
    paid = line(await _run(RavenConfigTool(), action="describe", path="tools"))
    assert "a model is all it lacks" in paid

    # A key the provider keeps on an endpoint is the key the tool would use.
    raw["providers"]["openai"] = {
        "endpoints": [{"label": "main", "apiBase": "https://relay.test/v1", "apiKey": "sk-ep"}]
    }
    config_file.write_text(json.dumps(raw))
    on_endpoint = line(await _run(RavenConfigTool(), action="describe", path="tools"))
    assert "a model is all it lacks" in on_endpoint


@pytest.mark.asyncio
async def test_a_download_that_timed_out_names_how_to_fetch_it_even_without_a_command(config_file):
    """A refusal whose remedy carries no launch to quote read
    "run `its own sign-in or setup command` once" for an npx fetch."""
    refusal = _Refused(
        "sub-agent 'Kimi Code' did not answer a test message, so it was not added: ...", {"kind": "download"}
    )
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"subagents.add": refusal, "subagents.list": _roster()}))
    reply = await _run(tool, action="add", path="subagents", value='{"preset": "kimi_code"}')
    assert "npx -y <package> --version" in reply and "sign-in" not in reply.split("\n")[1]


@pytest.mark.asyncio
async def test_a_sub_agent_can_be_named_by_its_preset(config_file):
    """Connecting Qwen Code, the agent described subagents.qwen_code, the preset it had just been told to add."""
    roster = {"rows": [{"name": "Qwen Code", "preset": "qwen_code", "kind": "acp", "configured": False}]}
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"subagents.list": roster}))
    qwen = json.loads(await _run(tool, action="describe", path="subagents.qwen_code"))
    assert qwen["name"] == "Qwen Code"


@pytest.mark.asyncio
async def test_add_refuses_keys_it_would_otherwise_drop(config_file):
    """Connecting an agent that was not installed, the model passed acp_args, and preset
    with command; both were dropped without a word and it retried the same launch."""
    calls = Calls()
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    stray = await _run(tool, action="add", path="subagents", value='{"preset": "kimi_code", "acp_args": "acp"}')
    both = await _run(tool, action="add", path="subagents", value='{"preset": "kimi_code", "command": "/x/kimi acp"}')
    assert "acp_args" in stray and "command" in both and "launch command is fixed" in both
    assert not any(m == "subagents.add" for m, _ in calls.calls)


@pytest.mark.asyncio
async def test_several_agents_connect_under_one_confirmation(config_file):
    """Seen live: "connect hermes, openclaw, qwen and kimi" raised one card per agent."""

    class Adds(Calls):
        async def __call__(self, method: str, params: dict[str, Any]) -> Any:
            self.calls.append((method, params))
            if method == "subagents.add" and params.get("preset") == "openclaw":
                raise _Refused(
                    "sub-agent 'OpenClaw' did not answer a test message, so it was not added: 401", {"kind": "api_key"}
                )
            if method == "subagents.add":
                return {"added": True, "name": params.get("preset") or params.get("name")}
            return _roster()

    calls = Adds()
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    value = '[{"preset": "qwen_code"}, {"preset": "openclaw"}, {"preset": "kimi_code"}]'

    card = tool.approval_evidence({"action": "add", "path": "subagents", "value": value})
    reply = await _run(tool, action="add", path="subagents", value=value)

    assert "Connect sub-agents: qwen_code; openclaw; kimi_code" in card["change"]
    assert [p.get("preset") or p.get("name") for m, p in calls.calls if m == "subagents.add"] == [
        "qwen_code",
        "openclaw",
        "kimi_code",
    ]
    assert "Connected sub-agent qwen_code" in reply and "Connected sub-agent kimi_code" in reply
    assert "Not added:" in reply and "This one needs the user" in reply
    assert reply.count("Never read, copy or test a key yourself") == 1


@pytest.mark.asyncio
async def test_writes_to_a_sub_agent_say_what_it_is_now(config_file):
    """After setting an agent's model the row was read back twice, and an agent just
    connected was described again: the replies said "Set" and "Connected" and
    nothing about the agent itself."""
    row = {
        "name": "CodeBuddy",
        "preset": "codebuddy",
        "kind": "acp",
        "enabled": True,
        "configured": True,
        "probe_status": "ready",
        "probe_detail": "connected to codebuddy 1.4.2 over ACP v1",
        "model_choices": [{"value": "m-base"}, {"value": "m-lite"}],
    }
    calls = Calls({"subagents.list": {"rows": [row]}, "subagents.add": {"added": True, "name": "CodeBuddy"}})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)

    listed = await _run(tool, action="set", path="subagents.CodeBuddy.model", value='"m-lite"')
    unlisted = await _run(tool, action="set", path="subagents.CodeBuddy.model", value='"m-max"')
    added = await _run(tool, action="add", value='{"preset": "codebuddy"}')

    assert "one of the models the agent lists (m-base, m-lite)" in listed
    assert "does not list it" in unlisted
    assert "connected to codebuddy 1.4.2" in added and "Models it lists: m-base, m-lite" in added


def test_every_method_the_tool_calls_is_one_the_gateway_lends_it():
    """`test` was offered and every call of it answered "raven_config may not call
    subagents.test": the tool and the gateway's allowlist drifted apart."""
    import re
    from pathlib import Path

    from raven.agent.tools import raven_config as module
    from raven.rpc.bootstrap import SELF_CONFIG_METHODS

    called = set(re.findall(r'self\._rpc\(\s*"([a-z_.]+)"', Path(module.__file__).read_text()))
    assert called and called <= SELF_CONFIG_METHODS, sorted(called - SELF_CONFIG_METHODS)


@pytest.mark.asyncio
async def test_an_unmeasured_agent_is_described_as_settable_rather_than_needing_a_test(config_file):
    """Changing a connected agent's model, the model read "run a test", tested,
    re-read twice, then set it -- the set would have measured the menu itself."""
    row = {"name": "CodeBuddy", "preset": "codebuddy", "kind": "acp", "configured": True, "model_source": "agent"}
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"subagents.list": {"rows": [row]}}))
    described = json.loads(await _run(tool, action="describe", path="subagents.CodeBuddy"))
    assert described["model_choices"] == [] and "set subagents.CodeBuddy.model" in described["model_note"]


def _raven_holds(config_file: Path, lent: list[str] | None = None, **keys: str) -> None:
    raw = json.loads(config_file.read_text())
    raw["providers"] = {name: {"apiKey": key} for name, key in keys.items()}
    if lent is not None:
        raw["subagents"] = {"agents": [{"name": "Pi", "preset": "pi", "kind": "acp", "command": "x", "lendKeys": lent}]}
    config_file.write_text(json.dumps(raw))


_PI_ROW = {"name": "Pi", "preset": "pi", "kind": "acp", "enabled": True, "configured": True, "needs_auth": True}


@pytest.mark.asyncio
async def test_a_key_raven_holds_is_offered_before_a_sign_in(config_file):
    """Seen live: Pi was told to log in on its own -- "Pi's credentials are its
    own, it cannot borrow Raven's key" -- while Raven held the OpenRouter key Pi reads."""
    _raven_holds(config_file, openrouter="sk-or-raven")
    refusal = _Refused("sub-agent 'Pi' did not answer a test message, so it was not added: 401", {"kind": "api_key"})
    tool = RavenConfigTool()
    tool.set_rpc_caller(
        Calls({"subagents.add": refusal, "subagents.list": {"rows": [_PI_ROW | {"configured": False}]}})
    )

    reply = await _run(tool, action="add", path="subagents", value='{"preset": "pi"}')

    assert '"lend_key": "openrouter"' in reply and "This one needs the user" not in reply
    assert "sk-or-raven" not in reply


@pytest.mark.asyncio
async def test_describe_names_what_can_be_lent_and_what_is(config_file):
    _raven_holds(config_file, lent=["openrouter"], openrouter="sk-or", deepseek="sk-ds", poe="sk-poe")
    tool = RavenConfigTool()
    tool.set_rpc_caller(Calls({"subagents.list": {"rows": [_PI_ROW]}}))
    pi = json.loads(await _run(tool, action="describe", path="subagents.Pi"))
    assert pi["lends_keys"] == ["openrouter"]
    assert pi["can_lend"] == ["deepseek"], "only providers Pi reads, and not one it already has"
    assert "can be lent" in pi["needs_auth"]
    assert "sk-" not in json.dumps(pi)


@pytest.mark.asyncio
async def test_lending_is_set_through_the_agents_own_writer_and_confirmed_as_sensitive(config_file):
    calls = Calls({"subagents.list": {"rows": [_PI_ROW]}})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    await _run(tool, action="set", path="subagents.Pi.lendKeys", value='["openrouter"]')
    assert ("subagents.update", {"name": "Pi", "lend_keys": ["openrouter"]}) in calls.calls

    params = {"action": "set", "path": "subagents.Pi.lendKeys", "value": '["openrouter"]'}
    assert surface.touches_sensitive(params), "smart mode's reviewer never hands out Raven's key"
    card = tool.approval_evidence(
        {"action": "add", "path": "subagents", "value": '{"preset": "pi", "lend_key": "openrouter"}'}
    )
    assert "started with Raven's openrouter key" in card["change"]
    assert not surface.carries_secret_value(
        {"action": "add", "path": "subagents", "value": '{"preset": "pi", "lend_key": "openrouter"}'}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "null", ' "restart"', "0", "false"])
async def test_the_restart_card_names_the_restart_that_runs(config_file, value):
    """For a value the tool falls back on, the card said reload while the tool
    restarted the whole process."""
    asked: list[str] = []

    async def restart(target: str) -> str:
        asked.append(target)
        return f"scheduled {target}"

    tool = RavenConfigTool()
    tool.set_restarter(restart)
    await _run(tool, action="set", path="sentinel.enabled", value="true")
    card = tool.approval_evidence({"action": "restart", "value": value})
    await _run(tool, action="restart", value=value)
    assert card["target"] == "restart" and asked == ["restart"]
    assert card["change"].startswith("Restart the whole Raven process")


@pytest.mark.asyncio
async def test_one_field_named_twice_in_a_call_is_refused(config_file):
    """The card showed both values and the write kept whichever came last."""
    calls = Calls({"channels.configure": {"applied": True, "outcome": "restarted"}})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    batch = {"channels.telegram.allowFrom": ["me"], "channels.telegram.allow_from": ["*"]}
    reply = await _run(tool, action="set", value=json.dumps(batch))
    assert "named twice" in reply and calls.calls == []
    reply = await _run(tool, action="set", path="channels.telegram", value='{"allowFrom": ["me"], "allow_from": ["*"]}')
    assert "named twice" in reply and calls.calls == []
    reply = await _run(tool, action="set", value='{"tools.exec.timeout": 30, "tools.exec.timeout ": 90}')
    assert "named twice" in reply
    assert json.loads(config_file.read_text())["tools"]["exec"]["timeout"] == 60


@pytest.mark.asyncio
async def test_reading_a_channel_secret_reports_only_whether_it_is_set(config_file):
    raw = json.loads(config_file.read_text())
    raw["channels"] = {"telegram": {"token": "PLAINTEXT-tgtoken-123"}, "feishu": {"encryptKey": "PLAINTEXT-enc-1"}}
    config_file.write_text(json.dumps(raw))
    tool = RavenConfigTool()
    for path in ("channels.telegram.token", "channels.feishu.encryptKey"):
        reply = await _run(tool, action="get", path=path)
        assert "PLAINTEXT" not in reply and '"set"' in reply, reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"action": "set", "path": "tools.mcpServers.z.url", "value": "https://mcp.example/s/PLAINpathSecret/mcp"},
        {"action": "set", "path": "tools.mcpServers.z.args", "value": '["--api-key", "PLAINargSecret"]'},
        {"action": "add", "path": "tools.mcpServers", "value": '{"z": {"url": "https://h/s/PLAINaddSecret"}}'},
        {"action": "unset", "path": "tools.mcpServers.z.url"},
        {"action": "set", "value": '{"tools.exec.timeout": 30, "tools.mcpServers.z.url": "https://h/PLAINbatch"}'},
        # A misspelt field is a path the tool will not write either, and its
        # value is often the very key that was meant for the real one.
        {"action": "set", "path": "channels.telegram.tokne", "value": "PLAINTEXT-ordinary-typo"},
        {"action": "set", "path": "channels.telegram", "value": '{"replyToMessage": true, "tokne": "PLAINobj"}'},
        {"action": "set", "value": '{"channels.telegram.tokne": "PLAINbatchtypo"}'},
        {"action": "set", "path": "channels.nosuch.token", "value": "PLAINchannel"},
        {"action": "set", "path": "subagents.pi.tokne", "value": "PLAINsub"},
    ],
)
async def test_a_path_the_tool_will_not_write_is_refused_before_anyone_reads_it(monkeypatch, params):
    """A key in an MCP server's URL or arguments has no name to recognise it by.
    The tool refuses those paths anyway, so the gate refuses them first and the
    value reaches neither the card nor the reviewer."""
    shown = _reviewing(monkeypatch, allow=True)
    responder = _Responder()
    start_permission_turn(responder, conversation_id="c-1", turn_id="t-1")
    decision = await _smart_gate().check("raven_config", params)
    assert isinstance(decision, Deny) and "not something raven_config changes" in decision.reason
    assert shown == [] and responder.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"action": "set", "path": "tools.exec.timeout", "value": "30"},
        {"action": "set", "path": "channels.telegram", "value": '{"replyToMessage": true}'},
        {"action": "set", "path": "channels.telegram.replyToMessage", "value": "true"},
        {"action": "set", "path": "channels.slack", "value": '{"dm.policy": "allowlist"}'},
        {"action": "set", "path": "channels.matrix.e2ee_enabled", "value": "true"},
        {"action": "set", "path": "subagents.pi.model", "value": '"x"'},
        {"action": "set", "value": '{"tools.exec.timeout": 30}'},
        {"action": "add", "value": '{"preset": "pi"}'},
        {"action": "unset", "path": "tools.exec.timeout"},
        {"action": "restart"},
    ],
)
async def test_everything_the_tool_does_write_still_reaches_the_ordinary_decision(params):
    decision = await _gate(PermissionsConfig(mode="ask")).check("raven_config", params)
    assert not isinstance(decision, Deny), params


@pytest.mark.asyncio
async def test_the_restart_prompt_title_names_the_restart_that_runs(config_file):
    """The ACP client draws only the description, which still said reload."""
    tool = RavenConfigTool()
    tool.set_restarter(lambda target: asyncio.sleep(0, result=target))
    await _run(tool, action="set", path="sentinel.enabled", value="true")
    responder = _Responder()
    start_permission_turn(responder, conversation_id="c-1", turn_id="t-1")
    await _gate(PermissionsConfig(mode="ask")).enforce("raven_config", {"action": "restart", "value": "null"}, tool)
    assert responder.calls and responder.calls[0]["description"].startswith("Restart the whole Raven process")


def test_the_card_carries_the_note_of_a_field_inside_an_object_and_names_a_bare_add():
    line = surface.change_line({"action": "set", "path": "channels.telegram", "value": '{"allow_from": ["*"]}'})
    assert "Note: widening it lets more people instruct Raven" in line
    view = surface.change_view({"action": "set", "path": "channels.slack", "value": '{"dm.policy": "open"}'}, {})
    assert view["sensitive"] == "widening it lets more people instruct Raven"
    added = surface.change_line({"action": "add", "value": '{"preset": "claude-code", "lend_key": "anthropic"}'})
    assert added.startswith("Connect sub-agent") and "started with Raven's anthropic key" in added


@pytest.mark.asyncio
async def test_changing_the_default_mode_says_this_conversation_keeps_its_own(config_file):
    """The reply said the change takes effect next turn while the gate kept reading
    this conversation's own `full` -- wrong in the unsafe direction."""
    from raven.permissions.session import set_session_mode

    calls = Calls({"settings.set": {"applied": True, "previous": "full"}})
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    start_permission_turn(_Responder(), conversation_id="c-own", turn_id="t-1")
    set_session_mode("c-own", "full")
    try:
        reply = await _run(tool, action="set", path="permissions.mode", value='"ask"')
    finally:
        set_session_mode("c-own", None)
    assert "has its own approval mode (full)" in reply
    plain = await _run(tool, action="set", path="permissions.mode", value='"ask"')
    assert "its own approval mode" not in plain


@pytest.mark.asyncio
async def test_a_batch_is_checked_whole_before_any_of_it_is_written(config_file):
    """A sub-agent field was checked only when its turn came, after the settings
    before it were already written, and the reply named only the error."""
    calls = Calls()
    tool = RavenConfigTool()
    tool.set_rpc_caller(calls)
    batch = {"agents.defaults.temperature": 0.5, "subagents.codex.enabled": "yes"}
    reply = await _run(tool, action="set", value=json.dumps(batch))
    assert reply.startswith("Error") and "takes true or false" in reply
    assert "agents" not in json.loads(config_file.read_text()) and calls.calls == []

    offline = RavenConfigTool()
    reply = await _run(
        offline,
        action="set",
        value=json.dumps({"agents.defaults.temperature": 0.5, "channels.telegram.replyToMessage": True}),
    )
    assert "nothing was changed" in reply and "agents" not in json.loads(config_file.read_text())


@pytest.mark.asyncio
async def test_a_write_refused_partway_names_what_already_took(config_file):
    class _RefusesAgents(Calls):
        async def __call__(self, method: str, params: dict[str, Any]) -> Any:
            if method == "subagents.toggle":
                raise ValueError("the agent is busy")
            if method == "subagents.list":
                return {"rows": [{"name": "codex", "configured": True, "enabled": True, "kind": "acp"}]}
            return await super().__call__(method, params)

    tool = RavenConfigTool()
    tool.set_rpc_caller(_RefusesAgents())
    batch = {"agents.defaults.temperature": 0.5, "subagents.codex.enabled": False}
    reply = await _run(tool, action="set", value=json.dumps(batch))
    assert reply.startswith("Error") and "applied before it" in reply and "agents.defaults.temperature" in reply
