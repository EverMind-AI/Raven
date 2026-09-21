"""One statement of what a complete install carries beside raven.

release.yml writes raven-plugins.txt from the plugin wheels it just built,
publish_beta.py builds and lists the same distributions for the beta channel,
and the upgrade helper keeps the everos line when the engines cannot build.
Three files, one list: these pins hold them equal, so adding a plugin in one
place turns red in the other two instead of drifting -- the drift that once
left the helper knowing one distribution while the installer shipped four.
"""

from __future__ import annotations

import re
from pathlib import Path

from raven.updates import upgrade
from scripts.publish_beta import PLUGIN_DISTRIBUTIONS, PLUGIN_LIST_NAME

ROOT = Path(__file__).resolve().parents[1]
RELEASE_YML = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def _step(text: str, name: str) -> str:
    start = text.index(f"- name: {name}")
    end = text.index("- name: ", start + 1)
    return text[start:end]


def test_the_release_workflow_lists_exactly_the_beta_publisher_distributions() -> None:
    step = _step(RELEASE_YML, "Write the plugin list")
    listed = re.findall(r"dist/([a-z_]+)-\*\.whl", step)
    assert listed == [name.replace("-", "_") for name in PLUGIN_DISTRIBUTIONS]
    assert f"-eq {len(PLUGIN_DISTRIBUTIONS)}" in step, "the line count guards a glob that matched nothing"
    for name in PLUGIN_DISTRIBUTIONS:
        assert f"uv build --wheel plugins-dist/{name} -o dist" in RELEASE_YML


def test_the_release_uploads_the_list_beside_the_wheel_and_the_constraints() -> None:
    create = RELEASE_YML[RELEASE_YML.index('gh release create "$tag"') :].splitlines()[0]
    assert "dist/raven-constraints.txt" in create
    assert f"dist/{PLUGIN_LIST_NAME}" in create
    assert upgrade.PLUGIN_LIST_NAME == PLUGIN_LIST_NAME
    assert f"dist/{PLUGIN_LIST_NAME}" in _step(RELEASE_YML, "Write the plugin list")


def test_the_installers_keep_the_same_distribution_on_their_memory_rung() -> None:
    """install.sh, install.ps1 and the upgrade helper each filter the list by
    the memory plugin's name for the rung that drops the engines; one rename
    must turn all three red."""
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    memory = next(name for name in PLUGIN_DISTRIBUTIONS if "memory" in name)
    assert f"grep '^{memory} ' \"$plugins\"" in sh
    assert f'-match "^{memory} "' in ps1
    for script in (sh, ps1):
        assert f"/{PLUGIN_LIST_NAME}" in script


def test_the_local_clone_mode_installs_the_same_three_distributions() -> None:
    """Run from a checkout, the installers install the workspace members
    editable instead of reading a release list; the members they name must be
    the ones a release ships, or a clone and a release would disagree about
    what a complete install is."""
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    for script in (sh, ps1):
        named = set(re.findall(r"plugins-dist[/\\]([a-z-]+)", script))
        assert named == set(PLUGIN_DISTRIBUTIONS), named


def test_the_release_installs_itself_before_it_is_published() -> None:
    """The one check that can say a release is whole: its own installer, piped
    like a user runs it, against the wheels and list this job built, with the
    plugin packages and `web` asserted on the result."""
    step = _step(RELEASE_YML, "Install the release from dist with install.sh")
    assert "sh -c 'cat install.sh | sh'" in step
    assert 'RAVEN_NO_LAUNCH: "1"' in step
    assert "import raven_everos, raven_design, raven_ppt" in step
    assert "web --help" in step
    assert RELEASE_YML.index("Install the release from dist") < RELEASE_YML.index("Create draft GitHub Release")


def test_the_helper_keeps_a_distribution_the_release_actually_ships() -> None:
    """The memory-only rung filters the list by one name; a rename on either
    side would leave that rung installing nothing and reporting nothing."""
    match = re.search(r'\.partition\(" @ "\)\[0\]\.strip\(\) == "([a-z-]+)"', upgrade._UPGRADE_HELPER_SOURCE)
    assert match is not None
    assert match.group(1) in PLUGIN_DISTRIBUTIONS
    assert upgrade._UPGRADE_HELPER_SOURCE.count(f"/{PLUGIN_LIST_NAME}") == 1
