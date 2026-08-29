"""Tests for ``session.create`` / ``session.close`` / ``session.resume`` handlers.

These handlers return realistic return shapes (NOT -32012 errors) because
ui-tui's ``useSessionLifecycle.ts`` boot path requires a usable
``SessionInfo`` payload before the UI renders.

A previous shape (session-manager-record: ``session_id`` / ``channel`` /
``chat_id`` / ``created_at`` / …) was wrong — it didn't match the TS ``SessionInfo`` at
``ui-tui/src/types.ts:148`` that ``SessionPanel`` consumes via
``Object.entries(info.skills)``. The current tests assert the
SessionPanel-required keys (``model`` / ``skills`` / ``tools``) plus the
optional banner fields actually populated by the stub.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.workdir import WorkdirPolicy, WorkdirResolver, default_channel_root
from raven.config.loader import load_config
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.errors import TurnInProgressError
from raven.rpc.methods import session as session_module
from raven.rpc.methods import turn as turn_module
from raven.rpc.methods.session import (
    _map_to_wire,
    register_session_methods,
    session_archive,
    session_branch,
    session_close,
    session_create,
    session_delete,
    session_export,
    session_list,
    session_most_recent,
    session_pin,
    session_resume,
    session_title,
)
from raven.session.manager import SessionManager

_SESSION_ID_RE = re.compile(r"^tui:\d{8}_\d{6}_[0-9a-f]{6}$")

# Required by SessionPanel (``ui-tui/src/components/branding.tsx``):
#   * ``info.model``  — line 231  ``info.model.split('/').pop()``
#   * ``info.skills`` — line 138  ``Object.entries(info.skills)``
#   * ``info.tools``  — line 166  ``Object.entries(info.tools)``
_SESSION_PANEL_REQUIRED_KEYS = {"model", "skills", "tools"}


def _assert_session_info(info: dict) -> None:
    """Assert that ``info`` matches the TS ``SessionInfo`` wire shape that
    ``SessionPanel`` consumes (``ui-tui/src/types.ts:148``)."""
    # Must contain everything SessionPanel reads without optional-chaining.
    assert _SESSION_PANEL_REQUIRED_KEYS.issubset(set(info)), (
        f"missing SessionPanel-required keys; got {set(info)}, missing {_SESSION_PANEL_REQUIRED_KEYS - set(info)}"
    )
    # Types: skills / tools must be dicts so ``Object.entries`` works in JS.
    assert isinstance(info["skills"], dict), "info.skills must be a dict (Object.entries target)"
    assert isinstance(info["tools"], dict), "info.tools must be a dict (Object.entries target)"
    assert isinstance(info["model"], str) and info["model"], "info.model must be a non-empty str"


async def test_session_create_returns_panel_compatible_info() -> None:
    result = await session_create({})
    assert set(result) == {"session_id", "info"}
    assert _SESSION_ID_RE.match(result["session_id"]), result["session_id"]
    _assert_session_info(result["info"])


async def test_session_close_returns_ok() -> None:
    result = await session_close({})
    assert result == {"ok": True}


async def test_session_resume_unknown_id_returns_fresh_mint_with_empty_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown session_id returns a fresh-minted key + empty messages (no error, no file)."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    result = await session_resume({"session_id": "tui:nonexistent_id"})
    assert set(result) == {"session_id", "info", "messages"}
    assert _SESSION_ID_RE.match(result["session_id"]), result["session_id"]
    _assert_session_info(result["info"])
    assert result["messages"] == []


async def test_session_info_skills_and_tools_are_empty_dicts_in_v01() -> None:
    """Regression for P0 fix #2: ``Object.entries(info.skills)`` in
    ``SessionPanel`` (``ui-tui/src/components/branding.tsx:138``) must not
    throw ``"Cannot convert undefined or null to object"``. Empty-dict v0.1
    placeholders satisfy that contract."""
    result = await session_create({})
    assert result["info"]["skills"] == {}
    assert result["info"]["tools"] == {}


@pytest.mark.parametrize(
    "method,expected_keys",
    [
        ("session.create", {"session_id", "info"}),
        ("session.close", {"ok"}),
        ("session.resume", {"session_id", "info", "messages"}),
    ],
)
async def test_session_handlers_dispatch_via_dispatcher(method: str, expected_keys: set[str]) -> None:
    """End-to-end: each handler is reachable through the Dispatcher and
    returns a JSON-RPC success frame (no ``error`` key)."""
    d = Dispatcher()
    register_session_methods(d)
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})
    assert "error" not in resp, f"{method} unexpectedly raised: {resp}"
    assert set(resp["result"]) == expected_keys


async def test_session_create_ignores_extra_params() -> None:
    """Extra params are accepted without error; channel override is not honoured."""
    result = await session_create({"unused": "ignored", "channel": "telegram", "cols": 120})
    assert _SESSION_ID_RE.match(result["session_id"]), result["session_id"]
    _assert_session_info(result["info"])


async def test_session_create_returns_unique_ids_per_call() -> None:
    r1 = await session_create({})
    r2 = await session_create({})
    assert r1["session_id"] != r2["session_id"], "two consecutive session.create calls returned the same session_id"


async def test_session_create_writes_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lazy mint: session.create must not create any file under the workspace.

    The handler does not touch the filesystem today; this test pins the seam
    a future in-module persistence would go through (module-level
    ``load_config`` -> ``config.workspace_path`` -> ``SessionManager``).
    ``SessionManager.__init__`` calls ``ensure_dir`` on the sessions dir, so
    even constructing a manager — let alone saving — would trip the snapshot.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    before = {p for p in tmp_path.rglob("*")}
    await session_create({})
    after = {p for p in tmp_path.rglob("*")}

    assert after == before, f"lazy mint must not touch the workspace; new paths: {after - before}"


async def test_session_create_accepts_title_param_without_error() -> None:
    """Accepting an optional title must not crash (title storage is a later task)."""
    result = await session_create({"title": "My new chat"})
    assert _SESSION_ID_RE.match(result["session_id"]), result["session_id"]


# ---------------------------------------------------------------------------
# P2-B new tests: session.resume with real transcript + session.close flush
# ---------------------------------------------------------------------------


def _write_session(tmp_path: Path, session_key: str, messages: list[dict]) -> None:
    """Write a minimal JSONL session file with the given messages."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    for msg in messages:
        session.add_message(msg["role"], msg["content"])
    mgr.save(session)


async def test_session_resume_loads_n_stored_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resume of a session with 3 stored messages returns exactly 3 wire messages."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_aabbcc"
    stored = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "how are you"},
    ]
    _write_session(tmp_path, session_key, stored)

    result = await session_resume({"session_id": session_key})

    assert set(result) == {"session_id", "info", "messages"}
    assert result["session_id"] == session_key
    _assert_session_info(result["info"])

    msgs = result["messages"]
    assert len(msgs) == 3, f"expected 3 wire messages, got {len(msgs)}: {msgs}"
    assert msgs[0]["role"] == "user"
    assert msgs[0]["text"] == "hello"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["text"] == "hi there"
    assert msgs[2]["role"] == "user"
    assert msgs[2]["text"] == "how are you"


def _loop_with_resolver(tmp_path: Path, *, explicit: Path | None = None) -> SimpleNamespace:
    """A stand-in loop that answers ``peek_session_workdir`` the way the real one
    does: through a ``WorkdirResolver`` built the way ``raven serve`` builds it."""
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path / "home",
        sessions=SessionManager(tmp_path),
        **({"explicit_workdir": explicit} if explicit else {}),
    )
    return SimpleNamespace(
        peek_session_workdir=lambda key: resolver.resolve(key, create=False),
        # The rest of what the init bundle reads off a loop. Present so the
        # bundle builds; this stand-in is about one question only.
        tools=SimpleNamespace(tool_names=[]),
        context=SimpleNamespace(skills=SimpleNamespace(list_skills=lambda **_: [])),
        model=None,
        strategies=SimpleNamespace(get=lambda _name: None),
    )


async def test_session_resume_reports_where_the_session_actually_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session pinned to its own directory resumes reporting THAT as ``info.cwd``.

    The served page shortens every path it shows against this value, so the
    launch directory is the wrong answer for a session started elsewhere.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    pinned = tmp_path / "elsewhere"
    pinned.mkdir()
    session_key = "tui:20260610_143052_workdir"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    session.add_message("user", "hello")
    session.metadata["workdir"] = str(pinned)
    mgr.save(session)

    result = await session_resume({"session_id": session_key}, agent_loop_factory=lambda: _loop_with_resolver(tmp_path))

    assert result["info"]["cwd"] == str(pinned)


