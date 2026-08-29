"""Every declared result model, checked against what its handler really returns.

Top-level models, to be exact. A nested row model is only reached when the
handler produces a row, and this fixture is deliberately empty, so
``ExtToolRow``, ``McpSnapshot``, ``ApiUsageModel``, ``ToolUsageCount`` and
``CronRun`` are validated against an empty list here. They are read against
their producers by hand instead; a fixture rich enough to populate all five
would be a second implementation of the runtime.

``test_rpc_schema_match`` compares the two halves of the contract to each other,
and ``test_rpc_registration`` compares the method sets. Neither of them looks
at a handler, so all three sides can agree perfectly on a shape no code produces
-- which is not hypothetical: ``session.create`` and ``session.resume`` are
declared as ``{session, last_messages}`` and have always returned
``{session_id, info, messages}``.

The models are ``extra="forbid"``, so a key a handler returns and the contract
does not declare fails here. That is the direction a hand-written contract
actually drifts: a field gets added to a response and nobody opens models.py.

Not covered, and each for a reason rather than for convenience:

* ``system.upgrade`` -- its only reachable branch outside a live ``raven serve``
  is the refusal, so there is no result to check. Its params are empty.
* ``memory.delete`` -- the delete goes through EverOS's own repository layer,
  imported inside the call. Faking that is faking the thing under test.
* the ``plug.* / plughub.* / skillhub.*`` group and the rest of ``subagents.*``
  -- validated against ``METHOD_MODELS`` in their own modules, where the
  fixtures that drive those handlers already live. This file used to claim that
  of the market group and it was not true: those tests exercised the handlers
  but asserted on individual keys, so ten declarations had nothing holding them
  to the code.
* ``turn.*`` and ``cli.dispatch`` -- driven by their own suites.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from raven.config.loader import set_config_path
from raven.rpc.models import METHOD_MODELS

_NOT_SUPPORTED_IN_V01 = -32012


def _check(method: str, payload: dict) -> BaseModel:
    """Validate a handler's real return value against its declared result model."""
    _, model = METHOD_MODELS[method]
    return model.model_validate(payload)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A config file, a cron dir and a workspace, none of them the user's.

    ``set_config_path`` moves the runtime subdirectories (cron among them), which
    covers every handler that derives its path from the config -- but not one
    that does not: ``settings_usage`` reads ``Path.home() / ".raven" /
    "telemetry"`` directly, so ``HOME`` is moved for it separately. Without
    that, the test scanned the developer's own telemetry and validated whatever
    happened to be there.

    The teardown restores whatever was there before rather than clearing it.
    Clearing does not mean "no config": it means the developer's own
    ``~/.raven/config.json``, so a later test in the same process reads their
    language setting and their workspace. That is a cross-file failure whose
    cause is in this file, and it only shows up when collection order puts this
    file first.
    """
    import raven.config.loader as loader

    previous = loader._current_config_path
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg_path = tmp_path / "config.json"
    # The workspace goes in the file rather than onto a loaded object: several
    # of these handlers call `load_config` themselves, from inside the call, so
    # patching one module's binding would move only some of them.
    cfg_path.write_text(json.dumps({"language": "en", "agents": {"defaults": {"workspace": str(ws)}}}))
    set_config_path(cfg_path)
    yield ws
    loader._current_config_path = previous


# ---------------------------------------------------------------------------
# console: ext.list / cron.* / settings.* / channels.status / fs.*
# ---------------------------------------------------------------------------


async def test_ext_list(workspace: Path) -> None:
    from raven.rpc.methods.console import ext_list

    _check("ext.list", await ext_list({}))


async def test_cron_round_trip(workspace: Path) -> None:
    """One job through save / list / set_enabled / runs / delete.

    Driven end to end rather than per-handler because ``cron.runs`` and
    ``cron.set_enabled`` both refuse an id that does not exist, so a save has to
    come first anyway -- and a saved-then-listed job is the shape that matters.
    """
    from raven.rpc.methods import console

    saved = await console.cron_save({"kind": "cron", "expr": "0 9 * * *", "name": "n", "message": "m"})
    _check("cron.save", saved)
    job_id = saved["job"]["id"]

    _check("cron.list", await console.cron_list({}))
    _check("cron.set_enabled", await console.cron_set_enabled({"id": job_id, "enabled": False}))
    _check("cron.runs", await console.cron_runs({"id": job_id}))
    _check("cron.delete", await console.cron_delete({"id": job_id}))


async def test_cron_save_every_and_at(workspace: Path) -> None:
    """The other two schedule kinds, which fill different optional fields."""
    from raven.rpc.methods import console

    _check("cron.save", await console.cron_save({"kind": "every", "every_seconds": 60, "name": "e", "message": "m"}))
    _check(
        "cron.save",
        await console.cron_save({"kind": "at", "at_iso": "2030-01-01T09:00:00", "name": "a", "message": "m"}),
    )


async def test_settings_get_and_set(workspace: Path) -> None:
    from raven.rpc.methods import console

    _check("settings.get", await console.settings_get({}))
    _check("settings.set", await console.settings_set({"key": "language", "value": "zh"}))
    # The raw-write branch returns the same shape through a different path.
    _check("settings.set", await console.settings_set({"key": "plugins.disabled", "value": ["x"]}))


async def test_settings_usage(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.rpc.methods.console import settings_usage

    # The telemetry dir hangs off `Path.home()`, not off the config path, so the
    # fixture alone leaves this reading the developer's real usage files.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _check("settings.usage", await settings_usage({"days": 1}))


async def test_settings_usage_reads_what_the_default_tracker_writes(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer/reader pairing, driven end to end.

    It regressed once: ``install_from_config``'s callers pointed UsageTracker
    at ``workspace/.token_wise`` while this reader scans ``RAVEN_HOME/telemetry``,
    so default-on usage tracking wrote rows the settings page reported as
    ``calls=0``. The pairing holds only while both sides resolve through the
    same default, which is what writing through one and reading through the
    other checks.
    """
    from raven.contracts.token_strategy import UsageSnapshot
    from raven.rpc.methods.console import settings_usage
    from raven.token_wise.usage_tracker import UsageTracker

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    tracker = UsageTracker()
    await tracker.after_llm_call({}, UsageSnapshot(model="stub/model", input_tokens=10, output_tokens=2))

    out = await settings_usage({"days": 1})
    _check("settings.usage", out)
    assert out["llm"]["total"]["calls"] == 1, "a row written by the default tracker is visible to the reader"
    assert [m["model"] for m in out["llm"]["models"]] == ["stub/model"]


