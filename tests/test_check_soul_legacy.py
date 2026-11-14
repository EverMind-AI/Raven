"""The SOUL.md legacy gate over a real git history."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from scripts import check_soul_legacy

SOUL = "raven/templates/SOUL.md"
LEGACY = "raven/templates/legacy/SOUL-old.md"


def _git(root: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "PATH": os.environ["PATH"],
    }
    return subprocess.check_output(["git", *args], cwd=root, text=True, env=env).strip()


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


def _changed_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    (repo / "raven" / "templates" / "legacy").mkdir(parents=True)
    old = "# Soul\n\nOld voice.\n"
    (repo / SOUL).write_text(old, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "feat: seed the soul")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / SOUL).write_text("# Soul\n\nNew voice.\n", encoding="utf-8")
    _commit(repo, "feat: change the soul")
    return repo, base, old


def _run(repo: Path, base: str) -> int:
    cwd = Path.cwd()
    try:
        os.chdir(repo)
        return check_soul_legacy.main([f"{base}..HEAD"])
    finally:
        os.chdir(cwd)


def test_a_missing_legacy_copy_fails_and_adding_it_passes(tmp_path: Path, capsys) -> None:
    repo, base, old = _changed_repo(tmp_path)

    assert _run(repo, base) == 1
    assert "outgoing text" in capsys.readouterr().err

    (repo / LEGACY).write_text(old, encoding="utf-8")
    _commit(repo, "test: keep the outgoing soul")

    assert _run(repo, base) == 0


def test_a_main_side_change_does_not_fail_a_branch_that_did_not_touch_soul(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "raven" / "templates" / "legacy").mkdir(parents=True)
    (repo / SOUL).write_text("# Soul\n\nOld voice.\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "feat: seed the soul")
    base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("work\n", encoding="utf-8")
    _commit(repo, "feat: add the branch work")

    _git(repo, "checkout", "-q", "main")
    (repo / SOUL).write_text("# Soul\n\nA newer voice on main.\n", encoding="utf-8")
    _commit(repo, "feat: change the soul on main")
    main_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "feature")

    assert _git(repo, "merge-base", base, "HEAD") == base

    assert _run(repo, main_tip) == 0


def test_an_unchanged_soul_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "raven" / "templates" / "legacy").mkdir(parents=True)
    (repo / SOUL).write_text("# Soul\n\nOnly voice.\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "feat: seed the soul")
    base = _git(repo, "rev-parse", "HEAD")

    assert _run(repo, base) == 0


def test_a_line_ending_only_change_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "raven" / "templates" / "legacy").mkdir(parents=True)
    (repo / SOUL).write_bytes(b"# Soul\r\n\r\nSame voice.\r\n")
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "feat: seed the soul")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / SOUL).write_bytes(b"# Soul\n\nSame voice.\n")
    _commit(repo, "style: rewrite the line endings")

    assert _run(repo, base) == 0