async def test_session_resume_reports_the_policy_default_not_the_launch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway's own case: nothing pinned, and its turns still do not run here.

    ``raven serve`` resolves per channel, so a ``tui:`` session lands under the
    channel root -- a sibling of agent home. Reading the session's stored
    ``workdir`` alone answered ``os.getcwd()`` for every such session, which is
    the case the page hits most.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_policy"
    _write_session(tmp_path, session_key, [{"role": "user", "content": "hello"}])

    result = await session_resume({"session_id": session_key}, agent_loop_factory=lambda: _loop_with_resolver(tmp_path))

    expected = default_channel_root(tmp_path / "home") / "tui"
    assert result["info"]["cwd"] == str(expected)
    assert result["info"]["cwd"] != os.getcwd()


async def test_session_resume_lets_an_explicit_workdir_outrank_the_stored_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-w`` beats the persisted override, because that is what the resolver does.

    Reading the metadata directly reported a directory the session's turns were
    not using -- a new wrong answer rather than a missing one.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    stored = tmp_path / "stored"
    stored.mkdir()
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    session_key = "tui:20260610_143052_explicit"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    session.add_message("user", "hello")
    session.metadata["workdir"] = str(stored)
    mgr.save(session)

    result = await session_resume(
        {"session_id": session_key},
        agent_loop_factory=lambda: _loop_with_resolver(tmp_path, explicit=explicit),
    )

    assert result["info"]["cwd"] == str(explicit)


async def test_session_create_reports_where_the_new_session_will_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new session's ``cwd`` is its own directory, not the gateway's."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    result = await session_create({}, agent_loop_factory=lambda: _loop_with_resolver(tmp_path))

    expected = default_channel_root(tmp_path / "home") / "tui"
    assert result["info"]["cwd"] == str(expected)


async def test_session_resume_keeps_the_launch_dir_with_no_loop_to_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No loop, no resolver: the answer stays this process's own directory."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_noloop"
    _write_session(tmp_path, session_key, [{"role": "user", "content": "hello"}])

    result = await session_resume({"session_id": session_key})

    assert result["info"]["cwd"] == os.getcwd()


