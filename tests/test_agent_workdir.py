"""Unit tests for the session working-directory resolver."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.workdir import (
    WorkdirPolicy,
    WorkdirResolver,
    bind,
    current,
    validate_override,
)


class _StubSessions:
    """Stands in for SessionManager: only ``get_or_create`` is used."""

    def __init__(self, metadata_by_key: dict[str, dict]) -> None:
        self._metadata_by_key = metadata_by_key

    def get_or_create(self, key: str):
        return SimpleNamespace(metadata=self._metadata_by_key.get(key, {}))


def test_every_chat_on_a_channel_shares_one_directory(tmp_path: Path) -> None:
    """The channel is the unit a user configures, so it is the unit of
    isolation; two chats on it are the same kind of work."""
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path,
        session_root=tmp_path / "chanwork",
    )
    assert resolver.resolve("web:2fb833e6") == tmp_path / "chanwork" / "web"
    assert resolver.resolve("web:other") == tmp_path / "chanwork" / "web"
    assert resolver.resolve("qq:2fb833e6") == tmp_path / "chanwork" / "qq"


def test_a_configured_channel_workspace_wins_over_the_default_root(tmp_path: Path) -> None:
    pinned = tmp_path / "elsewhere"
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path,
        session_root=tmp_path / "chanwork",
        channel_workspaces={"qq": str(pinned), "web": ""},
    )
    assert resolver.resolve("qq:123", create=False) == pinned
    # An empty configured value is not a configured value.
    assert resolver.resolve("web:123", create=False) == tmp_path / "chanwork" / "web"


def test_per_session_creates_the_directory(tmp_path: Path) -> None:
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path,
        session_root=tmp_path / "chanwork",
    )
    assert resolver.resolve("web:abc").is_dir()


def test_colonless_key_gets_a_placeholder_chat_id(tmp_path: Path) -> None:
    """``heartbeat`` is the one session key in the codebase with no colon."""
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path,
        session_root=tmp_path / "chanwork",
    )
    assert resolver.resolve("heartbeat") == tmp_path / "chanwork" / "heartbeat"


def test_chat_id_with_separators_is_sanitised(tmp_path: Path) -> None:
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path,
        session_root=tmp_path / "chanwork",
    )
    resolved = resolver.resolve("whatsapp:../../etc/passwd")
    assert (tmp_path / "chanwork") in resolved.parents


@pytest.mark.parametrize(
    "session_key",
    ["web:..", "web:.", "..:chat", ".:chat", "..:.."],
)
def test_dot_only_key_segments_cannot_escape_the_session_root(tmp_path: Path, session_key: str) -> None:
    """``safe_filename`` passes ``.`` and ``..`` through untouched.

    That is harmless for ``sessions/<chat_id>.jsonl`` (the suffix makes an
    ordinary name) but not for a bare directory segment: ``ws/web/..`` resolves
    to ``ws``, so every session with such a key would share one directory and
    one shadow repo -- the exact isolation this layout exists to provide. The
    sibling case with separators is not enough to catch this: ``/`` becomes
    ``_``, which leaves one inert segment, while a bare ``..`` never had one.
    """
    root = tmp_path / "chanwork"
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path,
        session_root=root,
    )
    resolved = resolver.resolve(session_key, create=False)
    assert root in resolved.parents
    assert resolved != root
    assert ".." not in resolved.parts


def test_launch_dir_ignores_the_session_key(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    resolver = WorkdirResolver(
        WorkdirPolicy.LAUNCH_DIR,
        agent_home=tmp_path / "home",
        launch_dir=project,
    )
    assert resolver.resolve("cli:one") == project
    assert resolver.resolve("cli:two") == project


def test_persisted_override_beats_the_policy_default(tmp_path: Path) -> None:
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path,
        session_root=tmp_path / "chanwork",
        sessions=_StubSessions({"web:abc": {"workdir": str(pinned)}}),
    )
    assert resolver.resolve("web:abc") == pinned


def test_explicit_workdir_beats_the_persisted_override(tmp_path: Path) -> None:
    """A flag typed for this run wins over anything on disk."""
    pinned = tmp_path / "pinned"
    flagged = tmp_path / "flagged"
    pinned.mkdir()
    flagged.mkdir()
    resolver = WorkdirResolver(
        WorkdirPolicy.LAUNCH_DIR,
        agent_home=tmp_path,
        launch_dir=tmp_path,
        explicit_workdir=flagged,
        sessions=_StubSessions({"cli:abc": {"workdir": str(pinned)}}),
    )
    assert resolver.resolve("cli:abc") == flagged


def test_override_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        validate_override("relative/path", tmp_path)


@pytest.mark.parametrize("subtree", ["user_memory", "skills", "sessions"])
def test_override_cannot_point_into_agent_home_internals(tmp_path: Path, subtree: str) -> None:
    with pytest.raises(ValueError, match=subtree):
        validate_override(tmp_path / subtree / "nested", tmp_path)


def test_override_cannot_be_agent_home_itself(tmp_path: Path) -> None:
    """Working in agent home puts every protected subtree one relative path away."""
    with pytest.raises(ValueError, match="agent home"):
        validate_override(tmp_path, tmp_path)


def test_override_cannot_be_an_ancestor_of_agent_home(tmp_path: Path) -> None:
    """An ancestor is refused for a stronger reason than agent home itself.

    The realistic ancestor is `~/.raven`, which is not merely agent home's
    parent but the instance data directory: `config.json` (provider keys),
    `oauth/`, `cron/`, `logs/`. The per-turn checkpoint runs `add -A` over the
    working directory, and the `.raven/` default exclude cannot help when
    `.raven` is itself the work-tree root, so working there commits every
    credential into a shadow repo. `user_memory/` and `skills/` are also back
    within relative reach, one level deeper than from agent home.
    """
    home = tmp_path / "nested" / "home"
    home.mkdir(parents=True)
    with pytest.raises(ValueError, match="agent home"):
        validate_override(tmp_path, home)
    with pytest.raises(ValueError, match="agent home"):
        validate_override(tmp_path / "nested", home)


def test_override_may_be_a_sibling_of_agent_home(tmp_path: Path) -> None:
    """Containment is the test, not proximity: a directory next to agent home
    holds none of it, so `add -A` there captures nothing protected."""
    home = tmp_path / "nested" / "home"
    home.mkdir(parents=True)
    sibling = tmp_path / "nested" / "project"
    sibling.mkdir()
    assert validate_override(sibling, home) == sibling


def test_binding_is_scoped(tmp_path: Path) -> None:
    assert current() is None
    with bind(tmp_path):
        assert current() == tmp_path
    assert current() is None


def test_mount_root_covers_every_per_session_directory(tmp_path: Path) -> None:
    """One sandbox mount has to contain every directory the process can produce."""
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path,
        session_root=tmp_path / "chanwork",
    )
    root = resolver.mount_root()
    assert root == tmp_path / "chanwork"
    assert root in resolver.resolve("web:abc").parents


def test_mount_root_is_the_launch_dir_for_terminal_entrypoints(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    resolver = WorkdirResolver(
        WorkdirPolicy.LAUNCH_DIR,
        agent_home=tmp_path / "home",
        launch_dir=project,
    )
    assert resolver.mount_root() == project


@pytest.mark.parametrize(
    "configured",
    [
        "",  # agent home itself, filled in by the test
        "skills/nested",
        "relative/path",
        "..",
    ],
)
def test_a_bad_channel_workspace_is_dropped_not_obeyed(tmp_path: Path, configured: str, caplog) -> None:
    """`channels.<name>.workspace` reaches the resolver from a config file and
    from the web UI's channels form, and nothing else validates it. Left
    unchecked, `channels.x.workspace = <agent home>` is accepted and the
    per-turn `add -A` then runs over user_memory/ and sessions/ -- the shape
    every other entry point already rejects.
    """
    home = tmp_path / "home"
    home.mkdir()
    value = str(home) if not configured else str(home / configured) if configured != "relative/path" else configured

    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=home,
        session_root=tmp_path / "chanwork",
        channel_workspaces={"qq": value},
    )

    assert resolver.resolve("qq:1", create=False) == tmp_path / "chanwork" / "qq"


def test_a_valid_channel_workspace_is_still_honoured(tmp_path: Path) -> None:
    """The guard must reject shapes, not the feature."""
    home = tmp_path / "home"
    pinned = tmp_path / "elsewhere"
    home.mkdir()
    pinned.mkdir()
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=home,
        session_root=tmp_path / "chanwork",
        channel_workspaces={"qq": str(pinned)},
    )

    assert resolver.resolve("qq:1", create=False) == pinned


def test_default_channel_root_never_lands_in_the_system_tmp(tmp_path: Path) -> None:
    """A home directly under the filesystem root is the case a plain sibling
    gets wrong: `--home /workspace` (a common container mount) would put every
    channel in the shared, world-writable, reboot-cleared system /tmp.
    """
    from raven.agent.workdir import default_channel_root

    assert default_channel_root(Path("/workspace")) == Path("/workspace-tmp")
    assert default_channel_root(Path("/")) == Path("/raven-tmp")
    # The shape everyone actually runs is unchanged.
    assert default_channel_root(Path("/root/.raven/workspace")) == Path("/root/.raven/tmp")
    assert default_channel_root(tmp_path / "a" / "home") == tmp_path / "a" / "tmp"
