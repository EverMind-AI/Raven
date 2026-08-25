"""The guard on a user-supplied working directory.

Every case here is a directory somebody could plausibly name -- a project, a
home, a scratch tree -- and the ones that are refused are refused because the
agent would end up writing over its own memory, skills or credentials. The
reasons differ per case, which is why they are enumerated rather than sampled.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.workdir import is_within, validate_override


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
    `oauth/`, `cron/`, `logs/`. A per-turn checkpoint that runs `add -A` over the
    working directory, with a `.raven/` exclude that cannot help when `.raven` is
    itself the work-tree root, commits every credential into a shadow repo.
    `user_memory/` and `skills/` are also back within relative reach, one level
    deeper than from agent home.
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


def test_override_comes_back_resolved(tmp_path: Path) -> None:
    """The caller stores what this returns, so the symlink is followed once here
    rather than differently by each later reader."""
    home = tmp_path / "home"
    home.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert validate_override(link, home) == real.resolve()


def test_a_symlink_into_a_protected_subtree_is_still_refused(tmp_path: Path) -> None:
    """Resolving before comparing is what makes this hold: checked as written,
    the path looks like an ordinary sibling."""
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    link = tmp_path / "innocent"
    link.symlink_to(home / "skills")

    with pytest.raises(ValueError, match="skills"):
        validate_override(link, home)


def test_is_within_compares_physical_paths(tmp_path: Path) -> None:
    """A validated override is resolved and a workspace root generally is not.
    Comparing the two unresolved can disagree about one directory on disk."""
    root = tmp_path / "root"
    (root / "inside").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(root / "inside")

    assert is_within(link, root) is True
    assert is_within(tmp_path / "elsewhere", root) is False
