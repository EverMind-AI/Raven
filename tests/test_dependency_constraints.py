"""The `[tool.uv] constraint-dependencies` ceilings, and that the lock honours them.

A constraint here exists because some *other* resolution -- `uv lock --upgrade`,
a re-lock after the lockfile is deleted, an in-repo `uv pip install` -- would
otherwise pick a version that breaks at runtime. Nothing in the suite exercises
those resolutions, so without this the only record of why a ceiling is there is
the comment beside it, and the first person to widen it finds out from a 500.

Not every resolution reads this table: `uv tool install` ignores it and takes
ceilings only from `-c`. A ceiling here is therefore not a claim that every
install path is covered.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_ROOT = Path(__file__).resolve().parents[1]


def _constraints() -> dict[str, Requirement]:
    manifest = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    raw = manifest["tool"]["uv"]["constraint-dependencies"]
    return {(r := Requirement(entry)).name: r for entry in raw}


def _locked_versions() -> dict[str, str]:
    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {p["name"]: p["version"] for p in lock["package"] if "version" in p}


def test_everalgo_boundary_is_kept_below_the_breaking_release() -> None:
    """0.3.0 added a required third field to the `DetectionResult` NamedTuple
    while `everalgo-agent-memory` still builds it with two, so everos answers
    500 from /memory/add and any sub-agent on the everos backend fails outright.
    """
    req = _constraints().get("everalgo-boundary")
    assert req is not None, "the everalgo-boundary ceiling was removed from constraint-dependencies"
    assert not req.specifier.contains("0.3.0"), f"0.3.0 must stay excluded, got {req}"


def test_every_constraint_is_satisfied_by_the_locked_version() -> None:
    """A ceiling the lock already violates is one nothing is enforcing.

    Also the negative half of the test above: a constraint that excluded
    everything would pass it and fail here.
    """
    locked = _locked_versions()
    for name, req in _constraints().items():
        version = locked.get(name)
        assert version is not None, f"{name} is constrained but absent from uv.lock"
        assert req.specifier.contains(Version(version), prereleases=True), (
            f"uv.lock pins {name}=={version}, which {req} excludes"
        )
