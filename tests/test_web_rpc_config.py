"""Gateway web config-admin RPC (P4): raven.subagents.{list,set,presets}."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.config import update_channels, update_skills, update_subagents
from raven.tui_rpc.dispatcher import Dispatcher
from raven.web_rpc.methods_config import register_config_methods


class _FakeAgent:
    def __init__(self) -> None:
        self.applied: list | None = None

    def apply_third_party_subagents(self, configs: list) -> None:
        self.applied = list(configs)


class _FakeToolRegistry:
    """Stands in for ``ToolRegistry``'s ``.get(name)`` accessor (not a dict)."""

    def __init__(self, tools: dict[str, object] | None = None) -> None:
        self._tools = tools or {}

    def get(self, name: str) -> object | None:
        return self._tools.get(name)


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "config.json"
    monkeypatch.setattr(update_subagents, "get_config_path", lambda: p)
    return p


async def _dispatch(d: Dispatcher, method: str, params: dict, rid: int = 1) -> dict:
    return await d.dispatch({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


async def test_presets_list_set_and_hotapply(cfg_path: Path) -> None:
    agent = _FakeAgent()
    d = Dispatcher()
    register_config_methods(d, agent=agent)

    # presets
    resp = await _dispatch(d, "raven.subagents.presets", {})
    names = {p["name"] for p in resp["result"]["presets"]}
    assert {"claude_code", "codex", "mirothinker"} <= names

    # set -> validates + writes + hot-applies to the agent
    resp = await _dispatch(
        d,
        "raven.subagents.set",
        {"agents": [{"name": "claude_code", "kind": "cli", "command": "claude -p {prompt}"}]},
    )
    assert "error" not in resp, resp
    assert resp["result"]["ok"] is True
    assert cfg_path.exists()
    assert agent.applied is not None and agent.applied[0].name == "claude_code"

    # list reflects it
    resp = await _dispatch(d, "raven.subagents.list", {})
    assert [a["name"] for a in resp["result"]["agents"]] == ["claude_code"]


async def test_set_invalid_returns_error_and_does_not_apply(cfg_path: Path) -> None:
    agent = _FakeAgent()
    d = Dispatcher()
    register_config_methods(d, agent=agent)
    resp = await _dispatch(d, "raven.subagents.set", {"agents": [{"name": "bad", "kind": "nope"}]})
    assert "error" in resp
    assert agent.applied is None  # not applied on validation failure
    assert not cfg_path.exists()


class _FakeCron:
    """Minimal live-cron-service stand-in using the real cron dataclasses."""

    def __init__(self) -> None:
        self.jobs: list = []
        self._n = 0

    def list_jobs(self, include_disabled: bool = False):
        return self.jobs

    def add_job(self, name, schedule, message, deliver=False, channel=None, to=None):
        from raven.proactive_engine.schedulers.cron.types import CronJob, CronPayload

        self._n += 1
        job = CronJob(
            id=f"job{self._n}",
            name=name,
            schedule=schedule,
            payload=CronPayload(message=message, channel=channel, to=to, deliver=deliver),
        )
        self.jobs.append(job)
        return job

    def remove_job(self, job_id: str) -> bool:
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j.id != job_id]
        return len(self.jobs) != before


async def test_cron_add_list_remove() -> None:
    cron = _FakeCron()
    d = Dispatcher()
    register_config_methods(d, cron=cron)

    resp = await _dispatch(
        d, "raven.cron.add", {"name": "morning", "schedule": {"kind": "cron", "expr": "0 9 * * *"}, "message": "wake"}
    )
    assert "error" not in resp, resp
    job = resp["result"]["job"]
    assert job["name"] == "morning" and job["schedule"]["expr"] == "0 9 * * *" and job["message"] == "wake"

    resp = await _dispatch(d, "raven.cron.list", {})
    assert [j["name"] for j in resp["result"]["jobs"]] == ["morning"]

    resp = await _dispatch(d, "raven.cron.remove", {"job_id": job["id"]})
    assert resp["result"]["removed"] is True
    resp = await _dispatch(d, "raven.cron.list", {})
    assert resp["result"]["jobs"] == []


async def test_channels_list_and_enable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_channels, "get_config_path", lambda: tmp_path / "config.json")
    d = Dispatcher()
    register_config_methods(d)

    resp = await _dispatch(d, "raven.channels.list", {})
    channels = resp["result"]["channels"]
    assert len(channels) > 0
    first = channels[0]
    assert set(first) >= {"name", "enabled", "specs", "config"}
    assert first["enabled"] is False
    name = first["name"]

    resp = await _dispatch(d, "raven.channels.set", {"name": name, "fields": {"enabled": "true"}})
    assert "error" not in resp, resp
    assert resp["result"]["restart_required"] is True

    resp = await _dispatch(d, "raven.channels.list", {})
    got = next(c for c in resp["result"]["channels"] if c["name"] == name)
    assert got["enabled"] is True