async def test_session_resume_joins_text_blocks_of_list_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multimodal user messages store LIST content (agent loop history sync).

    The wire ``text`` must join the ``text`` fields of ``type == "text"`` dict
    blocks (non-text blocks skipped) — not render the Python list repr.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_ffeedd"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    session.add_message(
        "user",
        [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxxx"}},
            {"type": "text", "text": "what is it?"},
        ],
    )
    mgr.save(session)

    result = await session_resume({"session_id": session_key})

    msgs = result["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["text"] == "look at this what is it?"
    assert "[{" not in msgs[0]["text"], "list repr must never leak into the wire text"


async def test_session_resume_maps_tool_role_with_name_and_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored role="tool" message passes through with name/context preserved.

    Known degradation: the TS renderer (``domain/messages.ts:54``) collapses
    tool rows into a generic tool trail line attached to the next assistant
    message — full tool output is not re-rendered on resume.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_112233"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    session.add_message("user", "run the tool")
    session.add_message("tool", "tool output here", name="exec", context="ls -la")
    session.add_message("assistant", "done")
    mgr.save(session)

    result = await session_resume({"session_id": session_key})

    msgs = result["messages"]
    assert len(msgs) == 3
    tool_msg = msgs[1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["text"] == "tool output here"
    assert tool_msg["name"] == "exec"
    assert tool_msg["context"] == "ls -la"


async def test_session_resume_skips_malformed_stored_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A roleless message line mid-transcript is skipped, not a -32603 crash.

    One corrupt line must never permanently brick resume for that id
    ("session bootstrap failed" panel) — the good messages still come back.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_path = tmp_path / "sessions" / "tui" / "20260610_143052_445566.jsonl"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(
        json.dumps({"role": "user", "content": "hello"})
        + "\n"
        + json.dumps({"foo": "bar"})
        + "\n"
        + json.dumps({"role": "assistant", "content": "hi"})
        + "\n"
    )

    result = await session_resume({"session_id": "tui:20260610_143052_445566"})

    assert result["session_id"] == "tui:20260610_143052_445566"
    msgs = result["messages"]
    assert len(msgs) == 2, f"malformed line must be skipped, got: {msgs}"
    assert msgs[0]["text"] == "hello"
    assert msgs[1]["text"] == "hi"


async def test_session_resume_corrupt_load_falls_back_to_fresh_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load path that raises must degrade to the fresh-mint response, not crash."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    class _BoomManager:
        _cache: dict = {}

        def _load(self, key: str):
            raise RuntimeError("corrupt session store")

    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: _BoomManager())

    result = await session_resume({"session_id": "tui:20260610_143052_998877"})

    assert set(result) == {"session_id", "info", "messages"}
    assert _SESSION_ID_RE.match(result["session_id"]), result["session_id"]
    _assert_session_info(result["info"])
    assert result["messages"] == []


async def test_session_resume_prefers_live_cache_with_unflushed_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume reads the live manager cache first — a mid-turn unflushed tail
    message must appear in the transcript even though disk lags behind."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_778899"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    session.add_message("user", "persisted message")
    mgr.save(session)
    session.add_message("assistant", "unflushed tail")

    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_resume({"session_id": session_key})

    msgs = result["messages"]
    assert len(msgs) == 2, f"cached unflushed tail must be visible, got: {msgs}"
    assert msgs[1]["text"] == "unflushed tail"


async def test_session_resume_unknown_id_does_not_create_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resuming an unknown session_id does not write any file to the workspace."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    sessions_dir = tmp_path / "sessions"
    before = set(sessions_dir.rglob("*")) if sessions_dir.exists() else set()

    await session_resume({"session_id": "tui:totally_unknown_id"})

    after = set(sessions_dir.rglob("*")) if sessions_dir.exists() else set()
    # Manager construction ensure_dirs the sessions dir, so directories are
    # expected; only files indicate actual persistence.
    new_files = {p for p in after - before if p.is_file()}
    assert not new_files, f"resume of unknown id must not write files; got: {new_files}"


async def test_session_close_flushes_dirty_cached_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """close flushes a dirty cached session — message added after last save lands on disk."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_ccddee"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    session.add_message("user", "first message")
    mgr.save(session)

    session.add_message("assistant", "second message")
    assert session._persisted_count == 1
    assert len(session.messages) == 2

    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_close({"session_id": session_key})
    assert result == {"ok": True}

    session_path = tmp_path / "sessions" / "tui" / "20260610_143052_ccddee.jsonl"
    assert session_path.exists()
    lines = [json.loads(ln) for ln in session_path.read_text().splitlines() if ln.strip()]
    msg_lines = [ln for ln in lines if ln.get("_type") != "metadata"]
    assert len(msg_lines) == 2, f"expected 2 messages on disk after flush, got {msg_lines}"


async def test_session_close_save_failure_still_returns_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A save that raises during close degrades to a warning, not a -32603."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_boom01"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    session.add_message("user", "dirty message")

    def _boom_save(s) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mgr, "save", _boom_save)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_close({"session_id": session_key})
    assert result == {"ok": True}


async def test_session_close_returns_ok_for_unknown_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """close with an unknown/absent session_key must not raise and returns ok."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    result = await session_close({"session_id": "tui:does_not_exist"})
    assert result == {"ok": True}


async def test_session_close_returns_ok_without_session_key() -> None:
    """close with no params must not raise (legacy call path)."""
    result = await session_close({})
    assert result == {"ok": True}


# ---------------------------------------------------------------------------
# P2-C new tests: session.list / session.delete / session.most_recent / session.title
# ---------------------------------------------------------------------------


async def test_session_list_returns_sessions_for_tui_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.list returns a sessions list from the tui channel, sorted by updated_at desc."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    for key in ("tui:20260610_100000_aaa111", "tui:20260610_110000_bbb222"):
        s = mgr.get_or_create(key)
        s.add_message("user", "hello")
        mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_list({})
    assert "sessions" in result
    items = result["sessions"]
    assert len(items) == 2
    ids = [item["id"] for item in items]
    assert "tui:20260610_100000_aaa111" in ids
    assert "tui:20260610_110000_bbb222" in ids


async def test_session_list_sorted_by_updated_at_desc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.list returns sessions ordered by updated_at descending."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    older = mgr.get_or_create("tui:20260610_090000_old111")
    older.add_message("user", "old")
    mgr.save(older)

    newer = mgr.get_or_create("tui:20260610_120000_new222")
    newer.add_message("user", "new")
    mgr.save(newer)

    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_list({})
    items = result["sessions"]
    assert items[0]["id"] == "tui:20260610_120000_new222"
    assert items[1]["id"] == "tui:20260610_090000_old111"


async def test_session_list_item_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each session.list item carries id, message_count, preview, started_at, title."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("tui:20260610_100000_shape1")
    s.add_message("user", "first user message")
    s.add_message("assistant", "reply")
    mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_list({})
    item = result["sessions"][0]
    assert "id" in item
    assert "message_count" in item
    assert "preview" in item
    assert "started_at" in item
    assert "title" in item
    assert item["message_count"] == 2
    assert isinstance(item["started_at"], (int, float))


async def test_session_list_only_tui_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.list does not include sessions from non-tui channels."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    tui_session = mgr.get_or_create("tui:20260610_100000_tui01")
    tui_session.add_message("user", "tui msg")
    mgr.save(tui_session)
    cli_session = mgr.get_or_create("cli:20260610_100000_cli01")
    cli_session.add_message("user", "cli msg")
    mgr.save(cli_session)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_list({})
    ids = [item["id"] for item in result["sessions"]]
    assert all(i.startswith("tui:") for i in ids)
    assert "cli:20260610_100000_cli01" not in ids


async def test_session_list_honors_limit_after_sort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """limit slices AFTER the updated_at-desc sort — newest sessions win."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    for chat_id in ("20260610_090000_lim001", "20260610_100000_lim002", "20260610_110000_lim003"):
        s = mgr.get_or_create(f"tui:{chat_id}")
        s.add_message("user", "x")
        mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_list({"limit": 2})
    items = result["sessions"]
    assert len(items) == 2
    assert items[0]["id"] == "tui:20260610_110000_lim003"
    assert items[1]["id"] == "tui:20260610_100000_lim002"


async def test_session_list_ignores_non_positive_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing, zero, or invalid limit returns all sessions."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    for chat_id in ("20260610_090000_nl0001", "20260610_100000_nl0002"):
        s = mgr.get_or_create(f"tui:{chat_id}")
        s.add_message("user", "x")
        mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    for params in ({}, {"limit": 0}, {"limit": -1}, {"limit": "bogus"}):
        result = await session_list(params)
        assert len(result["sessions"]) == 2, f"params={params}"


async def test_session_list_empty_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.list returns an empty list when no tui sessions exist."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_list({})
    assert result == {"sessions": []}


async def test_session_delete_removes_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.delete returns {deleted: session_id} and removes the file."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("tui:20260610_100000_rm01")
    s.add_message("user", "bye")
    mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_delete({"session_id": "tui:20260610_100000_rm01"})
    assert result == {"deleted": "tui:20260610_100000_rm01"}
    path = tmp_path / "sessions" / "tui" / "20260610_100000_rm01.jsonl"
    assert not path.exists()


async def test_session_delete_unknown_key_returns_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.delete on a missing session returns {deleted: null} so the UI
    can distinguish a typo from a real removal."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_delete({"session_id": "tui:ghost_session"})
    assert result == {"deleted": None}


async def test_session_delete_missing_param_returns_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.delete with no session_id returns {deleted: null}."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_delete({})
    assert result == {"deleted": None}


async def test_session_most_recent_returns_session_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.most_recent returns the full session_key for the newest tui session."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("tui:20260610_100000_recent1")
    s.add_message("user", "hi")
    mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_most_recent({})
    assert result["session_id"] == "tui:20260610_100000_recent1"


async def test_session_most_recent_returns_null_when_no_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """session.most_recent returns session_id=null when no tui sessions exist."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_most_recent({})
    assert result["session_id"] is None


async def test_session_title_on_existing_file_persists_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting a title on a session with an on-disk file saves it right away.

    Durability contract: the title must survive a fresh-manager reload,
    and the response carries pending=False.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("tui:20260610_100000_title1")
    s.add_message("user", "hello")
    mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_title({"session_id": "tui:20260610_100000_title1", "title": "My Chat"})
    assert result["title"] == "My Chat"
    assert result["pending"] is False

    reloaded = SessionManager(tmp_path).peek("tui:20260610_100000_title1")
    assert reloaded is not None
    assert reloaded.metadata.get("title") == "My Chat"


async def test_session_title_on_fresh_session_is_pending_and_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting a title on a never-saved session keeps it in memory only.

    pending=True signals deferred persistence; lazy mint is preserved
    (no file appears until the session's first save).
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_title({"session_id": "tui:20260610_100000_lazy01", "title": "Early"})
    assert result["title"] == "Early"
    assert result["pending"] is True

    sessions_dir = tmp_path / "sessions"
    files = [p for p in sessions_dir.rglob("*") if p.is_file()] if sessions_dir.exists() else []
    assert not files, f"lazy title must not write files; got: {files}"

    assert mgr.get_or_create("tui:20260610_100000_lazy01").metadata.get("title") == "Early"


async def test_session_title_missing_session_id_returns_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A falsy session_id short-circuits — no manager construction, no cache pollution."""

    def _boom(cfg):
        raise AssertionError("manager must not be built for a falsy session_id")

    monkeypatch.setattr(session_module, "_get_or_build_manager", _boom)

    result = await session_title({})
    assert result == {"title": None, "session_key": "", "pending": False}


async def test_session_title_get_returns_current_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.title with no title param returns the current title from metadata."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("tui:20260610_100000_title2")
    s.metadata["title"] = "Existing Title"
    s.add_message("user", "hello")
    mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_title({"session_id": "tui:20260610_100000_title2"})
    assert result["title"] == "Existing Title"


async def test_session_list_via_dispatcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.list is reachable through the Dispatcher."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    d = Dispatcher()
    register_session_methods(d)
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "session.list", "params": {}})
    assert "error" not in resp, f"session.list dispatch failed: {resp}"
    assert "sessions" in resp["result"]


async def test_session_delete_via_dispatcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.delete is reachable through the Dispatcher."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("tui:20260610_100000_disp1")
    s.add_message("user", "x")
    mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    d = Dispatcher()
    register_session_methods(d)
    resp = await d.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "session.delete", "params": {"session_id": "tui:20260610_100000_disp1"}}
    )
    assert "error" not in resp, f"session.delete dispatch failed: {resp}"
    assert resp["result"]["deleted"] == "tui:20260610_100000_disp1"


async def test_session_most_recent_via_dispatcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.most_recent is reachable through the Dispatcher."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    d = Dispatcher()
    register_session_methods(d)
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "session.most_recent", "params": {}})
    assert "error" not in resp, f"session.most_recent dispatch failed: {resp}"
    assert "session_id" in resp["result"]


# ---------------------------------------------------------------------------
# manager_for: shared-loop preference vs fresh-manager fall-through
# ---------------------------------------------------------------------------


def test_manager_for_reuses_shared_loop_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When agent_loop.sessions IS a SessionManager, manager_for returns that
    exact instance — the shared loop manager is reused, not rebuilt."""
    from types import SimpleNamespace

    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    shared = SessionManager(tmp_path)
    loop = SimpleNamespace(sessions=shared)

    def _boom(_cfg):
        raise AssertionError("must not build a fresh manager when the loop has one")

    monkeypatch.setattr(session_module, "_get_or_build_manager", _boom)

    assert session_module.manager_for(loop, cfg) is shared


def test_manager_for_falls_through_when_no_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """agent_loop=None falls through to a freshly built manager."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    sentinel = SessionManager(tmp_path)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda _cfg: sentinel)

    assert session_module.manager_for(None, cfg) is sentinel


def test_manager_for_falls_through_when_loop_sessions_not_a_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loop whose .sessions is not a SessionManager falls through to a fresh one."""
    from types import SimpleNamespace

    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    sentinel = SessionManager(tmp_path)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda _cfg: sentinel)
    loop = SimpleNamespace(sessions=None)

    assert session_module.manager_for(loop, cfg) is sentinel


def test_is_turn_active_reflects_active_turns(monkeypatch):
    import asyncio

    from raven.rpc.methods import turn as turn_module

    assert turn_module.is_turn_active("tui:none") is False

    async def _noop():
        await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    task = loop.create_task(_noop())
    monkeypatch.setitem(turn_module._active_turns, "tui:busy", task)
    try:
        assert turn_module.is_turn_active("tui:busy") is True
    finally:
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        loop.close()


def _seed_manager(tmp_path, key="tui:s1"):
    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create(key)
    s.record({"role": "user", "content": "q1"})
    s.record({"role": "assistant", "content": "a1"})
    s.record({"role": "user", "content": "q2"})
    s.record({"role": "assistant", "content": "a2"})
    mgr.save(s)
    return mgr, key


class _LoopWithManager:
    def __init__(self, mgr):
        self.sessions = mgr


async def test_session_clear_keeps_id_and_wipes(tmp_path):
    from raven.rpc.methods.session import session_clear

    mgr, key = _seed_manager(tmp_path)
    result = await session_clear({"session_id": key}, agent_loop_factory=lambda: _LoopWithManager(mgr))
    assert result == {"session_id": key, "cleared": True}
    assert mgr.get_or_create(key).messages == []
    assert mgr.peek(key).messages == []


async def test_session_clear_rejects_when_turn_active(tmp_path, monkeypatch):
    from raven.rpc.methods.session import session_clear

    mgr, key = _seed_manager(tmp_path)
    monkeypatch.setattr(turn_module, "is_session_busy", lambda k: k == key)
    with pytest.raises(TurnInProgressError):
        await session_clear({"session_id": key}, agent_loop_factory=lambda: _LoopWithManager(mgr))


async def test_session_undo_drops_last_turn(tmp_path):
    from raven.rpc.methods.session import session_undo

    mgr, key = _seed_manager(tmp_path)
    result = await session_undo({"session_id": key}, agent_loop_factory=lambda: _LoopWithManager(mgr))
    assert result == {"removed": 2}
    assert [m["content"] for m in mgr.get_or_create(key).messages] == ["q1", "a1"]


async def test_session_undo_nothing_to_undo_returns_zero(tmp_path):
    from raven.rpc.methods.session import session_undo

    mgr = SessionManager(tmp_path)
    mgr.get_or_create("tui:empty")
    result = await session_undo({"session_id": "tui:empty"}, agent_loop_factory=lambda: _LoopWithManager(mgr))
    assert result == {"removed": 0}


async def test_session_undo_rejects_when_turn_active(tmp_path, monkeypatch):
    from raven.rpc.methods.session import session_undo

    mgr, key = _seed_manager(tmp_path)
    monkeypatch.setattr(turn_module, "is_session_busy", lambda k: True)
    with pytest.raises(TurnInProgressError):
        await session_undo({"session_id": key}, agent_loop_factory=lambda: _LoopWithManager(mgr))


# ---------------------------------------------------------------------------
# session.branch — fork the session
# ---------------------------------------------------------------------------


async def test_session_branch_forks_and_returns_child_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)
    src_key = "tui:20260610_143052_aabbcc"
    _write_session(tmp_path, src_key, [{"role": "user", "content": "hi"}])

    result = await session_branch({"session_id": src_key})

    child_key = result["session_id"]
    assert child_key and child_key.startswith("tui:") and child_key != src_key
    child = SessionManager(tmp_path).get_or_create(child_key)
    assert child.metadata["parent_session_id"] == src_key
    assert [m["content"] for m in child.messages] == ["hi"]


