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

from raven.config.raven import MEMORY_BACKEND_DISTRIBUTIONS
from raven.updates import upgrade
from scripts.publish_beta import PLUGIN_DISTRIBUTIONS, PLUGIN_LIST_NAME

ROOT = Path(__file__).resolve().parents[1]
RELEASE_YML = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
#: The shipped distributions that carry a memory backend, in list order. The
#: rung that drops the engines keeps exactly these, so that a host whose
#: `memory.backend` names any of them keeps its memory when a native build
#: refuses on the user's platform.
MEMORY_DISTRIBUTIONS = tuple(
    name for name in PLUGIN_DISTRIBUTIONS if name in set(MEMORY_BACKEND_DISTRIBUTIONS.values())
)


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


def test_the_installers_keep_the_memory_distributions_on_their_memory_rung() -> None:
    """install.sh, install.ps1 and the upgrade helper each filter the list down
    to the memory backends for the rung that drops the engines; a rename, or a
    second memory backend the release ships, must turn all three red rather
    than leave one rung quietly installing a subset."""
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert len(MEMORY_DISTRIBUTIONS) > 1, MEMORY_DISTRIBUTIONS
    alternation = "|".join(MEMORY_DISTRIBUTIONS)
    assert f"grep -E '^({alternation}) ' \"$plugins\"" in sh
    assert f'-match "^({alternation}) "' in ps1
    for script in (sh, ps1):
        assert f"/{PLUGIN_LIST_NAME}" in script


def test_the_local_clone_mode_installs_the_same_distributions() -> None:
    """Run from a checkout, the installers install the workspace members
    editable instead of reading a release list; the members they name must be
    the ones a release ships, or a clone and a release would disagree about
    what a complete install is."""
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    for script in (sh, ps1):
        named = set(re.findall(r"plugins-dist[/\\]([a-z-]+)", script))
        assert named == set(PLUGIN_DISTRIBUTIONS), named


def test_the_release_notes_template_matches_the_roster_it_describes() -> None:
    """The workflow copies this template into every draft release, so a roster
    change that leaves it behind advertises the opposite of what the release
    does. It once promised that the plugin list installs every wheel but the
    cloud one, which stopped being true the moment that wheel joined the list."""
    template = (ROOT / ".github" / "release-notes-template.md").read_text(encoding="utf-8")
    for name in PLUGIN_DISTRIBUTIONS:
        assert f"`{name.replace('-', '_')}`" in template, name
    assert "installs every plugin wheel the release carries" in template


def test_the_release_installs_itself_before_it_is_published() -> None:
    """The one check that can say a release is whole: its own installer, piped
    like a user runs it, against the wheels and list this job built, with the
    plugin packages and `web` asserted on the result."""
    step = _step(RELEASE_YML, "Install the release from dist with install.sh")
    assert "sh -c 'cat install.sh | sh'" in step
    assert 'RAVEN_NO_LAUNCH: "1"' in step
    assert "import raven_everos, raven_everos_cloud, raven_design, raven_ppt" in step
    assert "web --help" in step
    assert RELEASE_YML.index("Install the release from dist") < RELEASE_YML.index("Create draft GitHub Release")


def test_the_helper_keeps_the_distributions_the_release_actually_ships() -> None:
    """The memory-only rung filters the list by name; a rename on either side,
    or a memory backend the release ships and the helper does not name, would
    leave that rung installing the wrong set and reporting nothing."""
    match = re.search(r"\n    memory = \(([^)]*)\)\n", upgrade._UPGRADE_HELPER_SOURCE)
    assert match is not None
    assert tuple(re.findall(r'"([a-z-]+)"', match.group(1))) == MEMORY_DISTRIBUTIONS
    assert upgrade._UPGRADE_HELPER_SOURCE.count(f"/{PLUGIN_LIST_NAME}") == 1