async def test_skills_get_set_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_skills, "get_config_path", lambda: tmp_path / "config.json")
    p = tmp_path / "config.json"
    p.write_text('{"skillForge": {"enabled": true}}', encoding="utf-8")

    d = Dispatcher()
    register_config_methods(d)

    got = await _dispatch(d, "raven.skills.get", {})
    assert got["result"]["skillforge"]["enabled"] is True

    res = await _dispatch(d, "raven.skills.set", {"fields": {"enabled": False}})
    assert res["result"] == {"ok": True, "restart_required": True}
    assert '"enabled": false' in p.read_text(encoding="utf-8")

    res = await _dispatch(d, "raven.skills.list", {})
    assert "skills" in res["result"]
    assert isinstance(res["result"]["skills"], list)


async def test_skills_body_and_hub_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.config import skill_ops

    class _Meta:
        id, name, description = "id-1", "alpha", "d"
        path, content, source = Path("/ws/alpha/SKILL.md"), "# body", "workspace"

    class _Cat:
        def gather_all_skills(self):
            return [_Meta()]

    class _Client:
        async def search(self, q, *, category=None, sort=None, limit=20):
            return [{"id": "1", "name": "beta"}]

        async def install(self, skill_id):
            return {"slug": "beta", "version": "v2", "dir": "/ws/skills/hub/beta@v2"}

        async def get(self, skill_id):
            return {"skill_md": "# hub", "name": "beta", "version": "v2"}

        async def aclose(self):
            pass

    monkeypatch.setattr(skill_ops, "_catalog", lambda: _Cat())
    monkeypatch.setattr(skill_ops, "_build_client", lambda endpoint=None, api_key=None: _Client())

    d = Dispatcher()
    register_config_methods(d)

    body = await _dispatch(d, "raven.skills.body", {"name": "alpha"})
    assert body["result"]["skillMd"] == "# body"

    hub_body = await _dispatch(d, "raven.skills.body", {"id": "1", "source": "hub"})
    assert hub_body["result"]["skillMd"] == "# hub"

    test = await _dispatch(d, "raven.skills.hub.test", {"endpoint": "https://h"})
    assert test["result"]["ok"] is True

    search = await _dispatch(d, "raven.skills.hub.search", {"q": "b", "limit": 3})
    assert search["result"]["items"][0]["name"] == "beta"

    install = await _dispatch(d, "raven.skills.hub.install", {"id": "1"})
    assert install["result"] == {"ok": True, "slug": "beta", "version": "v2", "dir": "/ws/skills/hub/beta@v2"}


async def test_subagent_instances_list_and_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.commit("web:s1", "claude_code", "refactor", "sess-a")
    await reg.commit("web:s2", "codex", "review", "th-b")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    d = Dispatcher()
    register_config_methods(d)

    resp = await _dispatch(d, "raven.subagents.instances", {})
    assert {r["handle"] for r in resp["result"]["instances"]} == {"refactor", "review"}

    resp = await _dispatch(d, "raven.subagents.instances", {"session_key": "web:s1"})
    only = resp["result"]["instances"]
    assert len(only) == 1
    assert only[0]["agent"] == "claude_code"
    assert only[0]["agentId"] == "sess-a"


async def test_subagent_instances_delete_by_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.commit("web:s1", "claude_code", "refactor", "sess-a")
    await reg.commit("web:s2", "codex", "review", "th-b")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    d = Dispatcher()
    register_config_methods(d)

    resp = await _dispatch(d, "raven.subagents.instances.delete", {"session_key": "web:s1"})
    assert resp["result"]["removed"] == 1

    resp = await _dispatch(d, "raven.subagents.instances", {})
    assert [r["handle"] for r in resp["result"]["instances"]] == ["review"]


