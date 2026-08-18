"""Gateway web config-admin RPC (P4): raven.subagents.{list,set,presets}."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from raven.config import (
    update_channels,
    update_everos,
    update_mcp,
    update_providers,
    update_skills,
    update_subagents,
    update_tools,
)
from raven.rpc.dispatcher import Dispatcher
from raven.web_rpc.methods_config import register_config_methods


@pytest.fixture(autouse=True)
def _pin_the_loaded_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``raven.everos.get`` off the developer's own ``~/.raven/config.json``.

    It falls back to a load whenever the dispatcher was registered without one,
    which is every everos test but one, so the backend those tests report came
    from whatever machine ran them.

    Substitute a real ``RavenConfig``, not a stand-in shaped like one. An
    earlier version of this fixture handed over ``class _Cfg: memory = _Mem()``,
    a shape no production config has, and pinned it in front of the loader the
    code was reading -- which is exactly why the suite stayed green while
    ``backend`` was in fact always None. Anything asserted against an invented
    shape only proves the invention.
    """
    from raven.config.raven import RavenConfig

    # Spelled out rather than left to the defaults: ``RavenConfig()`` is settings
    # -backed and picks up the running machine's config and environment, which is
    # the very thing this fixture exists to shut out.
    pinned = RavenConfig.model_validate({"memory": {"backend": None}})
    assert pinned.memory.backend is None
    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda *a, **k: pinned)


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