async def test_session_branch_returns_carried_message_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)
    src_key = "tui:20260610_143052_ddeeff"
    _write_session(
        tmp_path,
        src_key,
        [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ],
    )

    result = await session_branch({"session_id": src_key})

    assert result["message_count"] == 3


async def test_session_branch_uses_name_as_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)
    src_key = "tui:20260610_143052_bbccdd"
    _write_session(tmp_path, src_key, [{"role": "user", "content": "hi"}])

    result = await session_branch({"session_id": src_key, "name": "Experiment"})

    assert result["title"] == "Experiment"
    child = SessionManager(tmp_path).get_or_create(result["session_id"])
    assert child.metadata["title"] == "Experiment"


async def test_session_branch_empty_name_defaults_fork_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)
    src_key = "tui:20260610_143052_ccddee"
    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create(src_key)
    s.add_message("user", "hi")
    s.metadata["title"] = "Chat"
    mgr.save(s)

    result = await session_branch({"session_id": src_key, "name": ""})

    assert result["title"] == "Chat (fork)"


async def test_session_branch_unknown_session_returns_no_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    result = await session_branch({"session_id": "tui:nonexistent_id"})

    assert not result.get("session_id")


def test_session_branch_no_duplicate_registration() -> None:
    """session.branch is a real handler, removed from the stub table, so
    registering both groups on one dispatcher does not raise."""
    from raven.rpc.methods._stubs import (
        HERMES_ONLY_STUB_METHODS,
        register_stub_methods,
    )

    d = Dispatcher()
    register_session_methods(d)
    register_stub_methods(d)

    assert "session.branch" in d.methods()
    assert "session.branch" not in HERMES_ONLY_STUB_METHODS


# ── session.export ─────────────────────────────────────────────────────


async def test_session_export_writes_markdown_for_existing_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exporting a known session writes a Markdown file and returns its path."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260622_120000_abcdef"
    _write_session(
        tmp_path,
        session_key,
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
    )

    result = await session_export({"session_id": session_key})

    assert result["exported"] is True
    assert result["path"]
    written = Path(result["path"])
    assert written.exists()
    body = written.read_text(encoding="utf-8")
    assert "hello" in body and "hi there" in body
    assert written.parent == (tmp_path / "exports")


async def test_session_export_unknown_id_returns_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable id yields not_found and writes nothing."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    result = await session_export({"session_id": "nope000"})

    assert result == {"exported": False, "path": None, "reason": "not_found"}
    assert not (tmp_path / "exports").exists()


async def test_session_export_ambiguous_returns_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare id on two channels is ambiguous; both keys surface, nothing written."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    cid = "20260622_120000_dddddd"
    _write_session(tmp_path, f"cli:{cid}", [{"role": "user", "content": "a"}])
    _write_session(tmp_path, f"tui:{cid}", [{"role": "user", "content": "b"}])

    result = await session_export({"session_id": cid})

    assert result["exported"] is False
    assert result["reason"] == "ambiguous"
    assert set(result["candidates"]) == {f"cli:{cid}", f"tui:{cid}"}
    assert result["path"] is None


async def test_session_export_empty_session_id_returns_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent/empty session_id has nothing to export."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    result = await session_export({})

    assert result == {"exported": False, "path": None, "reason": "not_found"}


async def test_session_export_is_read_only_during_active_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Export does not reject on an active turn (read-only, unlike clear/undo)."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)
    monkeypatch.setattr(turn_module, "is_session_busy", lambda key: True)

    session_key = "tui:20260622_120000_eeeeee"
    _write_session(tmp_path, session_key, [{"role": "user", "content": "hi"}])

    result = await session_export({"session_id": session_key})

    assert result["exported"] is True


async def test_session_info_reports_this_sessions_model_not_the_default(monkeypatch) -> None:
    """The picker and the status bar sit next to each other; reporting the
    global default here would show two models for one conversation.
    """
    from unittest.mock import MagicMock

    import raven.rpc.methods.session as session_mod

    # MagicMock so the unrelated skills/tools enumeration in the bundle works;
    # only ``session_model`` is under test.
    loop = MagicMock()
    loop.model = "vendor-a/model"  # the usage baseline resolves a window from it
    loop.session_model = lambda key: "vendor-a/model" if key == "tui:a" else "boot/model"
    info = await session_mod._default_session_info(loop, session_mod.load_config(), "tui:a")

    assert info["model"] == "vendor-a/model"
    assert info["model_id"] == "vendor-a/model"


async def test_session_info_without_a_session_reports_the_default() -> None:
    """A session being created has no model of its own yet; the default is the
    right answer, because that is what it will start on.
    """
    from unittest.mock import MagicMock

    import raven.rpc.methods.session as session_mod

    config = session_mod.load_config()
    loop = MagicMock()
    loop.model = "vendor-a/model"  # the usage baseline resolves a window from it
    loop.session_model = lambda key: "vendor-a/model"
    info = await session_mod._default_session_info(loop, config, None)

    assert info["model"] == config.agents.defaults.model