async def test_dag_cancel_routes_to_the_tool() -> None:
    class _FakeDagTool:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def request_cancel(self, run_id: str) -> bool:
            self.asked.append(run_id)
            return run_id == "run-live"

    class _AgentWithDag:
        def __init__(self, tool: object) -> None:
            self.tools = _FakeToolRegistry({"run_subagent_dag": tool})

    tool = _FakeDagTool()
    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithDag(tool))

    resp = await _dispatch(d, "raven.subagents.dag.cancel", {"run_id": "run-live"})
    assert resp["result"]["cancelled"] is True
    resp = await _dispatch(d, "raven.subagents.dag.cancel", {"run_id": "run-gone"})
    assert resp["result"]["cancelled"] is False
    assert tool.asked == ["run-live", "run-gone"]


async def test_dag_cancel_without_a_dag_tool_is_false() -> None:
    d = Dispatcher()
    register_config_methods(d, agent=None)
    resp = await _dispatch(d, "raven.subagents.dag.cancel", {"run_id": "x"})
    assert resp["result"]["cancelled"] is False


async def test_dag_cancel_when_tool_registry_lacks_the_tool_is_false() -> None:
    class _AgentNoDag:
        def __init__(self) -> None:
            self.tools = _FakeToolRegistry({})

    d = Dispatcher()
    register_config_methods(d, agent=_AgentNoDag())
    resp = await _dispatch(d, "raven.subagents.dag.cancel", {"run_id": "x"})
    assert resp["result"]["cancelled"] is False


async def test_instances_cancel_routes_to_the_manager() -> None:
    class _FakeManager:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        async def cancel_by_instance(self, session_key: str, agent: str, handle: str) -> bool:
            self.calls.append((session_key, agent, handle))
            return handle == "live"

    class _AgentWithManager:
        def __init__(self, manager: object) -> None:
            self.subagents = manager

    manager = _FakeManager()
    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithManager(manager))

    resp = await _dispatch(
        d, "raven.subagents.instances.cancel", {"session_key": "web:s1", "agent": "claude_code", "handle": "live"}
    )
    assert resp["result"]["cancelled"] is True
    resp = await _dispatch(
        d, "raven.subagents.instances.cancel", {"session_key": "web:s1", "agent": "claude_code", "handle": "gone"}
    )
    assert resp["result"]["cancelled"] is False
    assert manager.calls == [
        ("web:s1", "claude_code", "live"),
        ("web:s1", "claude_code", "gone"),
    ]


async def test_instances_cancel_without_a_manager_is_false() -> None:
    d = Dispatcher()
    register_config_methods(d, agent=None)
    resp = await _dispatch(
        d, "raven.subagents.instances.cancel", {"session_key": "web:s1", "agent": "claude_code", "handle": "h"}
    )
    assert resp["result"]["cancelled"] is False


async def test_instances_reconciles_cli_row_not_live_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_spawn("web:s1", "claude_code", "live", "running", "sess-a")
    await reg.upsert_spawn("web:s1", "claude_code", "dead", "running", "sess-b")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    class _FakeManager:
        def live_handles(self, session_key: str) -> set[tuple[str, str]]:
            assert session_key == "web:s1"
            return {("claude_code", "live")}

    class _AgentWithManager:
        subagents = _FakeManager()

    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithManager())

    resp = await _dispatch(d, "raven.subagents.instances", {"session_key": "web:s1"})
    rows = {r["handle"]: r["status"] for r in resp["result"]["instances"]}
    assert rows == {"live": "running", "dead": "interrupted"}


