"""Agent Plugins package discovery: roots, packages, problems and containment."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from raven.agent_plugins.manifest import (
    AGENT_PLUGINS_SCHEMA_URL,
    MANIFEST_FILENAME,
    AgentPluginManifestError,
)
from raven.agent_plugins.package import (
    candidate_roots,
    discover_packages,
    is_contained,
    load_package,
    package_fingerprint,
)


def _package_dir(root: Path, name: str, *, version: str = "1.0.0") -> Path:
    """A directory holding a valid manifest."""
    root.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": name}
    if version:
        document["version"] = version
    (root / MANIFEST_FILENAME).write_text(json.dumps(document), encoding="utf-8")
    return root


def _link_to(link: Path, target: Path) -> None:
    """Point *link* at *target*.

    A symlink needs a privilege Windows withholds from ordinary accounts,
    so the fallback is a directory junction, which does not. Either way the
    link resolves to *target* while its own name stays inside the root.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# load_package
# ---------------------------------------------------------------------------


def test_load_package_without_a_manifest_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AgentPluginManifestError) as excinfo:
        load_package(tmp_path)
    assert MANIFEST_FILENAME in str(excinfo.value)


def test_load_package_reads_the_manifest(tmp_path: Path) -> None:
    root = _package_dir(tmp_path / "acme", "acme", version="2.1.0")
    package = load_package(root)
    assert package.name == "acme"
    assert package.version == "2.1.0"
    assert package.root == root
    assert package.manifest.schema_uri == AGENT_PLUGINS_SCHEMA_URL
    assert package.notes == ()