async def test_settings_everos(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.config import update_everos
    from raven.rpc.methods import console

    monkeypatch.setattr(update_everos, "everos_root", lambda: tmp_path)
    # Upstream gates the write primitives on root ownership; a tmp root is not
    # one raven created, so declare it owned for the test.
    monkeypatch.setattr(update_everos, "everos_owned", lambda: True)
    _check("settings.everos", await console.settings_everos({}))
    _check(
        "settings.everosSet",
        await console.settings_everos_set({"section": "llm", "fields": {"model": "gpt-4o"}}),
    )
    _check("settings.everosSet", await console.settings_everos_set({"section": "rerank", "clear": True}))
    # The set has to survive the round trip: a section written and read back is
    # what the page shows, and it is the branch where `api_key_set` is true.
    await console.settings_everos_set({"section": "llm", "fields": {"api_key": "sk-x"}})
    out = _check("settings.everos", await console.settings_everos({}))
    assert out.sections["llm"].api_key_set is True


async def test_channels_status(workspace: Path) -> None:
    from raven.rpc.methods.console import channels_status

    _check("channels.status", await channels_status({}))


async def test_fs_round_trip(workspace: Path) -> None:
    import base64

    from raven.rpc.methods import console

    payload = base64.b64encode(b"hi").decode()
    uploaded = _check("fs.upload", await console.fs_upload({"name": "a.txt", "content_b64": payload}))
    _check("fs.list", await console.fs_list({}))
    _check("fs.read", await console.fs_read({"path": uploaded.path}))
    # The truncating branch reports a size larger than the content it returns.
    read = _check("fs.read", await console.fs_read({"path": uploaded.path, "max_bytes": 1}))
    assert read.truncated is True


# ---------------------------------------------------------------------------
# browser.*
#
# Only the two that answer without ever starting Chromium: `state()` returns
# early while there is no page, and `tabs()` returns [] with no context. The
# rest need a real browser and belong to their own suite.
# ---------------------------------------------------------------------------


async def test_browser_state_and_tabs_without_a_browser() -> None:
    from raven.rpc.methods.browser import browser_state, browser_tabs

    _check("browser.state", await browser_state({}))
    _check("browser.tabs", await browser_tabs({"action": "list"}))


def test_a_tab_carries_every_key_the_driver_builds() -> None:
    """The models are extra="forbid", and the driver's fifth key was missing.

    Nothing validates a `browser.tabs` reply at runtime, so a client that
    checked against the published contract would have rejected every one of
    them over a field the handler always sends.
    """
    from raven.rpc.models import BrowserTab

    BrowserTab.model_validate({"index": 0, "url": "https://x", "title": "x", "active": True, "loading": True})


# ---------------------------------------------------------------------------
# memory.*
# ---------------------------------------------------------------------------


def _everos(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    async def _post(base_url: str, path: str, body: dict) -> dict:
        return payload

    monkeypatch.setattr("raven.rpc.methods.memory._post", _post)
    monkeypatch.setattr("raven.rpc.methods.memory._cfg", lambda: ("http://x", "u", "a"))


async def test_memory_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.rpc.methods.memory import memory_stats

    _everos(monkeypatch, {"data": {"total_count": 3}})
    _check("memory.stats", await memory_stats({}))


async def test_memory_stats_with_everos_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """The degraded answer is a different code path and still has to fit."""
    from raven.rpc.methods.memory import memory_stats

    async def _boom(*a: Any, **k: Any) -> dict:
        raise RuntimeError("connection refused")

    monkeypatch.setattr("raven.rpc.methods.memory._post", _boom)
    monkeypatch.setattr("raven.rpc.methods.memory._cfg", lambda: ("http://x", "u", "a"))
    out = _check("memory.stats", await memory_stats({}))
    assert out.ok is False


@pytest.mark.parametrize(
    ("kind", "field", "row"),
    [
        (
            "episode",
            "episodes",
            {"id": "1", "session_id": "s", "timestamp": "2026-01-01", "subject": "x", "summary": "y", "episode": "z"},
        ),
        ("profile", "profiles", {"id": "2", "profile_data": {"k": "v"}}),
        ("agent_case", "agent_cases", {"id": "3", "task_intent": "t", "approach": "a", "quality_score": 0.5}),
        ("agent_skill", "agent_skills", {"id": "4", "name": "n", "description": "d", "content": "c"}),
    ],
)
async def test_memory_list_every_kind(monkeypatch: pytest.MonkeyPatch, kind: str, field: str, row: dict) -> None:
    """One MemoryItem covers four different projections; each has to validate.

    A model that only fits the kind someone happened to test with is the reason
    this is parametrized: the four share ``id`` and ``kind`` and nothing else.
    """
    from raven.rpc.methods.memory import memory_list

    _everos(monkeypatch, {"data": {field: [row], "total_count": 1}})
    out = _check("memory.list", await memory_list({"kind": kind}))
    assert out.items[0].kind == kind


async def test_memory_list_search_carries_a_score(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.rpc.methods.memory import memory_list

    _everos(monkeypatch, {"data": {"episodes": [{"id": "1", "score": 0.9}]}})
    out = _check("memory.list", await memory_list({"kind": "episode", "q": "hi"}))
    assert out.items[0].score == 0.9


# ---------------------------------------------------------------------------
# session.create / resume / close / branch / compress / status
# ---------------------------------------------------------------------------


async def test_session_create(workspace: Path) -> None:
    from raven.rpc.methods import session as session_mod

    _check("session.create", await session_mod.session_create({}))


async def test_session_resume(workspace: Path) -> None:
    from raven.rpc.methods import session as session_mod
    from raven.session.manager import SessionManager

    _check("session.resume", await session_mod.session_resume({}))

    mgr = SessionManager(workspace)
    stored = mgr.get_or_create("tui:r")
    stored.add_message("user", "hello")
    mgr.save(stored)
    out = _check("session.resume", await session_mod.session_resume({"session_id": "tui:r"}))
    assert len(out.messages) == 1


async def test_session_close(workspace: Path) -> None:
    from raven.rpc.methods import session as session_mod

    _check("session.close", await session_mod.session_close({}))
    _check("session.close", await session_mod.session_close({"session_id": "tui:nope"}))


async def test_session_branch(workspace: Path) -> None:
    from raven.rpc.methods import session as session_mod
    from raven.session.manager import SessionManager

    _check("session.branch", await session_mod.session_branch({}))

    mgr = SessionManager(workspace)
    parent = mgr.get_or_create("tui:parent")
    parent.add_message("user", "hello")
    mgr.save(parent)
    out = _check("session.branch", await session_mod.session_branch({"session_id": "tui:parent", "name": "child"}))
    assert out.session_id is not None and out.message_count == 1


async def test_session_compress_without_a_consolidator(workspace: Path) -> None:
    from raven.rpc.methods import session as session_mod

    out = _check("session.compress", await session_mod.session_compress({"session_id": "tui:x"}))
    assert out.summary.noop is True


async def test_session_compress_after_archiving(workspace: Path) -> None:
    """The branch that carries ``info`` / ``messages`` / ``usage``.

    Those three are the whole reason the init bundle is modelled: they are the
    same shapes ``session.resume`` returns, so a client can redraw from either.
    """
    from raven.rpc.methods import session as session_mod
    from raven.session.manager import SessionManager

    mgr = SessionManager(workspace)
    session = mgr.get_or_create("tui:c")
    session.add_message("user", "one")
    session.add_message("assistant", "two")
    mgr.save(session)

    class _Consolidator:
        async def maybe_consolidate_by_tokens(self, session: Any, force: bool = False) -> dict:
            return {"before_tokens": 100, "after_tokens": 10, "archived": 1}

    # The loop has to carry the two enumerations the init bundle reads, or the
    # redraw payload is skipped and this test passes without checking anything.
    loop = SimpleNamespace(
        memory_consolidator=_Consolidator(),
        context_engine=None,
        model=None,
        tools=SimpleNamespace(tool_names=["read_file"]),
        context=SimpleNamespace(skills=SimpleNamespace(list_skills=lambda **k: [{"name": "s", "source": "local"}])),
    )
    out = _check(
        "session.compress",
        await session_mod.session_compress({"session_id": "tui:c"}, agent_loop_factory=lambda: loop),
    )
    assert out.info is not None and out.messages is not None and out.usage is not None


async def test_session_status(workspace: Path) -> None:
    from raven.rpc.methods.slash_routing import session_status

    _check("session.status", await session_status({}))


# ---------------------------------------------------------------------------
# the answer sinks, slash routing, terminal
# ---------------------------------------------------------------------------


async def test_approval_respond() -> None:
    from raven.rpc.methods.approval import approval_respond

    broker = type("B", (), {"resolve": lambda self, *a, **k: True})()
    _check("approval.respond", await approval_respond({}, approval_broker=broker))
    _check(
        "approval.respond",
        await approval_respond({"approval_id": "a", "session_id": "s", "choice": "allow"}, approval_broker=broker),
    )


async def test_clarify_respond() -> None:
    from raven.rpc.methods.question import question_respond

    broker = type("B", (), {"reply": lambda self, *a: False})()
    _check("clarify.respond", await question_respond({"request_id": "r", "answer": "y"}, question_broker=broker))


async def test_confirm_respond() -> None:
    from raven.rpc.methods.confirm import confirm_respond

    broker = type("B", (), {"resolve": lambda self, *a: True})()
    _check("confirm.respond", await confirm_respond({"request_id": "r", "answer": True}, confirm_broker=broker))


async def test_completion_providers() -> None:
    from raven.rpc.methods.slash_routing import complete_path, complete_slash

    _check("complete.slash", await complete_slash({}))
    _check("complete.path", await complete_path({"word": "sr"}))


async def test_slash_exec(workspace: Path) -> None:
    from raven.rpc.methods.slash_routing import slash_exec

    _check("slash.exec", await slash_exec({"command": ""}))
    _check("slash.exec", await slash_exec({"command": "definitely-not-a-verb"}))


async def test_terminal_resize() -> None:
    from raven.rpc.methods.terminal import terminal_resize

    _check("terminal.resize", await terminal_resize({"cols": 120, "rows": 40, "session_id": "tui:x"}))


# ---------------------------------------------------------------------------
# the stubs
# ---------------------------------------------------------------------------


STUBS = [
    "voice.record",
    "session.save",
    "session.steer",
    "session.usage",
    "skills.reload",
    "reload.env",
    "sudo.respond",
    "secret.respond",
    "image.attach",
    "prompt.submit",
    "prompt.background",
]


@pytest.mark.parametrize("method", STUBS)
async def test_a_stub_refuses_in_the_declared_shape(method: str) -> None:
    """A stub declares ``StubResult`` and never returns one: the shape travels in
    the refusal's ``data``. Checking it there keeps the declaration honest rather
    than decorative, and fails the day one of these is promoted to a real handler
    whose result nobody declared."""
    from types import SimpleNamespace

    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods import register_aligned_methods

    dispatcher = Dispatcher()
    register_aligned_methods(
        dispatcher,
        emitter=SimpleNamespace(),
        approval_broker=SimpleNamespace(),
        confirm_broker=SimpleNamespace(),
        question_broker=SimpleNamespace(),
        scheduler=SimpleNamespace(),
    )

    response = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})

    assert response["error"]["code"] == _NOT_SUPPORTED_IN_V01, response
    _check(method, response["error"]["data"])


# ---------------------------------------------------------------------------
# subagents.list
# ---------------------------------------------------------------------------


async def test_subagents_list(workspace: Path) -> None:
    """`SubagentRow` is `extra="forbid"`, so a field the handler grew and the
    contract did not fails here.

    Covered because it happened: adding ACP as a third transport put an
    `upgrade_to` on every row and a third value in `kind`, and neither the
    registration gate (the method is declared) nor the schema-match test (the two
    halves agreed with each other) can see that.
    """
    import json as _json

    from raven.config.loader import get_config_path
    from raven.rpc.methods.subagents import subagents_list

    # A configured entry on the transport its preset has since left, which is the
    # one row where `upgrade_to` is not null.
    cfg_path = get_config_path()
    cfg = _json.loads(cfg_path.read_text())
    cfg["subagents"] = {
        "thirdParty": [
            {
                "name": "claude_code",
                "preset": "claude_code",
                "kind": "cli",
                "command": "true {prompt}",
            }
        ]
    }
    cfg_path.write_text(_json.dumps(cfg))

    out = _check("subagents.list", await subagents_list({"probe": False}))

    row = next(r for r in out.rows if r.name == "claude_code")
    assert row.upgrade_to == "acp", "the preset moved to acp, so a cli entry should offer the upgrade"
    assert row.mcps == []
    assert row.allow_mcp_secrets is False