async def test_instances_reconciles_pending_cli_row_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """spawn() now writes a `pending` row before a spawn ever acquires the
    gate (I3); a `pending` row must be reconciled exactly like a `running`
    one -- a gateway restart while a spawn is still queued must not leave a
    dead `pending` row wearing a stop button that answers `cancelled: false`
    forever. A `pending` row the manager still reports live, by contrast,
    must be left alone (it may just be genuinely queued behind a full gate)."""
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_spawn("web:s1", "claude_code", "queued-live", "pending")
    await reg.upsert_spawn("web:s1", "claude_code", "queued-dead", "pending")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    class _FakeManager:
        def live_handles(self, session_key: str) -> set[tuple[str, str]]:
            assert session_key == "web:s1"
            return {("claude_code", "queued-live")}

    class _AgentWithManager:
        subagents = _FakeManager()

    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithManager())

    resp = await _dispatch(d, "raven.subagents.instances", {"session_key": "web:s1"})
    rows = {r["handle"]: r["status"] for r in resp["result"]["instances"]}
    assert rows == {"queued-live": "pending", "queued-dead": "interrupted"}


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_instances_does_not_reconcile_terminal_cli_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Only the two non-terminal cli statuses (`pending`, `running`) are
    reconciled. `completed` / `failed` / `cancelled` already reflect how the
    spawn actually ended; rewriting one to `interrupted` just because the
    manager (correctly) no longer reports it live -- it finished, so of
    course it isn't live -- would misreport a real outcome as an
    interruption."""
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_spawn("web:s1", "claude_code", "done", status)
    monkeypatch.setattr(instances_mod, "_registry", reg)

    class _FakeManager:
        def live_handles(self, session_key: str) -> set[tuple[str, str]]:
            return set()  # nothing live -- must not matter for a terminal status

    class _AgentWithManager:
        subagents = _FakeManager()

    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithManager())

    resp = await _dispatch(d, "raven.subagents.instances", {"session_key": "web:s1"})
    rows = {r["handle"]: r["status"] for r in resp["result"]["instances"]}
    assert rows == {"done": status}


async def test_instances_reconciles_dag_node_row_not_active_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_dag_node("web:s1", "run-live", "node-a", "claude_code", "running")
    await reg.upsert_dag_node("web:s1", "run-gone", "node-b", "claude_code", "pending")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    class _FakeDagTool:
        def active_run_ids(self) -> list[str]:
            return ["run-live"]

    class _AgentWithDag:
        def __init__(self) -> None:
            self.tools = _FakeToolRegistry({"run_subagent_dag": _FakeDagTool()})

    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithDag())

    resp = await _dispatch(d, "raven.subagents.instances", {"session_key": "web:s1"})
    rows = {r["runId"]: r["status"] for r in resp["result"]["instances"]}
    assert rows == {"run-live": "running", "run-gone": "interrupted"}


async def test_instances_reconciliation_is_conservative_without_an_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_spawn("web:s1", "claude_code", "h", "running", "sess-a")
    await reg.upsert_dag_node("web:s1", "run-x", "node-a", "claude_code", "running")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    d = Dispatcher()
    register_config_methods(d, agent=None)

    resp = await _dispatch(d, "raven.subagents.instances", {"session_key": "web:s1"})
    rows = {r["handle"]: r["status"] for r in resp["result"]["instances"]}
    assert rows == {"h": "interrupted", "run-x/node-a": "interrupted"}


async def test_instances_reconciliation_calls_live_handles_once_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_spawn("web:s1", "claude_code", "a", "running", "sess-a")
    await reg.upsert_spawn("web:s1", "claude_code", "b", "running", "sess-b")
    await reg.upsert_spawn("web:s1", "claude_code", "c", "running", "sess-c")
    await reg.upsert_spawn("web:s1", "claude_code", "d", "running", "sess-d")
    await reg.upsert_spawn("web:s1", "claude_code", "e", "running", "sess-e")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    class _FakeManager:
        def __init__(self) -> None:
            self.calls = 0

        def live_handles(self, session_key: str) -> set[tuple[str, str]]:
            self.calls += 1
            return {("claude_code", "a")}

    class _AgentWithManager:
        def __init__(self, manager: object) -> None:
            self.subagents = manager

    manager = _FakeManager()
    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithManager(manager))

    resp = await _dispatch(d, "raven.subagents.instances", {"session_key": "web:s1"})
    assert len(resp["result"]["instances"]) == 5
    assert manager.calls == 1


def _model_cfg():
    """Config with two vendors keyed, so routable/unroutable both have coverage."""
    from raven.config.schema import Config

    cfg = Config()
    cfg.agents.defaults.model = "deepseek/deepseek-v3"
    cfg.providers.deepseek.api_key = "KD"
    cfg.providers.anthropic.api_key = "KA"
    return cfg


class _FakeSessions:
    """Stands in for SessionManager: get_or_create + save, cache semantics included."""

    def __init__(self) -> None:
        from raven.session.manager import Session

        self._by_key: dict[str, Session] = {}
        self.saved: list[str] = []
        self._Session = Session

    def get_or_create(self, key: str):
        if key not in self._by_key:
            self._by_key[key] = self._Session(key=key)
        return self._by_key[key]

    def save(self, session) -> None:
        self.saved.append(session.key)


async def test_session_model_set_then_get_round_trips(cfg_path):
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=_model_cfg())

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.set",
            "params": {"session_key": "web:abc", "model": "anthropic/claude-opus-4-5"},
        }
    )
    assert r["result"] == {"ok": True, "model": "anthropic/claude-opus-4-5"}
    assert agent.sessions.saved == ["web:abc"]

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "raven.session.model.get",
            "params": {"session_key": "web:abc"},
        }
    )
    assert r["result"] == {"model": "anthropic/claude-opus-4-5", "default": "deepseek/deepseek-v3"}


async def test_session_model_get_is_none_when_unset(cfg_path):
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=_model_cfg())

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.get",
            "params": {"session_key": "web:fresh"},
        }
    )
    assert r["result"] == {"model": None, "default": "deepseek/deepseek-v3"}


async def test_session_model_get_reports_no_default_without_a_config(cfg_path):
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=None)

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.get",
            "params": {"session_key": "web:fresh"},
        }
    )
    assert r["result"] == {"model": None, "default": None}


async def test_session_model_set_null_clears_the_override(cfg_path):
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    agent.sessions.get_or_create("web:abc").metadata["model"] = "anthropic/claude-opus-4-5"
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=_model_cfg())

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.set",
            "params": {"session_key": "web:abc", "model": None},
        }
    )
    assert r["result"] == {"ok": True, "model": None}
    assert "model" not in agent.sessions.get_or_create("web:abc").metadata


async def test_session_model_set_rejects_an_unroutable_model_without_writing(cfg_path):
    """Validate before write: a bad model must not land in metadata (fail-fast)."""
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=_model_cfg())

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.set",
            "params": {"session_key": "web:abc", "model": "totally-unknown-vendor/x"},
        }
    )
    assert "error" in r
    assert agent.sessions.saved == []
    assert "model" not in agent.sessions.get_or_create("web:abc").metadata


async def test_session_model_set_rejects_a_blank_string(cfg_path):
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=_model_cfg())

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.set",
            "params": {"session_key": "web:abc", "model": "   "},
        }
    )
    assert "error" in r
    assert agent.sessions.saved == []
    assert "model" not in agent.sessions.get_or_create("web:abc").metadata


async def test_session_model_set_strips_padding_before_storing(cfg_path):
    """A value that validates must behave the way it reads -- stripped, not padded."""
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=_model_cfg())

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.set",
            "params": {"session_key": "web:abc", "model": " anthropic/claude-opus-4-5 "},
        }
    )
    assert r["result"] == {"ok": True, "model": "anthropic/claude-opus-4-5"}
    assert agent.sessions.get_or_create("web:abc").metadata["model"] == "anthropic/claude-opus-4-5"


async def test_session_model_set_accepts_a_curated_model_from_a_configured_gateway():
    """The picker offers `custom`'s curated models, and `custom` has no registry
    keywords -- so a name-only check rejects exactly what the picker just showed."""
    from raven.config.schema import Config

    cfg = Config()
    cfg.providers.custom.api_key = "KC"
    cfg.providers.custom.api_base = "http://localhost:9000/v1"
    cfg.providers.custom.models = ["in-house-7b"]

    assert cfg.serving_provider_for_model("in-house-7b") == "custom"

    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=cfg)

    r = await _dispatch(d, "raven.session.model.set", {"session_key": "web:abc", "model": "in-house-7b"})
    assert r["result"] == {"ok": True, "model": "in-house-7b"}


async def test_session_model_set_accepts_a_curated_model_from_a_configured_local_provider():
    from raven.config.schema import Config

    cfg = Config()
    cfg.providers.hosted_vllm.api_base = "http://localhost:8000/v1"
    cfg.providers.hosted_vllm.models = ["Qwen3-32B"]

    assert cfg.serving_provider_for_model("Qwen3-32B") == "hosted_vllm"

    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=cfg)

    r = await _dispatch(d, "raven.session.model.set", {"session_key": "web:abc", "model": "Qwen3-32B"})
    assert r["result"] == {"ok": True, "model": "Qwen3-32B"}


async def test_session_model_set_rejects_a_model_whose_vendor_is_not_configured():
    """The registry recognizes `gemini/...` but no Gemini key is set, so the call
    would silently fall through to Anthropic's adapter. Reject instead."""
    cfg = _model_cfg()

    assert cfg.serving_provider_for_model("gemini/gemini-3-pro") is None

    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=cfg)

    r = await _dispatch(d, "raven.session.model.set", {"session_key": "web:abc", "model": "gemini/gemini-3-pro"})
    assert "error" in r
    assert agent.sessions.saved == []
    assert "model" not in agent.sessions.get_or_create("web:abc").metadata


