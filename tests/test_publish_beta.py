"""Publish-boundary gate for the beta wheel.

``_verify_wheel`` is the last thing between a mis-built wheel and the channel
testers install from. Each check exists for an artifact that degrades quietly:
a missing bundle or page still installs and still starts, a missing sub-agent
tree just reports an empty installation, and a leaked secrets file is invisible
until someone unzips the wheel.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.publish_beta import PublishError, _verify_wheel

_COMPLETE = (
    "raven/__init__.py",
    "raven/ui-tui/dist/entry.js",
    "raven/ui/dist/index.html",
    "raven/ui/dist/assets/app.js",
    "raven/subagents/install.sh",
    "raven/subagents/raven-code/subagent.json",
    "raven/subagents/raven-code/.env.example",
)


def _wheel(tmp_path: Path, names: tuple[str, ...]) -> Path:
    path = tmp_path / "raven-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, "x")
    return path


def test_a_complete_wheel_passes(tmp_path: Path) -> None:
    _verify_wheel(_wheel(tmp_path, _COMPLETE))


@pytest.mark.parametrize(
    ("dropped", "message"),
    [
        ("raven/ui-tui/dist/entry.js", "entry.js missing"),
        ("raven/ui/dist/index.html", "index.html missing"),
        ("raven/ui/dist/assets/app.js", "page assets missing"),
        ("raven/subagents/raven-code/subagent.json", "vendored sub-agents missing"),
    ],
)
def test_each_missing_artifact_is_refused(tmp_path: Path, dropped: str, message: str) -> None:
    names = tuple(name for name in _COMPLETE if name != dropped)

    with pytest.raises(PublishError, match=message):
        _verify_wheel(_wheel(tmp_path, names))


def test_a_leaked_secrets_file_is_refused(tmp_path: Path) -> None:
    """The tree is read from git's index so this cannot happen upstream. It is
    still refused here: the cost of that rule being wrong once is a provider key
    published to everyone who installs the build."""
    names = (*_COMPLETE, "raven/subagents/raven-code/.env")

    with pytest.raises(PublishError, match="secrets leaked"):
        _verify_wheel(_wheel(tmp_path, names))


def test_the_template_beside_it_is_not_mistaken_for_one(tmp_path: Path) -> None:
    """``.env.example`` carries no value and ships; the check must not eat it,
    or every correctly built wheel is refused."""
    _verify_wheel(_wheel(tmp_path, _COMPLETE))

    with pytest.raises(PublishError, match="secrets leaked"):
        _verify_wheel(_wheel(tmp_path, (*_COMPLETE, "raven/subagents/raven-code/.env.local")))


def test_node_modules_is_still_refused(tmp_path: Path) -> None:
    names = (*_COMPLETE, "raven/ui-tui/node_modules/react/index.js")

    with pytest.raises(PublishError, match="node_modules leaked"):
        _verify_wheel(_wheel(tmp_path, names))