@pytest.fixture(autouse=True)
def _isolated_test_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here may touch the real ~/.raven/subagent_test_state.json.

    `raven.subagents.probe` and `.test` build a store with no explicit path, so
    without this every run of this file would accumulate verdicts in the user's
    own state file.
    """
    import raven.agent.subagent.test_state as state_mod

    monkeypatch.setattr(state_mod, "default_state_path", lambda: tmp_path / "_autouse_state.json")


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


async def test_set_rejects_local_file_access_on_an_openai_agent(cfg_path: Path) -> None:
    # The schema coerces this field away on load, so that a config an older form
    # wrote still starts. A caller sending it is a different case: it owns the
    # value and can act on the error, so the write path says no rather than
    # silently storing something other than what was asked for.
    agent = _FakeAgent()
    d = Dispatcher()
    register_config_methods(d, agent=agent)
    entry = {
        "name": "api",
        "kind": "openai",
        "baseUrl": "http://x/v1",
        "model": "m",
        "readsLocalFiles": True,
    }
    resp = await _dispatch(d, "raven.subagents.set", {"agents": [entry]})
    assert "error" in resp
    assert "readsLocalFiles" in str(resp["error"])
    assert agent.applied is None
    assert not cfg_path.exists()


async def test_set_rejects_local_file_access_under_its_snake_case_spelling(cfg_path: Path) -> None:
    # The schema accepts both spellings, so a payload that skipped the camel
    # alias must not skip the check with it.
    d = Dispatcher()
    register_config_methods(d, agent=_FakeAgent())
    entry = {
        "name": "api",
        "kind": "openai",
        "base_url": "http://x/v1",
        "model": "m",
        "reads_local_files": True,
    }
    resp = await _dispatch(d, "raven.subagents.set", {"agents": [entry]})
    assert "error" in resp
    assert not cfg_path.exists()


async def test_removing_an_agent_still_works_with_a_legacy_entry_stored(cfg_path: Path) -> None:
    # The guard lives at the RPC boundary, not in set_third_party_subagents:
    # add/remove re-write entries they read back raw, so a legacy `true` in an
    # unrelated entry would otherwise make every later add or remove fail.
    cfg_path.write_text(
        json.dumps(
            {
                "subagents": {
                    "thirdParty": [
                        {
                            "name": "legacy",
                            "kind": "openai",
                            "baseUrl": "http://x/v1",
                            "model": "m",
                            "readsLocalFiles": True,
                        },
                        {"name": "doomed", "kind": "cli", "command": "echo {prompt}"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    update_subagents.remove_third_party_subagent("doomed", config_path=cfg_path)
    remaining = update_subagents.get_third_party_subagents(config_path=cfg_path)
    assert [a["name"] for a in remaining] == ["legacy"]
    assert remaining[0]["readsLocalFiles"] is False  # healed by the write


class _FakeCron:
    """Minimal live-cron-service stand-in using the real cron dataclasses."""

    def __init__(self) -> None:
        self.jobs: list = []
        self._n = 0

    def list_jobs(self, include_disabled: bool = False):
        return self.jobs

    def add_job(self, name, schedule, message, channel=None, to=None):
        from raven.proactive_engine.schedulers.cron.types import CronJob, CronPayload

        self._n += 1
        job = CronJob(
            id=f"job{self._n}",
            name=name,
            schedule=schedule,
            payload=CronPayload(message=message, channel=channel, to=to),
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


async def test_skills_exposes_everos_knobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_skills, "get_config_path", lambda: tmp_path / "config.json")
    p = tmp_path / "config.json"
    p.write_text('{"skillForge": {"enabled": true}}', encoding="utf-8")

    d = Dispatcher()
    register_config_methods(d)

    res = await _dispatch(
        d,
        "raven.skills.set",
        {"fields": {"everos": {"enabled": True, "maxSkillsTopK": 8, "retireConfidence": 0.25}}},
    )
    assert res["result"] == {"ok": True, "restart_required": True}

    got = await _dispatch(d, "raven.skills.get", {})
    ev = got["result"]["skillforge"]["everos"]
    assert ev["enabled"] is True
    assert ev["maxSkillsTopK"] == 8
    assert ev["retireConfidence"] == 0.25
    # Untouched knobs keep their schema defaults.
    assert ev["complexTaskToolCallThreshold"] == 20


async def test_skills_set_rejects_out_of_range_everos_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_skills, "get_config_path", lambda: tmp_path / "config.json")
    (tmp_path / "config.json").write_text('{"skillForge": {"enabled": true}}', encoding="utf-8")

    d = Dispatcher()
    register_config_methods(d)

    res = await _dispatch(d, "raven.skills.set", {"fields": {"everos": {"retireConfidence": 5}}})
    assert "error" in res


async def test_everos_get_set_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    d = Dispatcher()
    register_config_methods(d)

    # Nothing configured yet: every section present, secret shown as empty.
    got = await _dispatch(d, "raven.everos.get", {})
    from raven.config.update_everos import WRITABLE_SECTIONS

    assert set(got["result"]["everos"]) == set(WRITABLE_SECTIONS)
    assert got["result"]["everos"]["llm"]["api_key"] == "(empty)"

    res = await _dispatch(
        d,
        "raven.everos.set",
        {"section": "llm", "fields": {"model": "m", "base_url": "http://x", "api_key": "sk-secret"}},
    )
    assert res["result"] == {"ok": True, "restart_required": True}

    # Read back: secret redacted, non-secret fields intact.
    got = await _dispatch(d, "raven.everos.get", {})
    llm = got["result"]["everos"]["llm"]
    assert llm["model"] == "m"
    assert llm["api_key"] == "****set****"

    # A redacted placeholder on write leaves the stored key untouched.
    await _dispatch(d, "raven.everos.set", {"section": "llm", "fields": {"model": "m2", "api_key": "****set****"}})
    raw = (tmp_path / "everos.toml").read_text(encoding="utf-8")
    assert "sk-secret" in raw and "m2" in raw

    res = await _dispatch(d, "raven.everos.clear", {"section": "llm"})
    assert res["result"] == {"ok": True, "restart_required": True}
    got = await _dispatch(d, "raven.everos.get", {})
    assert got["result"]["everos"]["llm"]["api_key"] == "(empty)"


async def test_everos_set_borrows_a_sibling_roles_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The web only ever sees a redacted key, so "reuse the llm key here" has to
    resolve on the gateway rather than by round-tripping the secret.

    Both roles sit on a catalog endpoint: borrowing is refused for a host the
    caller names freely, since that is how a key typed for one destination gets
    redirected to another."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    d = Dispatcher()
    register_config_methods(d)

    await _dispatch(
        d,
        "raven.everos.set",
        {
            "section": "llm",
            "fields": {
                "model": "m",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "sk-shared",
            },
        },
    )
    await _dispatch(
        d,
        "raven.everos.set",
        {
            "section": "embedding",
            "fields": {"model": "e", "base_url": "https://openrouter.ai/api/v1"},
            "reuse_key_from": "llm",
        },
    )

    got = (await _dispatch(d, "raven.everos.get", {}))["result"]["everos"]
    assert got["embedding"]["api_key"] == "****set****"
    assert (tmp_path / "everos.toml").read_text(encoding="utf-8").count("sk-shared") == 2

    # An explicitly typed key still wins over the borrowed one.
    await _dispatch(
        d,
        "raven.everos.set",
        {"section": "rerank", "fields": {"api_key": "sk-own"}, "reuse_key_from": "llm"},
    )
    raw = (tmp_path / "everos.toml").read_text(encoding="utf-8")
    assert "sk-own" in raw


async def test_everos_set_rejects_unknown_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")
    d = Dispatcher()
    register_config_methods(d)
    res = await _dispatch(d, "raven.everos.set", {"section": "bogus", "fields": {"model": "m"}})
    assert "error" in res


async def test_everos_test_probes_the_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    from raven.cli import onboard_everos

    monkeypatch.setattr(onboard_everos, "_probe_everos_chat", lambda model, **_: (True, "ok"))
    monkeypatch.setattr(
        onboard_everos,
        "_probe_embedding_dim",
        lambda url, headers, model: onboard_everos._REQUIRED_EMBEDDING_DIM,
    )

    d = Dispatcher()
    register_config_methods(d)

    res = await _dispatch(d, "raven.everos.test", {"section": "llm", "fields": {"model": "m", "base_url": "http://x"}})
    assert res["result"]["ok"] is True

    res = await _dispatch(
        d,
        "raven.everos.test",
        {"section": "embedding", "fields": {"model": "e", "base_url": "http://x"}},
    )
    assert res["result"]["ok"] is True


async def test_everos_borrows_the_credentials_page_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider already set up under Credentials should not be asked for the
    same secret again, and the web must never receive it to pass along."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    from raven.config import update_providers

    monkeypatch.setattr(
        update_providers,
        "get_provider_config",
        lambda name, **kw: {"api_key": "sk-from-credentials"} if name == "openrouter" else {},
    )

    d = Dispatcher()
    register_config_methods(d)

    await _dispatch(
        d,
        "raven.everos.set",
        {
            "section": "llm",
            "fields": {"model": "m", "base_url": "https://openrouter.ai/api/v1"},
            "credential_provider": "openrouter",
        },
    )
    assert "sk-from-credentials" in (tmp_path / "everos.toml").read_text(encoding="utf-8")

    # Never handed back out: the read stays redacted.
    got = (await _dispatch(d, "raven.everos.get", {}))["result"]["everos"]
    assert got["llm"]["api_key"] == "****set****"

    # A sibling role's key still wins over the credential.
    await _dispatch(d, "raven.everos.set", {"section": "embedding", "fields": {"api_key": "sk-own"}})
    await _dispatch(
        d,
        "raven.everos.set",
        {
            "section": "rerank",
            "fields": {"model": "r", "base_url": "https://openrouter.ai/api/v1"},
            "reuse_key_from": "embedding",
            "credential_provider": "openrouter",
        },
    )
    import tomllib

    data = tomllib.loads((tmp_path / "everos.toml").read_text(encoding="utf-8"))
    assert data["rerank"]["api_key"] == "sk-own"


async def test_everos_test_refuses_to_send_a_stored_key_to_an_unlisted_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probes take base_url from the caller and the key from storage, so an
    arbitrary host would turn "can edit the config" into "can read the key"."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    probed: list = []

    def _never(*a, **kw):
        probed.append(kw)
        return True, "ok"

    from raven.cli import onboard_everos

    monkeypatch.setattr(onboard_everos, "_probe_everos_chat", _never)

    d = Dispatcher()
    register_config_methods(d)
    await _dispatch(d, "raven.everos.set", {"section": "llm", "fields": {"api_key": "sk-secret", "model": "m"}})

    res = await _dispatch(
        d,
        "raven.everos.test",
        {"section": "llm", "fields": {"model": "m", "base_url": "https://attacker.example"}},
    )

    assert res["result"]["ok"] is False
    assert not probed, "the stored key was sent to a host outside the catalog"


async def test_everos_test_uses_a_roles_own_key_at_its_own_stored_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-hosted endpoint is unlisted by nature and is how EverOS usually
    runs, so gating the probe on the catalog made the primary deployment
    unprobeable -- and left ``set`` accepting exactly what ``test`` refused.
    The key and the address already sit next to each other on disk and the
    backend delivers one to the other on every boot, so the probe discloses
    nothing the stored config does not already commit to."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    seen: dict = {}

    from raven.cli import onboard_everos

    monkeypatch.setattr(
        onboard_everos,
        "_probe_everos_chat",
        lambda model, **kw: (seen.update(kw), (True, "ok"))[1],
    )

    d = Dispatcher()
    register_config_methods(d)
    await _dispatch(
        d,
        "raven.everos.set",
        {"section": "llm", "fields": {"api_key": "sk-own", "model": "m", "base_url": "http://10.0.0.9:8000/v1"}},
    )

    res = await _dispatch(
        d,
        "raven.everos.test",
        {"section": "llm", "fields": {"model": "m", "base_url": "http://10.0.0.9:8000/v1"}},
    )

    assert res["result"]["ok"] is True
    assert seen["api_key"] == "sk-own"


async def test_everos_test_refuses_a_roles_own_key_at_a_different_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exemption is for the pairing that already exists, not for the key.
    Re-aiming a role's own key at some other address is the original leak."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    probed: list = []

    from raven.cli import onboard_everos

    monkeypatch.setattr(
        onboard_everos,
        "_probe_everos_chat",
        lambda model, **kw: (probed.append(kw), (True, "ok"))[1],
    )

    d = Dispatcher()
    register_config_methods(d)
    await _dispatch(
        d,
        "raven.everos.set",
        {"section": "llm", "fields": {"api_key": "sk-own", "model": "m", "base_url": "http://10.0.0.9:8000/v1"}},
    )

    res = await _dispatch(
        d,
        "raven.everos.test",
        {"section": "llm", "fields": {"model": "m", "base_url": "https://attacker.example"}},
    )

    assert res["result"]["ok"] is False
    assert not probed, "a role's own key was re-aimed at a host it was never stored against"


async def test_everos_test_still_uses_a_stored_key_for_a_catalog_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not cost the ordinary case its saved key."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    seen: dict = {}

    from raven.cli import onboard_everos

    monkeypatch.setattr(
        onboard_everos,
        "_probe_everos_chat",
        lambda model, **kw: (seen.update(kw), (True, "ok"))[1],
    )

    d = Dispatcher()
    register_config_methods(d)
    await _dispatch(d, "raven.everos.set", {"section": "llm", "fields": {"api_key": "sk-secret", "model": "m"}})

    await _dispatch(
        d,
        "raven.everos.test",
        {
            "section": "llm",
            "fields": {"model": "m", "base_url": "https://openrouter.ai/api/v1"},
        },
    )

    assert seen["api_key"] == "sk-secret"


async def test_everos_set_refuses_to_store_a_borrowed_key_for_an_unlisted_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guarding the probe closed the immediate leak and left a deferred one: the
    write path pairs a borrowed key with a caller-named base_url, and the memory
    backend sends it there on the next gateway boot."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    from raven.config import update_providers

    monkeypatch.setattr(update_providers, "get_provider_config", lambda name, **kw: {"api_key": "sk-credential"})

    d = Dispatcher()
    register_config_methods(d)

    resp = await _dispatch(
        d,
        "raven.everos.set",
        {
            "section": "llm",
            "fields": {"model": "x", "base_url": "https://attacker.example"},
            "credential_provider": "anthropic",
        },
    )

    assert "error" in resp
    written = (tmp_path / "everos.toml").read_text(encoding="utf-8") if (tmp_path / "everos.toml").exists() else ""
    assert "sk-credential" not in written


async def test_everos_set_still_borrows_for_a_catalog_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not cost the ordinary case its borrowed key."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    from raven.config import update_providers

    monkeypatch.setattr(update_providers, "get_provider_config", lambda name, **kw: {"api_key": "sk-credential"})

    d = Dispatcher()
    register_config_methods(d)

    await _dispatch(
        d,
        "raven.everos.set",
        {
            "section": "llm",
            "fields": {"model": "x", "base_url": "https://openrouter.ai/api/v1"},
            "credential_provider": "openrouter",
        },
    )

    assert "sk-credential" in (tmp_path / "everos.toml").read_text(encoding="utf-8")


async def test_everos_set_accepts_a_typed_key_for_any_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-hosted endpoint is configured by typing its key, and that secret is
    the caller's own to spend."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    d = Dispatcher()
    register_config_methods(d)

    await _dispatch(
        d,
        "raven.everos.set",
        {"section": "llm", "fields": {"model": "x", "base_url": "http://localhost:8902/v1", "api_key": "mine"}},
    )

    assert "mine" in (tmp_path / "everos.toml").read_text(encoding="utf-8")


async def test_everos_set_refuses_to_re_aim_a_stored_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No borrowing needed: writing base_url alone keeps the section's own key
    and repoints it, so the next gateway boot ships it to the new host."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    d = Dispatcher()
    register_config_methods(d)
    await _dispatch(
        d,
        "raven.everos.set",
        {
            "section": "llm",
            "fields": {
                "model": "m",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "sk-user-secret",
            },
        },
    )

    resp = await _dispatch(
        d, "raven.everos.set", {"section": "llm", "fields": {"base_url": "https://attacker.example"}}
    )

    assert "error" in resp
    raw = (tmp_path / "everos.toml").read_text(encoding="utf-8")
    assert "attacker.example" not in raw
    assert "sk-user-secret" in raw


async def test_everos_set_keeps_editing_a_self_hosted_endpoint_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local vLLM is unlisted by nature. Changing the model on one leaves the
    pairing the user already established, so it must not be refused."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    d = Dispatcher()
    register_config_methods(d)
    await _dispatch(
        d,
        "raven.everos.set",
        {
            "section": "llm",
            "fields": {"model": "m", "base_url": "http://localhost:8902/v1", "api_key": "EMPTY"},
        },
    )

    resp = await _dispatch(
        d,
        "raven.everos.set",
        {"section": "llm", "fields": {"model": "m2", "base_url": "http://localhost:8902/v1"}},
    )

    assert "error" not in resp
    import tomllib

    llm = tomllib.loads((tmp_path / "everos.toml").read_text(encoding="utf-8"))["llm"]
    assert llm["model"] == "m2" and llm["api_key"] == "EMPTY"


async def test_everos_set_allows_a_move_when_the_key_is_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typing the key is what makes the destination the caller's own choice."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    d = Dispatcher()
    register_config_methods(d)
    await _dispatch(
        d,
        "raven.everos.set",
        {"section": "llm", "fields": {"base_url": "https://openrouter.ai/api/v1", "api_key": "old"}},
    )

    resp = await _dispatch(
        d,
        "raven.everos.set",
        {"section": "llm", "fields": {"base_url": "https://my-box.internal/v1", "api_key": "new"}},
    )

    assert "error" not in resp
    raw = (tmp_path / "everos.toml").read_text(encoding="utf-8")
    assert "my-box.internal" in raw and "new" in raw


async def test_borrowing_is_limited_to_the_writable_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer rejects an unknown section; a reader that does not lets
    reuse_key_from name any table that happens to sit in everos.toml."""
    path = tmp_path / "everos.toml"
    path.write_text('[secrets]\napi_key = "sk-not-a-role"\n', encoding="utf-8")
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: path)

    d = Dispatcher()
    register_config_methods(d)
    await _dispatch(
        d,
        "raven.everos.set",
        {"section": "llm", "fields": {"model": "m"}, "reuse_key_from": "secrets"},
    )

    import tomllib

    assert "api_key" not in tomllib.loads(path.read_text(encoding="utf-8"))["llm"]