async def test_session_model_set_rejects_a_pinned_default_provider():
    """With the provider pinned, Config._match_provider ignores the model name --
    every session's pick would resolve to that one vendor."""
    cfg = _model_cfg()
    cfg.agents.defaults.provider = "custom"
    cfg.providers.custom.api_key = "KC"

    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=cfg)

    r = await _dispatch(d, "raven.session.model.set", {"session_key": "web:abc", "model": "anthropic/claude-opus-4-5"})
    assert "error" in r
    assert "agents.defaults.provider" in str(r["error"])
    assert agent.sessions.saved == []
    assert "model" not in agent.sessions.get_or_create("web:abc").metadata


async def test_session_model_set_rejects_a_non_string_model(cfg_path):
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=_model_cfg())

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.set",
            "params": {"session_key": "web:abc", "model": 123},
        }
    )
    assert "error" in r
    assert agent.sessions.saved == []
    assert "model" not in agent.sessions.get_or_create("web:abc").metadata


async def test_instances_reconciliation_does_not_mutate_the_cached_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import instances as instances_mod

    path = tmp_path / "inst.json"
    reg = instances_mod.InstanceRegistry(path=path)
    await reg.upsert_spawn("web:s1", "claude_code", "h", "running", "sess-a")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    class _FakeManager:
        def live_handles(self, session_key: str) -> set[tuple[str, str]]:
            return set()  # nothing live -> the row must reconcile to "interrupted"

    class _AgentWithManager:
        subagents = _FakeManager()

    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithManager())

    resp = await _dispatch(d, "raven.subagents.instances", {"session_key": "web:s1"})
    assert resp["result"]["instances"][0]["status"] == "interrupted"

    # The registry's own cached record must be untouched by the reconciled read.
    assert reg._records[("web:s1", "claude_code", "h")]["status"] == "running"

    # A second, independent read straight from disk must also still say "running".
    fresh = instances_mod.InstanceRegistry(path=path)
    assert fresh.list_instances()[0]["status"] == "running"