async def test_session_resume_reports_the_model_the_loop_restored(tmp_path) -> None:
    """The handler no longer restores anything -- the loop reads the stored model
    on first ask, which is what makes the choice survive on every surface and not
    only on the one that calls this handler. What the handler still owes is
    passing the session key down, so the bundle reports *this* session's model
    instead of the configured default.
    """
    from raven.rpc.methods.session import session_resume
    from raven.session.manager import SessionManager

    sessions = SessionManager(tmp_path)
    record = sessions.get_or_create("tui:a")
    record.metadata["model"] = "vendor-a/model"
    sessions.save(record)

    from unittest.mock import MagicMock

    asked: list[str] = []
    loop = MagicMock()
    loop.sessions = sessions
    # A real id, not the MagicMock default: the init bundle resolves a window
    # from whatever the loop reports as its model.
    loop.model = "vendor-a/model"
    loop.session_model = lambda key: (asked.append(key), "vendor-a/model")[1]

    result = await session_resume({"session_id": "tui:a"}, agent_loop_factory=lambda: loop)

    assert asked == ["tui:a"], "the handler stopped passing the session key down"
    assert result["info"]["model"] == "vendor-a/model"


async def test_session_branch_carries_the_parents_model_to_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork continues its parent's conversation on its parent's model, and
    keeps it across a restart.

    Both halves matter and fail independently: dropping the binding hand-off
    leaves the child running on the default in this process, and dropping the
    record leaves it running on the default in the next one.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)
    src_key = "tui:20260610_143052_bb0001"
    _write_session(tmp_path, src_key, [{"role": "user", "content": "hi"}])

    sessions = SessionManager(tmp_path)
    parent = sessions.get_or_create(src_key)
    parent.metadata["model"] = "vendor-a/model"
    parent.metadata["provider"] = "anthropic"
    sessions.save(parent)

    parent_binding = SimpleNamespace(provider="prov-a", model="vendor-a/model")

    class _Loop:
        def __init__(self) -> None:
            self.sessions = sessions
            self.bindings: dict[str, object] = {src_key: parent_binding}

        def has_session_binding(self, key: str) -> bool:
            return key in self.bindings

        def binding_for_session(self, key: str) -> object:
            return self.bindings.get(key, SimpleNamespace(provider="boot", model="boot/model"))

        def set_session_binding(self, key: str, binding: object) -> None:
            self.bindings[key] = binding

    loop = _Loop()
    result = await session_branch({"session_id": src_key}, agent_loop_factory=lambda: loop)

    child_key = result["session_id"]
    assert loop.bindings[child_key] is parent_binding, "the fork must run on its parent's model now"

    reloaded = SessionManager(tmp_path).get_or_create(child_key)
    assert reloaded.metadata["model"] == "vendor-a/model", "and after a restart"
    assert reloaded.metadata["provider"] == "anthropic"


async def test_session_delete_releases_the_sessions_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted session must not leave its override -- and the live provider
    behind it -- held for the life of the process.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)
    key = "tui:20260610_100000_bb0002"
    _write_session(tmp_path, key, [{"role": "user", "content": "hi"}])

    cleared: list[str] = []

    class _Loop:
        sessions = None

        def clear_session_binding(self, session_key: str) -> None:
            cleared.append(session_key)

    await session_delete({"session_id": key}, agent_loop_factory=lambda: _Loop())

    assert cleared == [key]


async def test_session_resume_estimates_only_what_the_next_call_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The context meter counts the unconsolidated tail, not the whole file.

    The runtime consolidates on every user-inbound turn and never removes the
    archived messages from the transcript, so estimating over everything stored
    reported the size of a context that had already been archived -- a session a
    quarter full resumed showing a meter pegged at 100%.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_consol"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    for i in range(4):
        session.add_message("user", f"message number {i} " * 40)
    session.last_consolidated = 3
    mgr.save(session)

    result = await session_resume({"session_id": session_key})
    used = result["info"]["usage"]["context_used"]

    # The floor is what the tail alone costs; the ceiling keeps this from
    # passing on a version that counts everything -- four near-identical
    # messages make the whole file roughly four times the tail.
    tail_only = estimate_prompt_tokens(session.get_history())
    assert used == tail_only, "the estimate must be over what get_history() returns"
    whole_file = estimate_prompt_tokens(
        [{"role": m["role"], "content": m.get("content", "")} for m in session.messages]
    )
    assert used < whole_file / 2, f"{used} looks like the whole transcript ({whole_file}), not the tail"

    # The transcript itself is unchanged: resume still hands back everything on
    # disk, because that is what the caller draws.
    assert len(result["messages"]) == 4


async def test_session_resume_estimates_past_the_five_hundred_message_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tail is bounded in tokens, not in messages, so it can run past 500.

    `get_history()` defaults to the last 500; the two places that build or
    measure the real prompt both pass `max_messages=0`. Taking the default here
    under-reported a long unconsolidated session -- and it does not converge,
    because the reported number pins at whatever the newest 500 cost while the
    real prompt keeps growing toward the window.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_long01"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    for i in range(600):
        session.add_message("user", f"message number {i} " * 10)
    mgr.save(session)

    result = await session_resume({"session_id": session_key})
    used = result["info"]["usage"]["context_used"]

    whole_tail = estimate_prompt_tokens(session.get_history(max_messages=0))
    newest_500 = estimate_prompt_tokens(session.get_history())
    assert newest_500 < whole_tail, "fixture must exceed the default cap for this to mean anything"
    assert used == whole_tail, f"{used} is the newest 500 ({newest_500}), not the whole tail ({whole_tail})"


async def test_session_resume_carries_a_stored_tool_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stored diff is the one record with real line numbers -- the wire
    must carry it or a reloaded page can never renumber a change."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_445566"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    session.add_message("user", "edit it")
    session.add_message("tool", "Successfully edited", tool_call_id="c1", name="edit_file", diff="@@ -1 +1 @@\n-a\n+b")
    mgr.save(session)

    msgs = (await session_resume({"session_id": session_key}))["messages"]
    assert msgs[1]["diff"].startswith("@@ -1 +1 @@")


async def test_session_resume_carries_the_broken_turn_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``turn_ended`` says why a transcript stops where it does; without it a
    reloaded page renders the marker's model-facing text as an answer."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_778899"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    session.add_message("user", "do it")
    session.add_message("assistant", "(turn cancelled by the user)", turn_ended={"status": "cancelled"})
    mgr.save(session)

    msgs = (await session_resume({"session_id": session_key}))["messages"]
    assert msgs[1]["turn_ended"] == {"status": "cancelled"}


async def test_session_resume_carries_the_runtime_notice_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime prose is stored as an assistant message because the MODEL has to
    read it on the next turn. A reader must not: without ``notice`` on the wire
    the reload attributes it to the model, contradicting the live view, which
    drew it as its own row."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_991122"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    session.add_message("user", "clean up")
    session.add_message(
        "assistant",
        "The operation was not completed",
        notice={"kind": "action_blocked", "detail": "Error: Command blocked by safety guard"},
    )
    mgr.save(session)

    msgs = (await session_resume({"session_id": session_key}))["messages"]
    assert msgs[1]["notice"]["kind"] == "action_blocked"
    assert "safety guard" in msgs[1]["notice"]["detail"]


def test_resume_marks_missing_deliveries_without_dropping_metadata(tmp_path: Path) -> None:
    present = tmp_path / "report.pdf"
    present.write_bytes(b"pdf")
    messages = [
        {
            "role": "tool",
            "content": "Delivered files",
            "metadata": {
                "raven_delivery": {
                    "message": "Final files",
                    "files": [
                        {"name": "report.pdf", "path": str(present), "size": 3},
                        {"name": "gone.csv", "path": str(tmp_path / "gone.csv"), "size": 8},
                    ],
                },
                "other": "kept",
            },
        }
    ]

    wire = session_module._map_to_wire(messages, "tui:s1")

    metadata = wire[0]["metadata"]
    assert metadata["other"] == "kept"
    assert [item["missing"] for item in metadata["raven_delivery"]["files"]] == [False, True]