async def test_an_empty_api_key_removes_only_the_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Blanking it used to be indistinguishable from not retyping it, so the
    only way to drop a key was deleting the section and its model with it."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    d = Dispatcher()
    register_config_methods(d)
    await _dispatch(
        d,
        "raven.everos.set",
        {"section": "llm", "fields": {"api_key": "sk-secret", "model": "m", "base_url": "http://x"}},
    )

    await _dispatch(d, "raven.everos.set", {"section": "llm", "fields": {"api_key": ""}})

    import tomllib

    llm = tomllib.loads((tmp_path / "everos.toml").read_text(encoding="utf-8"))["llm"]
    assert "api_key" not in llm
    assert llm["model"] == "m" and llm["base_url"] == "http://x"


async def test_clearing_a_key_does_not_substitute_a_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty string is a request to remove, not an absent field to fill in."""
    monkeypatch.setattr(update_everos, "get_everos_config_path", lambda: tmp_path / "everos.toml")

    from raven.config import update_providers

    monkeypatch.setattr(update_providers, "get_provider_config", lambda name, **kw: {"api_key": "sk-credential"})

    d = Dispatcher()
    register_config_methods(d)
    await _dispatch(d, "raven.everos.set", {"section": "llm", "fields": {"api_key": "sk-secret", "model": "m"}})

    await _dispatch(
        d,
        "raven.everos.set",
        {"section": "llm", "fields": {"api_key": ""}, "credential_provider": "openrouter"},
    )

    import tomllib

    assert "api_key" not in tomllib.loads((tmp_path / "everos.toml").read_text(encoding="utf-8"))["llm"]


async def test_everos_get_reports_the_active_memory_backend() -> None:
    """Configuring these models is inert unless memory.backend is everos, so the
    page has to be told which backend is really in force.

    Built from the real ``RavenConfig``: ``memory`` is an extension block that
    only exists there, so a test that invents an object carrying the attribute
    proves nothing about what the gateway can actually read.
    """
    from raven.config.raven import RavenConfig

    active = RavenConfig.model_validate({"memory": {"backend": "everos"}})
    d = Dispatcher()
    register_config_methods(d, raven_config=active)
    assert (await _dispatch(d, "raven.everos.get", {}))["result"]["backend"] == "everos"


async def test_everos_get_does_not_read_the_backend_off_the_base_config(tmp_path: Path) -> None:
    """The regression this replaces: ``memory`` is in ``loader.EXTENSION_KEYS``,
    so it is stripped before the base ``Config`` validates and that class never
    declares it. Reading the backend off the object the gateway passes as
    ``config`` therefore always answered None, and the readiness card the
    feature exists to provide could never leave "markdown".

    ``config`` is a real ``Config`` -- loaded from a path that does not exist,
    so it is the genuine class with genuine defaults and no dependency on the
    config file of whoever runs this.
    """
    from raven.config.loader import load_config
    from raven.config.raven import RavenConfig

    base = load_config(tmp_path / "absent.json")
    assert not hasattr(type(base), "memory"), "base Config gained a memory field; this test is moot"

    d = Dispatcher()
    register_config_methods(d, config=base, raven_config=RavenConfig.model_validate({"memory": {"backend": "everos"}}))
    assert (await _dispatch(d, "raven.everos.get", {}))["result"]["backend"] == "everos"


async def test_everos_providers_strip_the_registry_prefix() -> None:
    """Registry ids are provider-qualified; an endpoint wants what follows its
    own slug, so a stale prefix would be sent as part of the model name."""
    d = Dispatcher()
    register_config_methods(d)
    provs = {p["name"]: p for p in (await _dispatch(d, "raven.everos.providers", {}))["result"]["providers"]}

    for name, p in provs.items():
        for m in p["chat_models"]:
            assert not m.startswith(f"{name}/"), f"{name} still carries its own prefix: {m}"
    # OpenRouter keeps the upstream vendor prefix, which is part of its id.
    assert any(m.startswith("anthropic/") for m in provs["openrouter"]["chat_models"])


async def test_everos_providers_lists_the_catalog() -> None:
    d = Dispatcher()
    register_config_methods(d)
    res = await _dispatch(d, "raven.everos.providers", {})
    provs = res["result"]["providers"]
    assert any(p["name"] == "openrouter" for p in provs)
    row = next(p for p in provs if p["name"] == "openrouter")
    assert row["base_url"].startswith("https://")
    assert isinstance(row["supports"], list) and "llm" in row["supports"]


async def test_everos_models_delegates_to_the_fetcher(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.cli import onboard_everos

    captured: dict = {}

    def _fake_fetch(base_url, api_key, *, section, provider_name):
        captured.update(base_url=base_url, api_key=api_key, section=section, provider_name=provider_name)
        return ["m-a", "m-b"]

    monkeypatch.setattr(onboard_everos, "_fetch_everos_models", _fake_fetch)

    d = Dispatcher()
    register_config_methods(d)
    res = await _dispatch(
        d,
        "raven.everos.models",
        {"section": "embedding", "base_url": "http://x", "api_key": "k", "provider_name": "deepinfra"},
    )
    assert res["result"]["models"] == ["m-a", "m-b"]
    assert captured == {
        "base_url": "http://x",
        "api_key": "k",
        "section": "embedding",
        "provider_name": "deepinfra",
    }


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
        self.node_calls: list[tuple[str, str, int, str | None]] = []
        self.run_calls: list[tuple[str, str | None]] = []

    def active_run_ids(self) -> list[str]:
        return [self.RUN_ID] if self._live else []

    async def read_run(self, run_id: str, session_key: str | None = None) -> dict:
        self.run_calls.append((run_id, session_key))
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

    async def read_node(
        self, run_id: str, node_id: str, *, max_output_chars: int = 20000, session_key: str | None = None
    ) -> dict:
        self.node_calls.append((run_id, node_id, max_output_chars, session_key))
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
    assert run["summary"] == {"total": 2, "completed": 1, "failed": 0, "skipped": 0, "cancelled": 0}


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

    tool = _ReadableDagTool(finalized=False)
    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithReadableDag(tool))

    resp = await _dispatch(d, "raven.subagents.dag.get", {"run_id": _ReadableDagTool.RUN_ID, "session_key": "web:s1"})
    assert {f["status"] for f in resp["result"]["run"]["files"]} == {"pending"}
    # The key is not only the registry-overlay scope: it also selects the
    # working directory the run dir is read from, so it has to reach the tool.
    assert tool.run_calls == [(_ReadableDagTool.RUN_ID, "web:s1")]


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
    assert tool.node_calls == [(_ReadableDagTool.RUN_ID, "node-a", 500, None)]


async def test_dag_node_forwards_the_session_key() -> None:
    """Without it the tool resolves the run dir against agent home, where a
    per-session run was never written, and the node reads back blank."""
    tool = _ReadableDagTool(finalized=True)
    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithReadableDag(tool))

    await _dispatch(
        d,
        "raven.subagents.dag.node",
        {"run_id": _ReadableDagTool.RUN_ID, "node": "node-a", "session_key": "web:s1"},
    )
    assert tool.node_calls == [(_ReadableDagTool.RUN_ID, "node-a", 20000, "web:s1")]


@pytest.mark.parametrize("method", ["raven.subagents.dag.get", "raven.subagents.dag.node"])
async def test_dag_reads_without_a_dag_tool_error_rather_than_returning_empty(method: str) -> None:
    d = Dispatcher()
    register_config_methods(d, agent=None)
    resp = await _dispatch(d, method, {"run_id": _ReadableDagTool.RUN_ID, "node": "node-a"})
    assert "error" in resp


async def test_gateway_restart_replies_ok_then_schedules_reexec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """raven.gateway.restart acknowledges immediately, then re-execs behind a
    short delay (so the reply flushes before the process image is replaced)."""
    import raven.web_rpc.methods_config as mc

    calls: list[bool] = []
    monkeypatch.setattr(mc, "_reexec_process", lambda: calls.append(True))

    d = Dispatcher()
    register_config_methods(d)

    resp = await _dispatch(d, "raven.gateway.restart", {})
    assert resp["result"] == {"ok": True}
    assert calls == []  # not yet -- it is deferred

    await asyncio.sleep(0.6)
    assert calls == [True]


async def test_channels_qr_renders_pending_login_code() -> None:
    """raven.channels.qr renders a channel's pending login QR to a PNG data URI,
    and reports connected from the adapter's own login flag."""

    class _Ch:
        pending_qr: str | None = "https://example.com/login?token=abc"
        is_running = True
        connected = False

    class _Mgr:
        def get_channel(self, name: str):
            return _Ch() if name == "weixin" else None

    d = Dispatcher()
    register_config_methods(d, channel_manager=_Mgr())

    resp = await _dispatch(d, "raven.channels.qr", {"name": "weixin"})
    r = resp["result"]
    assert r["qr"].startswith("data:image/png;base64,")
    assert r["qr_text"] is None  # rasterised, so no raw-payload fallback
    assert r["connected"] is False  # a QR is pending -> not paired yet
    assert r["running"] is True

    _Ch.pending_qr = None
    _Ch.connected = True  # paired
    resp = await _dispatch(d, "raven.channels.qr", {"name": "weixin"})
    assert resp["result"] == {"qr": None, "qr_text": None, "connected": True, "running": True}

    resp = await _dispatch(d, "raven.channels.qr", {"name": "nope"})
    assert resp["result"] == {"qr": None, "qr_text": None, "connected": False, "running": False}