class _ReadableDagTool:
    """A DAG tool whose read_run/read_node serve one canned run."""

    RUN_ID = "20260730T060242Z-6b0b89a3"

    def __init__(self, *, finalized: bool, live: bool = True) -> None:
        self._finalized = finalized
        self._live = live
        self.node_calls: list[tuple[str, str, int]] = []

    def active_run_ids(self) -> list[str]:
        return [self.RUN_ID] if self._live else []

    async def read_run(self, run_id: str) -> dict:
        status = "completed" if self._finalized else "pending"
        return {
            "run_id": run_id,
            "dir": f"/w/.ravenx_dag/{run_id}",
            "finalized": self._finalized,
            "terminal_outputs": [],
            "files": [
                {
                    "node": nid,
                    "subagent": "claude_code",
                    "depends_on": [],
                    "instance": None,
                    "status": status,
                    "started_at": None,
                    "ended_at": None,
                    "prompt_file": None,
                    "output_file": None,
                    "error": None,
                    "prompt_template": f"do {nid}",
                }
                for nid in ("node-a", "node-b")
            ],
            "summary": {"total": 2, "completed": 2 if self._finalized else 0, "failed": 0, "skipped": 0},
        }

    async def read_node(self, run_id: str, node_id: str, *, max_output_chars: int = 20000) -> dict:
        self.node_calls.append((run_id, node_id, max_output_chars))
        return {"run_id": run_id, "node": node_id, "prompt": "P", "output": "O"}


class _AgentWithReadableDag:
    def __init__(self, tool: object) -> None:
        self.tools = _FakeToolRegistry({"run_subagent_dag": tool})