async def test_session_list_sorted_by_latest_conversation_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """session.list returns sessions ordered by latest readable message."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    older = mgr.get_or_create("tui:20260610_090000_old111")
    older.add_message("user", "old")
    mgr.save(older)

    newer = mgr.get_or_create("tui:20260610_120000_new222")
    newer.add_message("user", "new")
    mgr.save(newer)

    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_list({})
    items = result["sessions"]
    assert items[0]["id"] == "tui:20260610_120000_new222"
    assert items[1]["id"] == "tui:20260610_090000_old111"


async def test_session_list_contract_accepts_real_multichannel_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared contract includes the fields the browser actually sends
    and consumes, including a populated nested row."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("cron:contract")
    session.add_message("user", "run the digest")
    session.add_message("assistant", "digest complete")
    mgr.save(session)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    params_model, result_model = METHOD_MODELS["session.list"]
    params_model.model_validate({"channels": ["tui", "cron"], "limit": 10})
    result_model.model_validate(await session_list({"channels": ["tui", "cron"]}))


async def test_session_list_scans_once_for_multiple_channels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    mgr = SessionManager(tmp_path)
    for key in ("tui:one", "cron:two", "cli:three"):
        session = mgr.get_or_create(key)
        session.add_message("user", key)
        mgr.save(session)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    scans = 0
    scan_file = mgr._scan_file

    def counted(path: Path):
        nonlocal scans
        scans += 1
        return scan_file(path)

    monkeypatch.setattr(mgr, "_scan_file", counted)
    result = await session_list({"channels": ["tui", "cron"]})

    assert {row["source"] for row in result["sessions"]} == {"tui", "cron"}
    assert scans == 3, "every stored file is scanned once, not once per requested channel"


async def test_session_delete_rejects_a_running_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    key = "tui:running"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(key)
    session.add_message("user", "keep working")
    mgr.save(session)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)
    monkeypatch.setattr(turn_module, "is_session_busy", lambda candidate: candidate == key)

    with pytest.raises(TurnInProgressError):
        await session_delete({"session_id": key})

    assert mgr.exists(key), "a running writer must keep its transcript"


async def test_session_most_recent_skips_archived_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session.most_recent does not auto-resume a session hidden by archiving."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    mgr = SessionManager(tmp_path)
    archived = mgr.get_or_create("tui:20260610_110000_archived")
    archived.add_message("user", "hide this")
    archived.metadata["archived"] = True
    mgr.save(archived)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_most_recent({})
    assert result["session_id"] is None


async def test_session_pin_persists_and_shows_up_in_the_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinning survives a reload and rides the session.list rows.

    The regression this pins: the web UI used to keep the flag in page memory
    only, so every refresh silently dropped the pinned group.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("tui:20260610_100000_pin001")
    s.add_message("user", "hello")
    mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_pin({"session_id": "tui:20260610_100000_pin001", "pinned": True})
    assert result == {"pinned": True, "session_key": "tui:20260610_100000_pin001", "pending": False}

    reloaded = SessionManager(tmp_path).peek("tui:20260610_100000_pin001")
    assert reloaded is not None and reloaded.metadata.get("pinned") is True

    listed = await session_list({})
    row = next(r for r in listed["sessions"] if r["id"] == "tui:20260610_100000_pin001")
    assert row["pinned"] is True

    result = await session_pin({"session_id": "tui:20260610_100000_pin001", "pinned": False})
    assert result["pinned"] is False
    reloaded = SessionManager(tmp_path).peek("tui:20260610_100000_pin001")
    assert reloaded is not None and "pinned" not in reloaded.metadata


async def test_session_archive_persists_and_filters_the_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    mgr = SessionManager(tmp_path)
    session_key = "tui:20260610_100000_archive1"
    session = mgr.get_or_create(session_key)
    session.add_message("user", "hide this session")
    mgr.save(session)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_archive({"session_id": session_key, "archived": True})
    assert result == {"archived": True, "session_key": session_key, "pending": False}
    assert await session_list({}) == {"sessions": []}

    reloaded = SessionManager(tmp_path).peek(session_key)
    assert reloaded is not None and reloaded.metadata.get("archived") is True

    result = await session_archive({"session_id": session_key, "archived": False})
    assert result == {"archived": False, "session_key": session_key, "pending": False}
    assert [row["id"] for row in (await session_list({}))["sessions"]] == [session_key]


async def test_session_archive_via_dispatcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    mgr = SessionManager(tmp_path)
    session_key = "tui:20260610_100000_archive2"
    session = mgr.get_or_create(session_key)
    session.add_message("user", "archive through dispatcher")
    mgr.save(session)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    dispatcher = Dispatcher()
    register_session_methods(dispatcher)
    response = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session.archive",
            "params": {"session_id": session_key, "archived": True},
        }
    )
    assert "error" not in response
    assert response["result"] == {"archived": True, "session_key": session_key, "pending": False}


async def test_a_direct_turn_still_counts_as_the_session_being_busy(monkeypatch):
    """A direct chat runs on a lane of its own, which is what makes it
    concurrent -- but clear / undo / compress / model-switch mean "is anything
    running in this session", and a sub-agent answering is."""
    from raven.rpc.methods import turn as turn_module
    from raven.spine import direct_lane

    turn_module._active_turns.clear()
    try:
        lane = direct_lane("tui:busy", "Coder", "h1")
        turn_module._active_turns[lane] = object()

        # Not the lane the guard is asked about ...
        assert turn_module.is_turn_active("tui:busy") is False
        # ... but the session it belongs to is busy.
        assert turn_module.is_session_busy("tui:busy") is True
        assert turn_module.is_session_busy("tui:other") is False
    finally:
        turn_module._active_turns.clear()


async def test_session_compress_requires_a_session_id() -> None:
    """No session_id is a caller error, not a silent no-op."""
    from raven.rpc.errors import ConfigValidationError

    with pytest.raises(ConfigValidationError) as exc:
        await session_module.session_compress({})

    assert exc.value.data == {"field": "session_id"}


async def test_session_compress_refuses_while_a_turn_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compacting under a live turn would archive messages the turn is still
    reading, so the handler refuses rather than racing it."""
    session_key = "tui:compress_busy"
    monkeypatch.setattr(turn_module, "is_session_busy", lambda key: key == session_key)

    with pytest.raises(TurnInProgressError):
        await session_module.session_compress({"session_id": session_key})


async def test_session_compress_is_a_noop_without_a_consolidator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime whose context engine owns compaction reports noop with a note,
    and leaves the message count untouched."""
    from types import SimpleNamespace

    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:compress_noop"
    _write_session(tmp_path, session_key, [{"role": "user", "content": "hi"}])
    loop = SimpleNamespace(memory_consolidator=None, sessions=SessionManager(tmp_path))

    result = await session_module.session_compress({"session_id": session_key}, agent_loop_factory=lambda: loop)

    assert result["removed"] == 0
    assert result["before_messages"] == result["after_messages"] == 1
    assert result["summary"]["noop"] is True
    assert result["summary"]["note"] == "no memory consolidator in this runtime"


