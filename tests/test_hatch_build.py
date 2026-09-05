"""Unit tests for the wheel build hook in ``hatch_build.py``.

The product tree is the part of the hook with teeth. Hatchling applies no
``include`` / ``exclude`` config to a force-included path -- ``recurse_forced_files``
drops only a hardcoded set of directories -- so whatever the hook maps is exactly
what ships. A working copy of ``agents/`` holds live ``.env`` keys next to the
source, which is why the hook enumerates the tree from git instead of walking
it. These tests pin that distinction in both directions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import hatch_build

PRODUCTS = hatch_build.PRODUCTS_ROOT


class _App:
    """Stand-in for hatchling's Application: collect warnings instead of printing."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def display_warning(self, message: str) -> None:
        self.warnings.append(message)


def _run_hook(root: Path, version: str = "standard") -> tuple[dict[str, str], _App]:
    app = _App()
    hook = hatch_build.CustomBuildHook(str(root), {}, None, None, str(root / "dist"), "wheel", app)
    build_data: dict = {}
    hook.initialize(version, build_data)
    return build_data.get("force_include", {}), app


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project root whose ``agents/`` mixes committed source with the
    untracked things a configured working copy accumulates."""
    root = tmp_path / "proj"
    tree = root / "agents"
    _write(tree / "README.md")
    _write(tree / "__init__.py")
    _write(tree / "demo-agent" / "subagent.json", '{"name": "Demo"}')
    _write(tree / "demo-agent" / "run.py")
    _write(tree / "demo-agent" / "install.py")
    _write(tree / "demo-agent" / ".env.example", "DEMO_API_KEY=")
    _write(tree / "demo-agent" / "plugins" / "demo" / "hooks.py")

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )

    # Everything below lands in a working copy once the onboarding wizard has
    # run, and none of it is committed. The .env is the one that matters: it is
    # scaffolded from .env.example and filled with a real provider key.
    _write(tree / "demo-agent" / ".env", "DEMO_API_KEY=sk-or-v1-live-secret")
    _write(tree / "demo-agent" / "__pycache__" / "run.cpython-312.pyc")
    return root


def test_packages_every_committed_file_of_the_tree(repo: Path) -> None:
    force_include, _ = _run_hook(repo)

    assert force_include[str(repo / "agents" / "demo-agent" / "subagent.json")] == (
        "raven/agents/demo-agent/subagent.json"
    )
    assert force_include[str(repo / "agents" / "demo-agent" / "run.py")] == "raven/agents/demo-agent/run.py"
    assert force_include[str(repo / "agents" / "demo-agent" / "plugins" / "demo" / "hooks.py")] == (
        "raven/agents/demo-agent/plugins/demo/hooks.py"
    )
    # The tree's own README rides along like any tracked file...
    assert force_include[str(repo / "agents" / "README.md")] == "raven/agents/README.md"


def test_the_trees_init_py_stays_out_of_the_wheel(repo: Path) -> None:
    """It exists so the repo can address ``agents/`` as a package; shipped, it
    would mint an importable ``raven.agents`` subpackage nothing imports."""
    force_include, _ = _run_hook(repo)

    assert str(repo / "agents" / "__init__.py") not in force_include


def test_leaves_the_untracked_working_copy_out(repo: Path) -> None:
    force_include, _ = _run_hook(repo)
    packaged = set(force_include)

    assert str(repo / "agents" / "demo-agent" / ".env") not in packaged
    assert str(repo / "agents" / "demo-agent" / "__pycache__" / "run.cpython-312.pyc") not in packaged
    # The template is committed and carries no value, so it does ship.
    assert str(repo / "agents" / "demo-agent" / ".env.example") in packaged


def test_no_secret_reaches_the_wheel_whatever_git_reports(repo: Path) -> None:
    """A committed ``.env`` is still refused.

    Driving off the index makes this unreachable today -- ``.env`` is gitignored
    in every fork. It is asserted anyway because the cost of the rule being wrong
    once is a provider key published in every wheel.
    """
    env = repo / "agents" / "demo-agent" / ".env"
    subprocess.run(["git", "add", "-f", str(env)], cwd=repo, check=True)

    with pytest.raises(ValueError, match="refusing to package"):
        _run_hook(repo)


def test_a_project_without_the_tree_warns_and_still_builds(tmp_path: Path) -> None:
    """A wheel built from an sdist has no tree and no git to read one from.

    It must say so rather than pass quietly: the same combined-``uv build`` trap
    that the release workflow already guards for the TUI bundle produces a wheel
    whose onboarding step 5 finds nothing.
    """
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    force_include, app = _run_hook(root)

    assert not any(target.startswith("raven/agents") for target in force_include.values())
    assert any("agents" in w for w in app.warnings)


def test_a_tree_git_cannot_read_is_skipped_with_a_warning(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No git means no way to tell source from working copy, so nothing ships.

    Guessing instead -- walking the directory with a deny-list -- is what puts a
    live key in the wheel, so the tree is dropped and the build says so.
    """
    monkeypatch.setattr(hatch_build.shutil, "which", lambda _name: None)

    force_include, app = _run_hook(repo)

    assert not any(target.startswith("raven/agents") for target in force_include.values())
    assert any("agents" in w for w in app.warnings)