async def test_dag_get_returns_a_finalized_run_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    # A stale registry row must NOT override a finalized manifest -- the
    # manifest is the authoritative record once it exists.
    await reg.upsert_dag_node("web:s1", _ReadableDagTool.RUN_ID, "node-a", "claude_code", "running")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithReadableDag(_ReadableDagTool(finalized=True)))

    resp = await _dispatch(d, "raven.subagents.dag.get", {"run_id": _ReadableDagTool.RUN_ID})
    run = resp["result"]["run"]
    assert {f["status"] for f in run["files"]} == {"completed"}
    assert run["summary"]["completed"] == 2


async def test_dag_get_overlays_registry_rows_on_an_unfinalized_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_dag_node("web:s1", _ReadableDagTool.RUN_ID, "node-a", "claude_code", "completed")
    await reg.upsert_dag_node("web:s1", _ReadableDagTool.RUN_ID, "node-b", "claude_code", "running")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithReadableDag(_ReadableDagTool(finalized=False)))

    resp = await _dispatch(d, "raven.subagents.dag.get", {"run_id": _ReadableDagTool.RUN_ID, "session_key": "web:s1"})
    run = resp["result"]["run"]
    by_node = {f["node"]: f for f in run["files"]}
    assert by_node["node-a"]["status"] == "completed"
    assert by_node["node-b"]["status"] == "running"
    # The first registry write for a node is its "running" transition, so it
    # doubles as the start time the UI resumes its duration counter from.
    assert by_node["node-b"]["started_at"] > 0
    assert by_node["node-b"]["ended_at"] is None
    assert by_node["node-a"]["ended_at"] > 0
    assert run["summary"] == {"total": 2, "completed": 1, "failed": 0, "skipped": 0}


async def test_dag_get_reports_a_dead_runs_live_looking_nodes_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_dag_node("web:s1", _ReadableDagTool.RUN_ID, "node-a", "claude_code", "completed")
    await reg.upsert_dag_node("web:s1", _ReadableDagTool.RUN_ID, "node-b", "claude_code", "running")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    d = Dispatcher()
    # The run is not among the tool's active ids -- e.g. the gateway restarted
    # mid-run -- so node-b cannot still be running whatever its row says.
    register_config_methods(d, agent=_AgentWithReadableDag(_ReadableDagTool(finalized=False, live=False)))

    resp = await _dispatch(d, "raven.subagents.dag.get", {"run_id": _ReadableDagTool.RUN_ID, "session_key": "web:s1"})
    by_node = {f["node"]: f for f in resp["result"]["run"]["files"]}
    assert by_node["node-a"]["status"] == "completed"
    assert by_node["node-b"]["status"] == "interrupted"
    # Reconciled to terminal, so it gets an end time rather than reading as
    # still-open forever.
    assert by_node["node-b"]["ended_at"] > 0


async def test_dag_get_of_another_session_does_not_leak_its_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_dag_node("web:s2", _ReadableDagTool.RUN_ID, "node-a", "claude_code", "completed")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithReadableDag(_ReadableDagTool(finalized=False)))

    resp = await _dispatch(d, "raven.subagents.dag.get", {"run_id": _ReadableDagTool.RUN_ID, "session_key": "web:s1"})
    assert {f["status"] for f in resp["result"]["run"]["files"]} == {"pending"}


async def test_dag_node_forwards_the_output_cap() -> None:
    tool = _ReadableDagTool(finalized=True)
    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithReadableDag(tool))

    resp = await _dispatch(
        d,
        "raven.subagents.dag.node",
        {"run_id": _ReadableDagTool.RUN_ID, "node": "node-a", "max_output_chars": 500},
    )
    assert resp["result"]["node"]["output"] == "O"
    assert tool.node_calls == [(_ReadableDagTool.RUN_ID, "node-a", 500)]


@pytest.mark.parametrize("method", ["raven.subagents.dag.get", "raven.subagents.dag.node"])
async def test_dag_reads_without_a_dag_tool_error_rather_than_returning_empty(method: str) -> None:
    d = Dispatcher()
    register_config_methods(d, agent=None)
    resp = await _dispatch(d, method, {"run_id": _ReadableDagTool.RUN_ID, "node": "node-a"})
    assert "error" in resp