async def test_session_compress_reports_what_the_consolidator_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a consolidator the handler forces a token pass and reports the
    before/after counts the clients print verbatim."""
    from types import SimpleNamespace

    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:compress_real"
    _write_session(
        tmp_path,
        session_key,
        [{"role": "user", "content": f"m{i}"} for i in range(5)],
    )

    forced: list[bool] = []

    class _Consolidator:
        async def maybe_consolidate_by_tokens(self, session, force=False):
            forced.append(force)
            # Reporting an archive and leaving the session untouched is a state
            # the real consolidator cannot produce: `archived` is its report,
            # `last_consolidated` is the fact, and the handler reads the fact.
            session.last_consolidated = 3
            return {"before_tokens": 900, "after_tokens": 300, "compacted": 3}

    # A fake is only evidence if it answers to the same contract as the real
    # thing. This one was written against a signature the shipped consolidator
    # did not have (`force` was added later, on a branch), so the test passed
    # while the handler raised TypeError against the module it actually calls.
    # Binding the real signature is what makes the fake accountable.
    _real = inspect.signature(MemoryConsolidator.maybe_consolidate_by_tokens)
    _real.bind(object(), object(), force=True)
    assert inspect.signature(_Consolidator.maybe_consolidate_by_tokens).parameters.keys() == _real.parameters.keys(), (
        "the fake consolidator has drifted from MemoryConsolidator"
    )

    loop = SimpleNamespace(memory_consolidator=_Consolidator(), sessions=SessionManager(tmp_path))

    result = await session_module.session_compress({"session_id": session_key}, agent_loop_factory=lambda: loop)

    assert forced == [True], "the point of session.compress is forcing the pass early"
    assert result["removed"] == 3
    assert result["before_messages"] == 5
    assert result["after_messages"] == 2
    assert result["summary"]["headline"] == "compacted 3 messages"
    assert result["summary"]["noop"] is False
    assert result["summary"]["token_line"] == "900 -> 300 tokens"


async def test_session_list_reaches_the_channels_it_was_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler used to hardcode channel="tui" and ignore ``channels``, so
    scheduled runs -- which live under ``cron:`` -- were unreachable even though
    they were on disk. Nothing had ever passed the parameter."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    mgr = SessionManager(tmp_path)
    for key in ("tui:20260610_100000_aaa111", "cron:20260610_110000_bbb222"):
        s = mgr.get_or_create(key)
        s.add_message("user", "hello")
        mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    both = await session_list({"channels": ["tui", "cron"]})
    sources = {item["source"] for item in both["sessions"]}
    assert sources == {"tui", "cron"}, "session.list must return every channel it was given"

    # Asking for one non-default channel alone: the bug only shows when the
    # caller wants something other than the default, and "both" would still
    # pass against a handler that ignored the parameter and returned everything.
    cron_only = await session_list({"channels": ["cron"]})
    assert [item["source"] for item in cron_only["sessions"]] == ["cron"]

    default = await session_list({})
    assert {item["source"] for item in default["sessions"]} == {"tui"}, (
        "omitting channels must keep the old tui-only behaviour for existing callers"
    )


async def test_session_list_orders_by_latest_conversational_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An assistant reply is new Session activity and moves its row."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    mgr = SessionManager(tmp_path)
    older_user = mgr.get_or_create("tui:20260610_100000_older1")
    older_user.add_message("user", "spoke first", timestamp="2026-06-10T10:00:00")
    mgr.save(older_user)

    recent_user = mgr.get_or_create("tui:20260610_110000_recent")
    recent_user.add_message("user", "spoke second", timestamp="2026-06-10T11:00:00")
    mgr.save(recent_user)

    older_user.add_message("assistant", "finished later", timestamp="2026-06-10T12:00:00")
    mgr.save(older_user)

    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)
    result = await session_list({})
    assert [item["id"] for item in result["sessions"]][0] == "tui:20260610_100000_older1"


async def test_session_list_counts_runtime_origin_user_rows_as_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delegated and runtime-origin content visibly changes its Session."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    mgr = SessionManager(tmp_path)
    injected = mgr.get_or_create("tui:injected")
    injected.add_message("user", "human at ten", timestamp="2026-06-10T10:00:00")
    mgr.save(injected)
    human = mgr.get_or_create("tui:human")
    human.add_message("user", "human at eleven", timestamp="2026-06-10T11:00:00")
    mgr.save(human)
    injected.add_message(
        "user",
        "delegated result at noon",
        timestamp="2026-06-10T12:00:00",
        origin="runtime",
        delegated={"kind": "subagent"},
    )
    mgr.save(injected)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    rows = (await session_list({}))["sessions"]
    assert rows[0]["id"] == "tui:injected"
    assert rows[0]["preview"] == "human at ten"
    assert rows[0]["last_message_preview"] == "delegated result at noon"


async def test_session_compress_persists_what_it_archived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compaction that is not saved is a report, not a compaction: the next read
    of the session would find every message still there."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:compress_persists"
    _write_session(tmp_path, session_key, [{"role": "user", "content": f"m{i}"} for i in range(5)])

    class _Consolidator:
        async def maybe_consolidate_by_tokens(self, session, *, force=False):
            session.last_consolidated = 3
            return {"before_tokens": 900, "after_tokens": 300, "compacted": 3}

    mgr = SessionManager(tmp_path)
    loop = SimpleNamespace(memory_consolidator=_Consolidator(), sessions=mgr)
    await session_module.session_compress({"session_id": session_key}, agent_loop_factory=lambda: loop)

    reread = SessionManager(tmp_path).get_or_create(session_key)
    assert reread.last_consolidated == 3, "the archived boundary must survive a reload"


async def test_session_list_previews_the_first_thing_the_user_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduled run has no title of its own, so the preview is the only thing
    distinguishing one row from another."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("tui:20260610_100000_ccc333")
    # A non-user message first, deliberately: a scan that takes "the first
    # message" rather than "the first *user* message" previews the machine
    # talking to itself, and a session opened by a scheduled task or a system
    # preamble is exactly where that shows.
    s.add_message("assistant", "[Scheduled Task] Timer fired")
    s.add_message("user", "summarise the quarterly report")
    s.add_message("assistant", "sure")
    mgr.save(s)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    result = await session_list({})
    assert result["sessions"][0]["preview"] == "summarise the quarterly report"


async def test_session_list_keeps_identity_separate_from_latest_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:preview_fields")
    session.add_message("user", "original question")
    session.add_message("assistant", "first answer")
    session.add_message("tool", "internal tool output")
    session.add_message("assistant", "latest answer")
    mgr.save(session)
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda cfg: mgr)

    [row] = (await session_list({}))["sessions"]
    assert row["preview"] == "original question"
    assert row["last_message_preview"] == "latest answer"


async def test_session_compress_hands_back_what_the_caller_must_redraw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a compaction the caller is displaying messages that no longer
    exist, so the reply carries the survivors, refreshed info, and usage."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:compress_redraw"
    _write_session(tmp_path, session_key, [{"role": "user", "content": f"m{i}"} for i in range(5)])

    class _Consolidator:
        # Advances the boundary and removes nothing, which is what
        # MemoryConsolidator does: it annotates through the store and moves
        # `last_consolidated`. A fake that deleted from the list agreed with a
        # handler counting `before - archived` and hid that the reply was
        # handing back every message it had just called archived.
        async def maybe_consolidate_by_tokens(self, session, force=False):
            session.last_consolidated = 3
            return {"before_tokens": 900, "after_tokens": 300, "compacted": 3}

    loop = SimpleNamespace(
        memory_consolidator=_Consolidator(),
        sessions=SessionManager(tmp_path),
        context=SimpleNamespace(skills=SimpleNamespace(list_skills=lambda **_kw: [])),
        tools=SimpleNamespace(tool_names=[], get=lambda _n: None),
        model="test/model",
    )
    result = await session_module.session_compress({"session_id": session_key}, agent_loop_factory=lambda: loop)

    assert len(result["messages"]) == 2, "the survivors, so the caller can redraw"
    # The count and the payload have to come from one slice, or a caller is told
    # a number that contradicts the list printed beside it.
    assert result["after_messages"] == len(result["messages"])
    assert [m["text"] for m in result["messages"]] == ["m3", "m4"]
    assert result["info"]["model"]
    assert isinstance(result["usage"], dict)


async def test_session_compress_reports_the_same_session_as_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The redraw bundle answers for THIS session, not for no session at all.

    The TUI adopts it wholesale, and ``info.cwd`` drives its status-bar
    directory and the git branch beside it -- so a bundle built without the key
    read as ``/compress`` moving the session to the launch directory, with the
    configured default model in place of the one it runs on, until the next
    resume put both back.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:compress_same_session"
    _write_session(tmp_path, session_key, [{"role": "user", "content": f"m{i}"} for i in range(5)])

    class _Consolidator:
        async def maybe_consolidate_by_tokens(self, session, force=False):
            session.last_consolidated = 3
            return {"before_tokens": 900, "after_tokens": 300, "compacted": 3}

    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path / "home",
        sessions=SessionManager(tmp_path),
    )
    loop = SimpleNamespace(
        memory_consolidator=_Consolidator(),
        sessions=SessionManager(tmp_path),
        context=SimpleNamespace(skills=SimpleNamespace(list_skills=lambda **_kw: [])),
        tools=SimpleNamespace(tool_names=[], get=lambda _n: None),
        model="configured/default",
        session_model=lambda key: f"chosen/for-{key}",
        peek_session_workdir=lambda key: resolver.resolve(key, create=False),
    )

    result = await session_module.session_compress({"session_id": session_key}, agent_loop_factory=lambda: loop)
    resumed = await session_resume({"session_id": session_key}, agent_loop_factory=lambda: loop)

    assert result["info"]["cwd"] == resumed["info"]["cwd"]
    assert result["info"]["cwd"] == str(default_channel_root(tmp_path / "home") / "tui")
    assert result["info"]["cwd"] != os.getcwd()
    assert result["info"]["model"] == f"chosen/for-{session_key}"


async def test_session_create_persists_the_workdir_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``workdir`` param must land in the minted session's metadata — the
    override ``WorkdirResolver`` reads — and be reported back as ``info.cwd``,
    so a client attached to a shared gateway works in its own launch
    directory rather than the gateway's."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path / "home")
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)
    mgr = SessionManager(tmp_path / "home")
    monkeypatch.setattr(session_module, "_get_or_build_manager", lambda _cfg: mgr)
    project = tmp_path / "proj"
    project.mkdir()

    result = await session_create({"workdir": str(project)})

    resolved = str(project.resolve())
    assert result["info"]["cwd"] == resolved
    assert mgr.get_or_create(result["session_id"]).metadata["workdir"] == resolved


async def test_session_create_rejects_a_workdir_inside_the_agent_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.rpc.errors import ConfigValidationError

    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path / "home")
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    with pytest.raises(ConfigValidationError):
        await session_create({"workdir": str(tmp_path / "home" / "skills")})


async def test_session_create_without_workdir_stays_lazy_and_unpinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The embedded path sends no workdir; nothing may change for it — no
    manager built, no metadata written (see the writes-no-file test above)."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path / "home")
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    def _must_not_build(_cfg):
        raise AssertionError("no workdir given, so no manager may be built")

    monkeypatch.setattr(session_module, "_get_or_build_manager", _must_not_build)

    result = await session_create({"cols": 80})
    assert _SESSION_ID_RE.match(result["session_id"])


def test_a_dag_tool_row_names_the_run_it_started() -> None:
    """The run id is in the result text the tool authored; the client should not
    have to parse prose to find it."""
    rows = _map_to_wire(
        [
            {
                "role": "tool",
                "name": "run_subagent_dag",
                "tool_call_id": "call-1",
                "content": (
                    "DAG run 20260823T063516789587Z-1e97d546 finished: 2 completed, 0 failed "
                    "(of 2).\nRun dir: /root/.raven/x/subagents/mas_dag/20260823T063516789587Z-1e97d546"
                ),
            }
        ],
        "sess",
    )

    assert rows[0]["dag_run_id"] == "20260823T063516789587Z-1e97d546"


def test_a_non_dag_tool_row_names_no_run() -> None:
    rows = _map_to_wire(
        [{"role": "tool", "name": "read_file", "tool_call_id": "c", "content": "contents"}],
        "sess",
    )

    assert "dag_run_id" not in rows[0]


def test_a_dag_row_whose_text_names_no_run_is_left_alone() -> None:
    """A graph rejected by validation returns an error, not a run."""
    rows = _map_to_wire(
        [{"role": "tool", "name": "run_subagent_dag", "tool_call_id": "c", "content": "rejected: bad spec"}],
        "sess",
    )

    assert "dag_run_id" not in rows[0]


def test_a_non_dag_tool_row_with_dag_shaped_content_names_no_run() -> None:
    """A read_file or grep row can legitimately quote a DAG's own "finished"
    line back (a saved log, a grep hit) -- the name guard, not the regex, is
    what must keep such a row from being mistaken for the run it quotes."""
    rows = _map_to_wire(
        [
            {
                "role": "tool",
                "name": "read_file",
                "tool_call_id": "c",
                "content": (
                    "DAG run 20260823T063516789587Z-1e97d546 finished: 2 completed, 0 failed "
                    "(of 2).\nRun dir: /root/.raven/x/subagents/mas_dag/20260823T063516789587Z-1e97d546"
                ),
            }
        ],
        "sess",
    )

    assert "dag_run_id" not in rows[0]


def test_a_dag_tool_row_with_notice_prefix_still_names_the_run() -> None:
    """``_with_notices`` prepends "Note: ...\\n\\n" ahead of the tool's own
    first line whenever the graph carried a capability-downgrade notice; the
    id must still be found mid-string, not just at the start of it."""
    rows = _map_to_wire(
        [
            {
                "role": "tool",
                "name": "run_subagent_dag",
                "tool_call_id": "call-1",
                "content": (
                    "Note: node 'b': agent 'x' cannot take injected skills.\n\n"
                    "DAG run 20260823T063516789587Z-1e97d546 finished: 2 completed, 0 failed "
                    "(of 2).\nRun dir: /root/.raven/x/subagents/mas_dag/20260823T063516789587Z-1e97d546"
                ),
            }
        ],
        "sess",
    )

    assert rows[0]["dag_run_id"] == "20260823T063516789587Z-1e97d546"


def test_a_spawn_tool_row_names_the_task_it_started() -> None:
    """The task id is in the result text the manager authored; the client
    should not have to parse prose to find it."""
    rows = _map_to_wire(
        [
            {
                "role": "tool",
                "name": "spawn",
                "tool_call_id": "call-1",
                "content": ("Subagent [research CO] started (id: 1a021575). I'll notify you when it completes."),
            }
        ],
        "sess",
    )

    assert rows[0]["spawn_task_id"] == "1a021575"


def test_a_spawn_label_quoting_a_started_line_does_not_hijack_the_task_id() -> None:
    """The label between the brackets is model-authored and can itself contain
    a "started (id: ...)" shape; the manager's own suffix is the last one in
    the sentence it wrote, so the last match names the task."""
    rows = _map_to_wire(
        [
            {
                "role": "tool",
                "name": "spawn",
                "tool_call_id": "call-1",
                "content": (
                    "Subagent [inspect log: started (id: deadbeef)] started (id: 1a021575). "
                    "I'll notify you when it completes."
                ),
            }
        ],
        "sess",
    )

    assert rows[0]["spawn_task_id"] == "1a021575"


def test_a_non_spawn_tool_row_with_spawn_shaped_content_names_no_task() -> None:
    """A read_file or grep row can legitimately quote a spawn's own "started"
    line back -- the name guard, not the regex, keeps such a row from being
    mistaken for the run it quotes."""
    rows = _map_to_wire(
        [
            {
                "role": "tool",
                "name": "read_file",
                "tool_call_id": "c",
                "content": "Subagent [x] started (id: 1a021575).",
            }
        ],
        "sess",
    )

    assert "spawn_task_id" not in rows[0]


def test_a_spawn_row_that_was_refused_names_no_task() -> None:
    """A refused spawn returns its refusal, not a run."""
    rows = _map_to_wire(
        [
            {
                "role": "tool",
                "name": "spawn",
                "tool_call_id": "c",
                "content": "Spawn refused: delegation is paused.",
            }
        ],
        "sess",
    )

    assert "spawn_task_id" not in rows[0]


async def test_session_resume_carries_the_stored_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The banner names what is being resumed.

    Carried on the init bundle rather than fetched separately: a client resuming
    a session is already being told what it is resuming, and a second round trip
    for the name would draw the panel without it first.
    """
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    session_key = "tui:20260610_143052_titled"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(session_key)
    session.add_message("user", "cut a desktop release")
    session.set_title("Cut a desktop release")
    mgr.save(session)

    result = await session_resume({"session_id": session_key})

    assert result["info"]["title"] == "Cut a desktop release"