def test_an_untracked_tree_is_reported_as_untracked(tmp_path: Path) -> None:
    """A tree git answered about and does not track is not a broken checkout.

    Collapsing it into the "git cannot list" wording would send the reader to
    look for a git that does not work, when what is wrong is that nothing under
    the directory was ever committed.
    """
    root = tmp_path / "proj"
    (root / PRODUCTS).mkdir(parents=True)
    _write(root / PRODUCTS / "demo-agent" / "subagent.json", "{}")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    force_include, app = _run_hook(root)

    assert not any(target.startswith("raven/agents") for target in force_include.values())
    assert any("tracks no files" in w for w in app.warnings)
    assert not any("cannot list" in w for w in app.warnings)


def test_an_editable_install_gets_no_copy_of_the_tree(repo: Path) -> None:
    """An editable install is a source checkout, and ``agents_root`` reads the
    tree beside the package there.

    Hatchling honours the force-include map for editable builds as well
    (``get_forced_inclusion_map`` folds ``build_data`` in via ``build_force_include``),
    so without this guard every ``uv sync`` copies the whole tree into a
    site-packages directory that nothing ever reads.
    """
    # Shown first, then withheld, in one case: an assertion that the tree is
    # absent is satisfied just as well by a hook that maps nothing at all.
    standard, _ = _run_hook(repo, version="standard")
    assert any(target.startswith("raven/agents") for target in standard.values())

    editable, _ = _run_hook(repo, version="editable")
    assert not any(target.startswith("raven/agents") for target in editable.values())


def test_an_editable_install_gets_no_copy_of_the_web_assets(repo: Path) -> None:
    """The TUI bundle and the served page are skipped for the same reason.

    An editable install resolves both from the checkout it was built from:
    ``resolve_dist_entry`` and ``resolve_ui_dist`` try the packaged path first,
    and for an editable install that path is inside the checkout's own
    ``raven/``, where neither exists. So the site-packages copy was 4.6 MB
    uncompressed (3.0 for the bundle, 1.6 for the page) that nothing ever opens.
    """
    _write(repo / "ui-tui" / "dist" / "entry.js")
    _write(repo / "ui-web" / "dist" / "index.html")

    # Mapped first, then withheld, in one case: "the copy is absent" is
    # satisfied just as well by a hook that maps nothing at all.
    standard, _ = _run_hook(repo, version="standard")
    assert standard[str(repo / "ui-tui" / "dist")] == "raven/ui-tui/dist"
    # Source `ui-web/`, destination `raven/ui/dist`: the page's sources were
    # renamed and the installed layout was not, because `_install_guard` reads
    # that path back out of the RECORD and it is what leaves this repository.
    # Asserted as two different strings so the day someone "fixes" the
    # mismatch, this says it was deliberate.
    assert standard[str(repo / "ui-web" / "dist")] == "raven/ui/dist"

    editable, _ = _run_hook(repo, version="editable")
    assert not any(target.startswith("raven/ui") for target in editable.values())


def test_an_editable_build_does_not_advise_building_a_release_wheel(repo: Path) -> None:
    """With neither artifact present, a wheel build says what to run before
    building a release wheel. An editable build is not one, and the command that
    needs the artifact already says so accurately -- ``raven tui`` names both
    candidate paths and the npm command, ``raven web`` names ``ui-web/build.py``.
    """
    _, wheel_app = _run_hook(repo, version="standard")
    assert sum("before building a release wheel" in w for w in wheel_app.warnings) == 2

    _, editable_app = _run_hook(repo, version="editable")
    assert not any("release wheel" in w for w in editable_app.warnings)


def test_still_packages_the_tui_bundle(repo: Path) -> None:
    _write(repo / "ui-tui" / "dist" / "entry.js")

    force_include, _ = _run_hook(repo)

    assert force_include[str(repo / "ui-tui" / "dist")] == "raven/ui-tui/dist"