async def test_channels_qr_is_not_connected_before_the_first_qr_arrives() -> None:
    """Both QR adapters flip _running before fetching the first QR. Reporting
    connected off "running and no QR pending" would call that window paired and
    stop the UI polling, so it has to come from the adapter's login flag."""

    class _Ch:
        pending_qr = None  # not fetched yet
        is_running = True  # start() already set this
        connected = False  # but nobody has scanned anything

    class _Mgr:
        def get_channel(self, name: str):
            return _Ch()

    d = Dispatcher()
    register_config_methods(d, channel_manager=_Mgr())

    resp = await _dispatch(d, "raven.channels.qr", {"name": "whatsapp"})
    assert resp["result"]["connected"] is False
    assert resp["result"]["running"] is True


async def test_channels_qr_falls_back_to_raw_payload_without_qrcode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qrcode ships only with the QR-login extras, so a gateway without it must
    hand the client the raw payload instead of raising ModuleNotFoundError."""
    from raven.web_rpc import methods_config as mc

    monkeypatch.setattr(mc, "_render_qr_png", lambda text: None)

    class _Ch:
        pending_qr = "https://example.com/login?token=abc"
        is_running = True
        connected = False

    class _Mgr:
        def get_channel(self, name: str):
            return _Ch()

    d = Dispatcher()
    register_config_methods(d, channel_manager=_Mgr())

    r = (await _dispatch(d, "raven.channels.qr", {"name": "weixin"}))["result"]
    assert r["qr"] is None
    assert r["qr_text"] == "https://example.com/login?token=abc"


# ---------------------------------------------------------------------------
# raven.channels.qr against the real adapters. The RPC reaches into the channel
# by attribute name (pending_qr / is_running / connected), and a fake channel
# would keep every assertion green through a rename on the adapter side. These
# drive the adapters' own code paths so the contract fails loudly instead.
# ---------------------------------------------------------------------------


async def test_channels_qr_reads_a_real_whatsapp_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    from raven.channels.adapters.whatsapp.channel import WhatsAppChannel
    from raven.config.schema import WhatsAppConfig

    monkeypatch.setattr("raven.config.paths.get_runtime_subdir", lambda name: tmp_path / name)
    ch = WhatsAppChannel(WhatsAppConfig(enabled=True))
    ch._running = True

    class _Mgr:
        def get_channel(self, name: str):
            return ch

    d = Dispatcher()
    register_config_methods(d, channel_manager=_Mgr())

    # Nothing pending yet, and the task being up is not being paired.
    r = (await _dispatch(d, "raven.channels.qr", {"name": "whatsapp"}))["result"]
    assert r == {"qr": None, "qr_text": None, "connected": False, "running": True}

    await ch._handle_bridge_message(_json.dumps({"type": "qr", "qr": "2@abc"}))
    r = (await _dispatch(d, "raven.channels.qr", {"name": "whatsapp"}))["result"]
    assert r["qr"].startswith("data:image/png;base64,")
    assert r["connected"] is False

    await ch._handle_bridge_message(_json.dumps({"type": "status", "status": "connected"}))
    r = (await _dispatch(d, "raven.channels.qr", {"name": "whatsapp"}))["result"]
    assert r == {"qr": None, "qr_text": None, "connected": True, "running": True}


async def test_channels_qr_reads_a_real_weixin_adapter() -> None:
    from unittest.mock import AsyncMock

    from raven.channels.adapters.weixin.channel import WeixinChannel
    from raven.config.schema import WeixinConfig

    ch = WeixinChannel(WeixinConfig())
    ch._running = True
    ch._save_state = lambda: None
    ch._print_qr = lambda url: None

    class _Mgr:
        def get_channel(self, name: str):
            return ch

    d = Dispatcher()
    register_config_methods(d, channel_manager=_Mgr())

    r = (await _dispatch(d, "raven.channels.qr", {"name": "weixin"}))["result"]
    assert r == {"qr": None, "qr_text": None, "connected": False, "running": True}

    # Park the login flow on "waiting to be scanned" and read the RPC mid-flight.
    ch._fetch_qr = AsyncMock(return_value=("qid", "https://scan/1"))
    ch._get = AsyncMock(return_value={"status": "waiting"})
    login = asyncio.create_task(ch._qr_login())
    for _ in range(50):
        await asyncio.sleep(0)
        if ch.pending_qr:
            break
    r = (await _dispatch(d, "raven.channels.qr", {"name": "weixin"}))["result"]
    assert r["qr"].startswith("data:image/png;base64,")
    assert r["connected"] is False

    ch._running = False  # unwind the poll loop
    login.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await login


async def test_subagents_probe_covers_config_and_presets(cfg_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import raven.agent.subagent.probe as probe_mod

    # A real capture shells out to `bash -lic` (up to 15s) and would make the
    # result depend on what happens to be installed on the test machine.
    monkeypatch.setattr(probe_mod, "_login_path", lambda: "/nonexistent-probe-path")
    d = Dispatcher()
    register_config_methods(d, agent=_FakeAgent())
    await _dispatch(d, "raven.subagents.set", {"agents": [{"name": "mine", "kind": "cli", "command": "nope {prompt}"}]})

    resp = await _dispatch(d, "raven.subagents.probe", {})
    assert "error" not in resp, resp
    results = resp["result"]["results"]
    by_key = {(r["source"], r["name"]): r for r in results}
    assert by_key[("config", "mine")]["status"] == "missing"
    # Every built-in preset is probed too, so the Presets group can show what is
    # installed before the user commits to configuring it.
    assert ("preset", "claude_code") in by_key
    # A keyless openai preset is a template: reported, but never requested.
    assert by_key[("preset", "mirothinker")]["status"] == "attention"
    assert set(results[0]) == {"name", "source", "kind", "status", "detail", "target", "elapsedMs", "lastTest"}


async def test_subagents_test_reports_an_unknown_name_without_raising(cfg_path: Path) -> None:
    d = Dispatcher()
    register_config_methods(d, agent=_FakeAgent())
    resp = await _dispatch(d, "raven.subagents.test", {"name": "ghost", "source": "config"})
    assert "error" not in resp, resp
    result = resp["result"]["result"]
    assert result["ok"] is False
    assert result["kind"] is None
    assert "no such subagent" in result["detail"]


async def test_subagents_test_rejects_an_unknown_source(cfg_path: Path) -> None:
    d = Dispatcher()
    register_config_methods(d, agent=_FakeAgent())
    resp = await _dispatch(d, "raven.subagents.test", {"name": "x", "source": "wherever"})
    assert "error" in resp
    # A bare `"error" in resp` also passes when the method is not registered at
    # all, so it cannot tell a rejected source from any other failure. Pin both
    # ends: the error is not method_not_found, it names the offending field, and
    # the same call with a valid source does not error at all -- which is what
    # attributes the rejection to `source` rather than to anything else.
    assert resp["error"]["message"] != "method_not_found"
    assert "source" in resp["error"].get("data", {}).get("traceback_tail", "")
    ok_resp = await _dispatch(d, "raven.subagents.test", {"name": "x", "source": "preset"})
    assert "error" not in ok_resp, ok_resp
    assert ok_resp["result"]["result"]["ok"] is False


async def test_subagents_test_runs_the_saved_entry(
    cfg_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    import raven.agent.subagent.backends.env as env_mod
    import raven.agent.subagent.probe as probe_mod

    exe = tmp_path / "rpc-agent"
    exe.write_text("#!/bin/sh\necho PONG\n")
    exe.chmod(0o755)
    monkeypatch.setattr(probe_mod, "_login_path", lambda: f"{tmp_path}:/usr/bin:/bin")
    # `_login_path` alone only steers the pre-check probe; the real spawn below
    # builds its child env straight from `login_shell_env()`'s process-wide
    # cache, so that cache needs the same PATH (mirrors
    # test_subagent_probe.py's `_patch_login_env_for_spawn`).
    monkeypatch.setattr(
        env_mod, "_LOGIN_ENV", {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/root")}
    )
    d = Dispatcher()
    register_config_methods(d, agent=_FakeAgent())
    await _dispatch(
        d, "raven.subagents.set", {"agents": [{"name": "runme", "kind": "cli", "command": "rpc-agent {prompt}"}]}
    )
    resp = await _dispatch(d, "raven.subagents.test", {"name": "runme", "source": "config"})
    assert "error" not in resp, resp
    assert resp["result"]["result"]["ok"] is True
    assert resp["result"]["result"]["reply"] == "PONG"


async def test_subagents_test_records_a_verdict_the_probe_then_returns(
    cfg_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    import raven.agent.subagent.backends.env as env_mod
    import raven.agent.subagent.probe as probe_mod

    exe = tmp_path / "verdict-agent"
    exe.write_text("#!/bin/sh\necho PONG\n")
    exe.chmod(0o755)
    monkeypatch.setattr(probe_mod, "_login_path", lambda: f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setattr(
        env_mod,
        "_LOGIN_ENV",
        {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/root")},
    )

    d = Dispatcher()
    register_config_methods(d, agent=_FakeAgent())
    await _dispatch(
        d,
        "raven.subagents.set",
        {"agents": [{"name": "verdicts", "kind": "cli", "command": "verdict-agent {prompt}"}]},
    )
    resp = await _dispatch(d, "raven.subagents.test", {"name": "verdicts", "source": "config"})
    assert resp["result"]["result"]["ok"] is True

    probe = await _dispatch(d, "raven.subagents.probe", {})
    row = next(r for r in probe["result"]["results"] if r["source"] == "config" and r["name"] == "verdicts")
    assert row["lastTest"] is not None
    assert row["lastTest"]["ok"] is True
    assert row["lastTest"]["testedAtMs"] > 0


async def test_mcp_list_reports_config_plus_live_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The panel needs both halves: what is configured, and whether the agent
    actually connected it (MCP connects lazily, on the first turn that needs it)."""
    monkeypatch.setattr(update_mcp, "get_config_path", lambda: tmp_path / "config.json")
    p = tmp_path / "config.json"
    p.write_text(
        '{"tools": {"mcpServers": {"browser": {"command": "npx"}, "api": {"url": "https://x.test/mcp"}}}}',
        encoding="utf-8",
    )

    class _Tools:
        tool_names = ["mcp_browser_click", "mcp_browser_navigate", "read_file"]

    class _Agent:
        tools = _Tools()
        _mcp_connected = True

    d = Dispatcher()
    register_config_methods(d, agent=_Agent())

    res = await _dispatch(d, "raven.mcp.list", {})
    assert "error" not in res, res
    assert res["result"]["connected"] is True
    servers = {s["name"]: s for s in res["result"]["servers"]}
    assert servers["browser"]["connected"] is True
    # Reported without the mcp_<server>_ prefix the registry adds.
    assert servers["browser"]["tools"] == ["click", "navigate"]
    # Configured but contributed no tools: not connected, not an error either.
    assert servers["api"]["connected"] is False
    assert servers["api"]["tools"] == []