async def test_session_resume_of_an_unnamed_session_carries_no_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh mint has nothing to name yet, so the field is absent rather than
    an empty string the panel would have to test for."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    result = await session_resume({"session_id": "tui:nonexistent_for_title"})

    assert result["info"].get("title") is None


async def test_session_title_refuses_a_name_past_the_ceiling_with_a_legible_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not -32603. A name too long is ordinary user input, and an internal_error
    tells the person nothing about what they typed."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    with pytest.raises(SessionTitleTooLongError) as caught:
        await session_title({"session_id": "tui:20260610_143052_toolong", "title": "x" * 201})

    assert caught.value.code == -32018
    assert caught.value.data == {"limit": 200}


async def test_session_title_answers_with_the_stored_form_not_the_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller draws what it is told. Echoing the raw argument would have it
    paint a name that is not the one on disk."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    monkeypatch.setattr(session_module, "load_config", lambda: cfg)

    result = await session_title({"session_id": "tui:20260610_143052_ws", "title": "  Ship\n the   fix "})

    assert result["title"] == "Ship the fix"


import inspect

from raven.memory_engine.consolidate.consolidator import MemoryConsolidator
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.errors import SessionTitleTooLongError, TurnInProgressError
from raven.rpc.methods import session as session_module
from raven.rpc.models import METHOD_MODELS
from raven.utils.tokens import estimate_prompt_tokens
