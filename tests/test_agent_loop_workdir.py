"""AgentLoop binds a per-session working directory for the duration of a turn."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent import workdir as workdir_mod
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, SubagentWiring, TurnPolicy
from raven.agent.workdir import WorkdirPolicy, WorkdirResolver
from raven.contracts.llm_provider import LLMResponse
from raven.session.manager import SessionManager
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _FakeChatProvider:
    """Minimal provider: only used to construct an AgentLoop, never invoked."""

    def get_default_model(self) -> str:
        return "stub-model"


class _FakeReplyProvider:
    """Non-streaming provider returning one final reply with no tool calls."""

    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        return self._response

    def get_default_model(self) -> str:
        return "fake/model"


def _stub_edges(loop: AgentLoop) -> None:
    """No-op the sandbox executor bring-up so a turn runs without a VM.

    ``_connect_mcp`` needs no stub: it is already a no-op with no
    ``mcp_servers`` configured (the constructor default used here). Same
    shape as the executor stub used in the run/emit turn tests.
    """

    async def _noop() -> None:
        return None

    loop._start_executor = _noop


def _req(text: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="web", chat_id="abc", sender_id="u", chat_type=ChatType.DM),
        text=text,
    )


def _resolver(tmp_path: Path) -> WorkdirResolver:
    return WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path,
        session_root=tmp_path / "chanwork",
    )


def test_session_workdir_uses_the_resolver(tmp_path: Path) -> None:
    loop = AgentLoop(
        provider=_FakeChatProvider(), workspace=tmp_path, subagents=SubagentWiring(workdir_resolver=_resolver(tmp_path))
    )

    assert loop.session_workdir("web:abc") == tmp_path / "chanwork" / "web"


def test_session_workdir_falls_back_to_the_workspace(tmp_path: Path) -> None:
    """No resolver means the pre-split behaviour: one directory for everything."""
    loop = AgentLoop(provider=_FakeChatProvider(), workspace=tmp_path)

    assert loop.session_workdir("web:abc") == tmp_path


class _EmitCollector:
    def __init__(self) -> None:
        self.events: list = []

    async def __call__(self, ev) -> None:
        self.events.append(ev)


def _drain() -> list:
    return []


@pytest.mark.asyncio
async def test_turn_binds_the_session_workdir(tmp_path: Path) -> None:
    """A turn runs with the binding active and releases it afterwards."""
    seen: list[Path | None] = []
    loop = AgentLoop(
        provider=_FakeReplyProvider(LLMResponse(content="ok", finish_reason="stop")),
        workspace=tmp_path,
        subagents=SubagentWiring(workdir_resolver=_resolver(tmp_path)),
    )
    _stub_edges(loop)

    original = loop._set_tool_context

    def _spy(*args, **kwargs):
        seen.append(workdir_mod.current())
        return original(*args, **kwargs)

    loop._set_tool_context = _spy
    await loop.run_turn(_req("hi"), _EmitCollector(), _drain, stream=False)

    assert seen == [tmp_path / "chanwork" / "web"]
    assert workdir_mod.current() is None


def _checkpoint_loop(tmp_path: Path, provider) -> AgentLoop:
    from raven.config.raven import RuntimeConfig

    runtime = RuntimeConfig()
    runtime.checkpoint.policy = "always"
    return AgentLoop(
        provider=provider,
        workspace=tmp_path,
        subagents=SubagentWiring(workdir_resolver=_resolver(tmp_path)),
        engine=EngineWiring(runtime_config=runtime),
        policy=TurnPolicy(interactive=True),
    )


def test_checkpoints_are_per_working_directory(tmp_path: Path) -> None:
    """One shadow git repo per working directory, cached on that directory.

    Under per-channel working directories two chats on one channel share a
    directory, so they legitimately share its repo; another channel is another
    directory and must get its own. Keying the cache on the directory rather
    than on the session key is what makes both true at once.
    """
    loop = _checkpoint_loop(tmp_path, _FakeChatProvider())

    with workdir_mod.bind(loop.session_workdir("web:one")):
        first = loop._turn_checkpoint()
        again = loop._turn_checkpoint()
    with workdir_mod.bind(loop.session_workdir("web:two")):
        same_channel = loop._turn_checkpoint()
    with workdir_mod.bind(loop.session_workdir("qq:one")):
        other_channel = loop._turn_checkpoint()

    assert first is again
    assert first is same_channel
    assert first is not other_channel
    assert first._workspace == tmp_path / "chanwork" / "web"
    assert other_channel._workspace == tmp_path / "chanwork" / "qq"


class _WritingProvider:
    """Leaves an edit in the bound working directory, then replies.

    Stands in for the turn's tool calls: the checkpoint only commits when the
    turn actually changed something, so the oracle below needs a real edit.
    """

    def __init__(self, filename: str) -> None:
        self._filename = filename

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        (workdir_mod.current() / self._filename).write_text("edited\n", encoding="utf-8")
        return LLMResponse(content="ok", finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake/model"


@pytest.mark.asyncio
async def test_turn_checkpoint_commits_the_session_working_directory(tmp_path: Path) -> None:
    """The per-turn snapshot must land in the directory the turn wrote to.

    Regression: the checkpoint used to be keyed on the always-empty
    extraction session id, so every gateway turn snapshotted the fallback
    ``ws/_`` while the turn's edits landed in the session's own directory.

    Asserting the commit -- not just the cache entry -- is deliberate:
    ``_stash_recovery`` populates ``_checkpoints`` too, so a cache-only
    assertion stays green even if the end-of-turn snapshot is deleted.
    """
    loop = _checkpoint_loop(tmp_path, _WritingProvider("edited.txt"))
    _stub_edges(loop)

    await loop.run_turn(_req("hi"), _EmitCollector(), _drain, stream=False)

    session_dir = tmp_path / "chanwork" / "web"
    assert list(loop._checkpoints) == [session_dir]
    assert not (tmp_path / "chanwork" / "_").exists()

    svc = loop._checkpoints[session_dir]
    assert svc._workspace == session_dir
    rc, subject, _ = await svc._git("log", "-1", "--pretty=%s")
    assert rc == 0, "the turn left no commit in the session's shadow repo"
    assert subject.strip() == "turn web:abc [completed]"
    rc, tracked, _ = await svc._git("ls-tree", "-r", "--name-only", "HEAD")
    assert rc == 0
    assert "edited.txt" in tracked.splitlines()


def test_sandbox_mounts_the_root_covering_every_session(tmp_path: Path, monkeypatch) -> None:
    recorded: dict = {}

    def _fake_build_executor(cfg, workspace, owned_ids=None, extra_volumes=(), *, sandbox_dir=None):
        recorded["workspace"] = workspace
        recorded["extra_volumes"] = list(extra_volumes)

        class _Stub:
            pass

        return _Stub()

    monkeypatch.setattr("raven.agent.loop.main.build_executor", _fake_build_executor)
    AgentLoop(
        provider=_FakeChatProvider(), workspace=tmp_path, subagents=SubagentWiring(workdir_resolver=_resolver(tmp_path))
    )

    assert recorded["workspace"] == tmp_path / "chanwork"
    # Per-session dirs live under ``<workspace>/ws``, a child of workspace, not
    # an ancestor of it -- so the mount does not already cover agent home and
    # a separate volume must keep it reachable inside the VM.
    assert recorded["extra_volumes"] == [(str(tmp_path), "/agent-home", "rw")]


def test_sandbox_skips_the_home_volume_when_the_mount_already_covers_it(tmp_path: Path, monkeypatch) -> None:
    """LAUNCH_DIR policy with no override mounts the workspace itself -- no
    second volume is needed since agent home and the mount are the same
    directory."""
    recorded: dict = {}

    def _fake_build_executor(cfg, workspace, owned_ids=None, extra_volumes=(), *, sandbox_dir=None):
        recorded["workspace"] = workspace
        recorded["extra_volumes"] = list(extra_volumes)

        class _Stub:
            pass

        return _Stub()

    monkeypatch.setattr("raven.agent.loop.main.build_executor", _fake_build_executor)
    resolver = WorkdirResolver(WorkdirPolicy.LAUNCH_DIR, agent_home=tmp_path, launch_dir=tmp_path)
    AgentLoop(provider=_FakeChatProvider(), workspace=tmp_path, subagents=SubagentWiring(workdir_resolver=resolver))

    assert recorded["workspace"] == tmp_path
    assert recorded["extra_volumes"] == []


def test_sandboxed_turn_refuses_a_workdir_outside_the_mount(tmp_path, monkeypatch):
    """A pinned directory outside the VM mount is a hard error, not a silent miss."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sessions = SessionManager(tmp_path / "home")
    session = sessions.get_or_create("web:abc")
    session.metadata["workdir"] = str(outside)
    sessions.save(session)

    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path / "home",
        session_root=tmp_path / "home" / "chanwork",
        sessions=sessions,
    )
    loop = AgentLoop(
        provider=_FakeChatProvider(), workspace=tmp_path / "home", subagents=SubagentWiring(workdir_resolver=resolver)
    )
    monkeypatch.setattr(type(loop._executor), "is_sandboxed", property(lambda self: True))

    with pytest.raises(ValueError, match="outside the sandbox mount"):
        loop.session_workdir("web:abc")