async def test_mcp_list_adds_no_live_key_the_write_path_would_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel round-trips this response into ``raven.mcp.set``, which drops
    exactly ``MCP_RUNTIME_KEYS`` before writing. A live key added here without
    being listed there would silently start landing in the user's config, so
    pin the two lists to each other rather than trusting them to stay in step."""
    monkeypatch.setattr(update_mcp, "get_config_path", lambda: tmp_path / "config.json")
    (tmp_path / "config.json").write_text(
        '{"tools": {"mcpServers": {"browser": {"command": "npx"}}}}', encoding="utf-8"
    )

    class _Agent:
        tools = type("_Tools", (), {"tool_names": ["mcp_browser_click"]})()
        _mcp_connected = True

    d = Dispatcher()
    register_config_methods(d, agent=_Agent())

    res = await _dispatch(d, "raven.mcp.list", {})
    listed = set(res["result"]["servers"][0])
    configured = set(update_mcp.get_mcp_servers(config_path=tmp_path / "config.json")[0])
    assert listed - configured <= update_mcp.MCP_RUNTIME_KEYS


async def test_mcp_list_without_an_agent_reports_nothing_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_mcp, "get_config_path", lambda: tmp_path / "config.json")
    (tmp_path / "config.json").write_text('{"tools": {"mcpServers": {"a": {"command": "x"}}}}', encoding="utf-8")
    d = Dispatcher()
    register_config_methods(d)

    res = await _dispatch(d, "raven.mcp.list", {})
    assert res["result"]["connected"] is False
    assert res["result"]["servers"][0]["connected"] is False


async def test_mcp_set_writes_and_asks_for_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_mcp, "get_config_path", lambda: tmp_path / "config.json")
    p = tmp_path / "config.json"
    p.write_text('{"tools": {}}', encoding="utf-8")
    d = Dispatcher()
    register_config_methods(d)

    res = await _dispatch(d, "raven.mcp.set", {"servers": [{"name": "browser", "command": "npx"}]})
    assert res["result"] == {"ok": True, "restart_required": True}
    assert "browser" in p.read_text(encoding="utf-8")

    res = await _dispatch(d, "raven.mcp.list", {})
    assert [s["name"] for s in res["result"]["servers"]] == ["browser"]


async def test_mcp_set_surfaces_a_rejected_server_as_an_rpc_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_mcp, "get_config_path", lambda: tmp_path / "config.json")
    (tmp_path / "config.json").write_text('{"tools": {}}', encoding="utf-8")
    d = Dispatcher()
    register_config_methods(d)

    res = await _dispatch(d, "raven.mcp.set", {"servers": [{"name": "broken"}]})
    assert "error" in res


async def test_mcp_list_attributes_a_tool_to_the_longest_matching_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mcp_<server>_<tool>`` puts no reserved character between the two, so a
    plain prefix match lets a short server name claim a longer one's tools."""
    monkeypatch.setattr(update_mcp, "get_config_path", lambda: tmp_path / "config.json")
    p = tmp_path / "config.json"
    p.write_text(
        '{"tools": {"mcpServers": {"foo": {"command": "a"}, "foo_bar": {"command": "b"}}}}',
        encoding="utf-8",
    )

    class _Tools:
        tool_names = ["mcp_foo_ping", "mcp_foo_bar_query", "read_file"]

    class _Agent:
        tools = _Tools()
        _mcp_connected = True

    d = Dispatcher()
    register_config_methods(d, agent=_Agent())

    res = await _dispatch(d, "raven.mcp.list", {})
    servers = {s["name"]: s for s in res["result"]["servers"]}
    assert servers["foo"]["tools"] == ["ping"]
    assert servers["foo_bar"]["tools"] == ["query"]