def test_load_package_reports_an_invalid_manifest(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    root.mkdir()
    (root / MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(AgentPluginManifestError):
        load_package(root)


# ---------------------------------------------------------------------------
# candidate_roots
# ---------------------------------------------------------------------------


def test_a_package_root_is_its_own_candidate(tmp_path: Path) -> None:
    root = _package_dir(tmp_path / "acme", "acme")
    assert candidate_roots(root) == [root]


def test_a_directory_of_package_roots_yields_its_children(tmp_path: Path) -> None:
    first = _package_dir(tmp_path / "beta", "beta")
    second = _package_dir(tmp_path / "alpha", "alpha")
    (tmp_path / "not-a-package").mkdir()
    assert candidate_roots(tmp_path) == [second, first]


def test_an_entry_with_no_package_yields_nothing(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert candidate_roots(tmp_path / "empty") == []
    assert candidate_roots(tmp_path / "missing") == []


# ---------------------------------------------------------------------------
# discover_packages
# ---------------------------------------------------------------------------


def test_discover_packages_finds_the_packages_under_a_directory(tmp_path: Path) -> None:
    _package_dir(tmp_path / "alpha", "alpha")
    _package_dir(tmp_path / "beta", "beta")
    packages, problems = discover_packages([tmp_path])
    assert [package.name for package in packages] == ["alpha", "beta"]
    assert problems == []


def test_discover_packages_skips_a_disabled_package(tmp_path: Path) -> None:
    _package_dir(tmp_path / "alpha", "alpha")
    _package_dir(tmp_path / "beta", "beta")
    packages, problems = discover_packages([tmp_path], disabled=["beta"])
    assert [package.name for package in packages] == ["alpha"]
    assert problems == []


def test_discover_packages_reports_an_invalid_manifest_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    _package_dir(tmp_path / "alpha", "alpha")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")

    packages, problems = discover_packages([tmp_path])
    assert [package.name for package in packages] == ["alpha"]
    assert len(problems) == 1
    assert problems[0].path == str(broken)
    assert "JSON" in problems[0].reason


@pytest.mark.parametrize("encoding", ["latin-1", "utf-16"])
def test_a_manifest_that_is_not_utf8_is_a_package_problem(tmp_path: Path, encoding: str) -> None:
    """A CP1252 or UTF-16 manifest is this package's problem, not the process's."""
    _package_dir(tmp_path / "good", "good")
    bad = tmp_path / "bad"
    bad.mkdir()
    document = {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "bad", "description": "\x80"}
    (bad / MANIFEST_FILENAME).write_text(json.dumps(document, ensure_ascii=False), encoding=encoding)

    packages, problems = discover_packages([tmp_path])

    assert [package.name for package in packages] == ["good"]
    assert [Path(problem.path).name for problem in problems] == ["bad"]


def test_json_nested_past_the_recursion_limit_is_a_package_problem(tmp_path: Path) -> None:
    """``json.loads`` raises ``RecursionError``, which is not a ``JSONDecodeError``."""
    _package_dir(tmp_path / "good", "good")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / MANIFEST_FILENAME).write_text("[" * 200000 + "]" * 200000, encoding="utf-8")

    packages, problems = discover_packages([tmp_path])

    assert [package.name for package in packages] == ["good"]
    assert [Path(problem.path).name for problem in problems] == ["bad"]


def test_discover_packages_keeps_the_first_of_two_packages_with_one_name(
    tmp_path: Path,
) -> None:
    first = _package_dir(tmp_path / "first" / "acme", "acme")
    second = _package_dir(tmp_path / "second" / "acme", "acme")

    packages, problems = discover_packages([first, second])
    assert [package.name for package in packages] == ["acme"]
    assert packages[0].root == first
    assert len(problems) == 1
    assert problems[0].path == str(second)
    assert "already taken" in problems[0].reason


def test_discover_packages_returns_the_packages_sorted_by_name(tmp_path: Path) -> None:
    _package_dir(tmp_path / "one" / "zulu", "zulu")
    _package_dir(tmp_path / "two" / "alpha", "alpha")
    _package_dir(tmp_path / "three" / "mike", "mike")
    packages, _ = discover_packages([tmp_path / "one", tmp_path / "two", tmp_path / "three"])
    assert [package.name for package in packages] == ["alpha", "mike", "zulu"]


def test_discover_packages_reports_an_entry_without_a_package(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    packages, problems = discover_packages([empty])
    assert packages == []
    assert len(problems) == 1
    assert problems[0].path == str(empty)
    assert MANIFEST_FILENAME in problems[0].reason


# ---------------------------------------------------------------------------
# is_contained
# ---------------------------------------------------------------------------


def test_a_child_path_is_contained(tmp_path: Path) -> None:
    root = tmp_path / "root"
    child = root / "skills"
    child.mkdir(parents=True)
    assert is_contained(root, child)
    assert is_contained(root, root)


def test_a_path_that_escapes_with_a_parent_reference_is_not_contained(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert not is_contained(root, root / ".." / "outside")
    assert not is_contained(root, tmp_path / "outside")


def test_a_symlink_that_points_outside_the_root_is_not_contained(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "escape"
    try:
        _link_to(link, outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this host cannot create a link: {exc}")
    assert not is_contained(root, link)


def test_a_symlink_that_stays_inside_the_root_is_contained(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "skills"
    inside.mkdir()
    link = root / "link"
    try:
        _link_to(link, inside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this host cannot create a link: {exc}")
    assert is_contained(root, link)


# ---------------------------------------------------------------------------
# package_fingerprint
# ---------------------------------------------------------------------------


def _tree(root: Path) -> Path:
    _package_dir(root, "acme")
    skill = root / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    return root


def test_the_fingerprint_is_stable_for_identical_trees(tmp_path: Path) -> None:
    first = _tree(tmp_path / "first")
    second = _tree(tmp_path / "second")
    assert package_fingerprint(first) == package_fingerprint(second)
    assert package_fingerprint(first) == package_fingerprint(first)


def test_the_fingerprint_changes_when_a_file_changes(tmp_path: Path) -> None:
    root = _tree(tmp_path / "acme")
    before = package_fingerprint(root)
    (root / "skills" / "demo" / "SKILL.md").write_text("# changed\n", encoding="utf-8")
    assert package_fingerprint(root) != before


def test_the_package_exposes_the_same_fingerprint(tmp_path: Path) -> None:
    root = _tree(tmp_path / "acme")
    package = load_package(root)
    assert package.fingerprint() == package_fingerprint(root)