def test_sandboxed_turn_allows_an_override_reached_through_a_symlinked_ancestor(tmp_path, monkeypatch):
    """A persisted override is resolved through symlinks; ``mount_root()`` is
    not. An override that is physically inside the mount must not be refused
    just because it was spelled through a symlinked ancestor of agent home."""
    real_home = tmp_path / "real_home"
    real_home.mkdir()
    link_home = tmp_path / "link_home"
    link_home.symlink_to(real_home, target_is_directory=True)

    sessions = SessionManager(link_home)
    session = sessions.get_or_create("web:abc")
    session.metadata["workdir"] = str(link_home / "chanwork" / "sub")
    sessions.save(session)

    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=link_home,
        # Named explicitly, and through the symlink, so the mount root is the
        # tree the override sits in -- the point of the test is that the two
        # spellings of one physical directory compare equal.
        session_root=link_home / "chanwork",
        sessions=sessions,
    )
    loop = AgentLoop(
        provider=_FakeChatProvider(), workspace=link_home, subagents=SubagentWiring(workdir_resolver=resolver)
    )
    monkeypatch.setattr(type(loop._executor), "is_sandboxed", property(lambda self: True))

    resolved = loop.session_workdir("web:abc")

    assert resolved == real_home / "chanwork" / "sub"