async def test_mcp_list_still_lists_the_good_servers_beside_a_broken_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_mcp, "get_config_path", lambda: tmp_path / "config.json")
    (tmp_path / "config.json").write_text(
        '{"tools": {"mcpServers": {"ok": {"command": "npx"}, "bad": {"command": 123}}}}',
        encoding="utf-8",
    )
    d = Dispatcher()
    register_config_methods(d)

    res = await _dispatch(d, "raven.mcp.list", {})
    assert "error" not in res, res
    servers = {s["name"]: s for s in res["result"]["servers"]}
    assert servers["ok"]["command"] == "npx"
    assert servers["bad"]["error"]


# ---------------------------------------------------------------------------
# raven.tools.{list,set} -- the Serper key and the media tools' key/model.
# ---------------------------------------------------------------------------


@pytest.fixture
def tool_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One config file for both writers, and no ambient keys.

    ``update_tools`` and ``update_providers`` each bound their own
    ``get_config_path``, so both have to be pointed at it. The env vars matter
    as much: ``keySource`` reports what would actually supply the key, so a
    developer who exports one would see these assert the wrong branch.
    """
    path = tmp_path / "config.json"
    monkeypatch.setattr(update_tools, "get_config_path", lambda: path)
    monkeypatch.setattr(update_providers, "get_config_path", lambda: path)
    for var in ("SERPER_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return path


class _FakeToolNames:
    """``AgentLoop.tools`` as far as ``raven.tools.list`` reads it."""

    def __init__(self, *names: str) -> None:
        self.tool_names = list(names)


class _AgentWithTools:
    def __init__(self, *names: str) -> None:
        self.tools = _FakeToolNames(*names)


async def test_tools_list_describes_what_is_not_configured(tool_cfg: Path) -> None:
    d = Dispatcher()
    register_config_methods(d)

    rows = (await _dispatch(d, "raven.tools.list", {}))["result"]["tools"]
    assert [r["kind"] for r in rows] == ["web_search", "image", "speech", "video"]

    web = rows[0]
    assert web["registered"] is False
    assert web["apiKey"] == "(empty)"
    assert web["keySource"] == "none"
    # The page cannot tell the user where to put a key it has to name itself.
    assert web["settingPath"] == "tools.web.search.apiKey"
    assert web["envKey"] == "SERPER_API_KEY"

    image = rows[1]
    assert image["configured"] is False
    # Read off the tool class, so the page offers the model the tool would
    # actually pick rather than a copy that can drift from it.
    assert image["defaultModel"] == "google/gemini-2.5-flash-image"
    assert image["defaultApiBase"].startswith("https://openrouter.ai")
    assert image["tool"] == "image_generate"


async def test_tools_set_writes_and_says_a_restart_is_needed(tool_cfg: Path) -> None:
    d = Dispatcher()
    register_config_methods(d)

    resp = await _dispatch(d, "raven.tools.set", {"kind": "web_search", "fields": {"api_key": "serper-abc"}})
    assert "error" not in resp, resp
    assert resp["result"]["restart_required"] is True
    assert json.loads(tool_cfg.read_text())["tools"]["web"]["search"]["apiKey"] == "serper-abc"

    web = (await _dispatch(d, "raven.tools.list", {}))["result"]["tools"][0]
    assert web["apiKey"] == "****set****"
    assert web["keySource"] == "own"
    # Still false, and that is the whole point of restart_required: the tool is
    # registered where the loop is built, so this process never gains it.
    assert web["registered"] is False


async def test_tools_list_reads_the_live_registry_not_the_file(tool_cfg: Path) -> None:
    # The inverse case of the one above: a key exported into the environment the
    # gateway started with registers the tool while the config file stays empty.
    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithTools("web_search", "image_generate"))

    rows = (await _dispatch(d, "raven.tools.list", {}))["result"]["tools"]
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["web_search"]["registered"] is True
    assert by_kind["web_search"]["apiKey"] == "(empty)"
    assert by_kind["image"]["registered"] is True
    assert by_kind["speech"]["registered"] is False


async def test_media_borrows_the_openrouter_key_but_is_not_enabled_by_it(tool_cfg: Path) -> None:
    update_providers.set_provider_fields("openrouter", {"api_key": "sk-or"})
    d = Dispatcher()
    register_config_methods(d)

    image = {r["kind"]: r for r in (await _dispatch(d, "raven.tools.list", {}))["result"]["tools"]}["image"]
    assert image["keySource"] == "openrouter"
    # AgentLoop's rule, restated: an OpenRouter key set for chat never surfaces
    # image/speech/video to the model. A model (or its own key) is what does.
    assert image["configured"] is False

    await _dispatch(d, "raven.tools.set", {"kind": "image", "fields": {"model": "google/gemini-2.5-flash-image"}})
    image = {r["kind"]: r for r in (await _dispatch(d, "raven.tools.list", {}))["result"]["tools"]}["image"]
    assert image["configured"] is True
    assert image["keySource"] == "openrouter"  # still borrowed; it has none of its own


async def test_tools_set_rejects_an_unknown_kind(tool_cfg: Path) -> None:
    d = Dispatcher()
    register_config_methods(d)
    resp = await _dispatch(d, "raven.tools.set", {"kind": "music", "fields": {"model": "m"}})
    assert "error" in resp
    assert not tool_cfg.exists()


async def test_tools_methods_are_served_by_the_gateway_registration(tool_cfg: Path) -> None:
    """Registering them on a dispatcher this file built proves nothing about the
    gateway. Driven through ``register_web_methods``, which is what the gateway
    calls, so a group dropped there turns this red -- and the live loop has to
    reach the handler, or ``registered`` is silently False for every tool.
    """
    from raven.web_rpc.methods import register_web_methods

    class _Emitter:
        async def emit(self, session_key: str, event: dict) -> None:  # pragma: no cover - unused
            pass

    d = Dispatcher()
    register_web_methods(
        d,
        emitter=_Emitter(),
        scheduler=None,
        turn_ids={},
        direct_targets={},
        agent=_AgentWithTools("web_search"),
        cron=None,
        config=None,
        channel_manager=None,
        raven_config=None,
    )
    assert {"raven.tools.list", "raven.tools.set"} <= set(d.methods())

    rows = (await _dispatch(d, "raven.tools.list", {}))["result"]["tools"]
    assert rows[0]["registered"] is True, "the handler never reached the live loop"
