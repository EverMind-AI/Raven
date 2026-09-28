"""``raven_config``: the agent reading and changing its own configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from raven.agent.tools.raven_config import RavenConfigTool
from raven.config.schema import PermissionsConfig
from raven.config.self_surface import Effect
from raven.contracts.permissions import Allow, ApprovalChoice, ApprovalOutcome, NeedsApproval
from raven.permissions.builtin import BuiltinRulings
from raven.permissions.gate import PermissionGate
from raven.permissions.turn import start_permission_turn


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
async def test_describe_lists_sections_then_one_section(config_file):
    tool = RavenConfigTool()
    root = json.loads(await _run(tool, action="describe"))
    names = [s["name"] for s in root["sections"]]
    assert {"model", "tools", "channels", "subagents", "security"} <= set(names)
    assert "note" in root  # no writer lent yet
    tools = json.loads(await _run(tool, action="describe", path="tools"))
    timeout = next(s for s in tools["settings"] if s["path"] == "tools.exec.timeout")
    assert timeout["type"] == "int" and "next turn" in timeout["takes_effect"]


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
    assert "not in the catalog" in await _run(tool, action="get", path="providers.nobody")


@pytest.mark.asyncio
async def test_a_raw_setting_is_written_and_a_reload_left_pending(config_file):
    tool = RavenConfigTool()
    reply = await _run(tool, action="set", path="agents.defaults.temperature", value="0.7")
    assert "reload" in reply and "Pending" in reply
    assert json.loads(config_file.read_text())["agents"]["defaults"]["temperature"] == 0.7
    root = json.loads(await _run(tool, action="describe"))
    assert root["pending"] == {"reload": ["agents.defaults.temperature"]}


@pytest.mark.asyncio
async def test_restart_without_a_restarter_says_so_and_keeps_pending(config_file):
    tool = RavenConfigTool()
    await _run(tool, action="set", path="agents.defaults.temperature", value="0.7")
    reply = await _run(tool, action="restart")
    assert "cannot restart itself" in reply
    assert json.loads(await _run(tool, action="describe"))["pending"]


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
    assert "pending" not in json.loads(await _run(tool, action="describe"))


@pytest.mark.asyncio
async def test_a_reload_keeps_what_only_a_restart_applies(config_file):
    async def restart(target: str) -> str:
        return target

    tool = RavenConfigTool()
    tool.set_restarter(restart)
    await _run(tool, action="set", path="agents.defaults.temperature", value="0.7")
    await _run(tool, action="set", path="sentinel.enabled", value="true")
    await _run(tool, action="restart", value="reload")
    assert json.loads(await _run(tool, action="describe"))["pending"] == {"restart": ["sentinel.enabled"]}


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
            {"name": "codex", "kind": "acp", "enabled": True, "model_source": "agent", "model_choices": ["gpt-6"]},
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
@pytest.mark.parametrize(
    "config",
    [
        PermissionsConfig(mode="full"),
        PermissionsConfig(mode="ask", tools={"raven_config": "allow"}),
    ],
)
async def test_every_change_asks_whatever_the_mode_or_rules_say(config):
    params = {"action": "set", "path": "permissions.mode", "value": '"full"'}
    decision = await _gate(config).check("raven_config", params)
    assert isinstance(decision, NeedsApproval)
    assert decision.session_keys == ()
    assert "permissions.mode" in decision.description


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
    below = json.loads(await _run(tool, action="describe", path="tools.exec"))
    assert below["prefix"] == "tools.exec"
    assert "tools.exec.timeout" in [s["path"] for s in below["settings"]]
    assert "not in the catalog" in await _run(tool, action="describe", path="tools.nothing")


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
    """The card saves the key itself; the tool only learns whether it landed."""
    tool = RavenConfigTool()
    calls = Calls()
    tool.set_rpc_caller(calls)
    batch = json.dumps({"tools.web.search.provider": "tavily", "tools.web.providers.tavily.apiKey": None})
    reply = await _run(tool, action="set", value=batch)
    assert "tools.web.providers.tavily.apiKey is still not set" in reply
    assert calls.calls == [("settings.set", {"key": "tools.web.search.provider", "value": "tavily"})]

    data = json.loads(config_file.read_text())
    data["tools"]["web"] = {"providers": {"tavily": {"apiKey": "tvly-typed-on-the-card"}}}
    config_file.write_text(json.dumps(data))
    reply = await _run(tool, action="set", path="tools.web.providers.tavily.apiKey", value="null")
    assert "is set" in reply and "tvly-typed-on-the-card" not in reply


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
    assert key == {**key, "secret": True, "was": "not set", "input": {"via": "settings.set"}}
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
async def test_a_secret_named_to_be_typed_asks_like_any_change():
    params = {"action": "set", "path": "tools.web.providers.tavily.apiKey", "value": "null"}
    decision = await _gate(PermissionsConfig(mode="ask")).check("raven_config", params)
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
    assert tool.approval_evidence({"action": "set", "path": "session.model", "value": value})["was"].endswith(
        "(the default)"
    )
    assert set(seen) == {"tui:abc"}
    assert "session" not in json.loads(config_file.read_text())