def test_mount_root_follows_an_explicit_workdir_under_per_session_policy(tmp_path) -> None:
    """An explicit ``-w`` override must win through every policy, including
    PER_SESSION. ``resolve()`` already lets ``explicit_workdir`` win over the
    policy default for every session; ``mount_root()`` must agree, or a
    sandboxed turn is refused unconditionally once that combination becomes
    reachable."""
    explicit = tmp_path / "explicit"
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path,
        session_root=tmp_path / "chanwork",
        explicit_workdir=explicit,
    )

    assert resolver.mount_root() == explicit
    assert resolver.resolve("web:abc") == explicit


@pytest.mark.asyncio
async def test_run_turn_refuses_a_workdir_outside_the_mount(tmp_path, monkeypatch):
    """The Step 4b guard must fire on the real turn path, not just when
    ``session_workdir`` is called directly."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sessions = SessionManager(tmp_path / "home")
    session = sessions.get_or_create("web:abc")
    session.metadata["workdir"] = str(outside)
    sessions.save(session)

    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path / "home",
        session_root=tmp_path / "home" / "chanwork",
        sessions=sessions,
    )
    loop = AgentLoop(
        provider=_FakeReplyProvider(LLMResponse(content="ok", finish_reason="stop")),
        workspace=tmp_path / "home",
        subagents=SubagentWiring(workdir_resolver=resolver),
    )
    _stub_edges(loop)
    monkeypatch.setattr(type(loop._executor), "is_sandboxed", property(lambda self: True))

    with pytest.raises(RuntimeError, match="Session working directory is invalid"):
        await loop.run_turn(_req("hi"), _EmitCollector(), _drain, stream=False)


def test_peek_does_not_create_the_directory(tmp_path: Path) -> None:
    """Reporting where a session would work must not touch the disk."""
    loop = AgentLoop(
        provider=_FakeChatProvider(), workspace=tmp_path, subagents=SubagentWiring(workdir_resolver=_resolver(tmp_path))
    )

    peeked = loop.peek_session_workdir("web:abc")

    assert peeked == tmp_path / "chanwork" / "web"
    assert not peeked.exists()
    assert loop.session_workdir("web:abc").is_dir()


def test_peek_does_not_refuse_a_workdir_outside_the_mount(tmp_path, monkeypatch) -> None:
    """A read reports the path; refusing belongs to the paths about to use it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sessions = SessionManager(tmp_path / "home")
    session = sessions.get_or_create("web:abc")
    session.metadata["workdir"] = str(outside)
    sessions.save(session)

    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path / "home",
        session_root=tmp_path / "home" / "chanwork",
        sessions=sessions,
    )
    loop = AgentLoop(
        provider=_FakeChatProvider(), workspace=tmp_path / "home", subagents=SubagentWiring(workdir_resolver=resolver)
    )
    monkeypatch.setattr(type(loop._executor), "is_sandboxed", property(lambda self: True))

    assert loop.peek_session_workdir("web:abc") == outside


def test_refused_workdir_is_not_created(tmp_path, monkeypatch) -> None:
    """The mount check runs before the mkdir, so a refusal leaves nothing behind."""
    outside = tmp_path / "outside"
    sessions = SessionManager(tmp_path / "home")
    session = sessions.get_or_create("web:abc")
    session.metadata["workdir"] = str(outside)
    sessions.save(session)

    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path / "home",
        session_root=tmp_path / "home" / "chanwork",
        sessions=sessions,
    )
    loop = AgentLoop(
        provider=_FakeChatProvider(), workspace=tmp_path / "home", subagents=SubagentWiring(workdir_resolver=resolver)
    )
    monkeypatch.setattr(type(loop._executor), "is_sandboxed", property(lambda self: True))

    with pytest.raises(ValueError, match="outside the sandbox mount"):
        loop.session_workdir("web:abc")
    assert not outside.exists()
