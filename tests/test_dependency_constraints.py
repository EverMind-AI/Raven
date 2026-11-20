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


_PLAYWRIGHT_PINS = (
    ("host browser extra", "pyproject.toml", ("project", "optional-dependencies", "browser")),
    (
        "design-engine render extra",
        "plugins-dist/design-engine/pyproject.toml",
        ("project", "optional-dependencies", "render"),
    ),
    ("ppt-engine carrier", "plugins-dist/ppt-engine/pyproject.toml", ("project", "dependencies")),
)


def test_the_three_playwright_pins_read_as_one_specifier() -> None:
    """playwright is declared three times on purpose: the host browser extra,
    the design render extra that imports it, and the ppt-engine carrier whose
    comment explains why an unimported dependency is not a stale one. The one
    environment they meet in resolves a single version, so the three specifiers
    must stay byte-identical -- whoever moves one moves all three in the same
    change, and a cleanup that drops the carrier turns this red before it turns
    a rendering install broken.
    """
    seen: dict[str, str] = {}
    for label, path, keys in _PLAYWRIGHT_PINS:
        table: object = tomllib.loads((_ROOT / path).read_text(encoding="utf-8"))
        for key in keys:
            table = table[key]  # type: ignore[index]
        entries = [entry for entry in table if Requirement(entry).name == "playwright"]  # type: ignore[union-attr]
        assert len(entries) == 1, f"{label}: expected exactly one playwright entry, got {entries}"
        seen[label] = entries[0]
    assert len(set(seen.values())) == 1, f"the playwright pins drifted apart: {seen}"


def test_wecom_extra_requires_the_media_upload_api() -> None:
    """The media path uses WECOM_UPLOAD_MAX_BYTES and the SDK's reply/send media
    methods, which first appear in 0.1.7. A lower floor lets 0.1.5/0.1.6 import
    the adapter and silently disable the channel.
    """
    manifest = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = manifest["project"]["optional-dependencies"]["channel-wecom"]
    requirements = [Requirement(entry) for entry in extras]
    requirement = next(entry for entry in requirements if entry.name == "wecom-aibot-sdk-python")
    assert requirement.specifier.contains("0.1.7"), f"channel-wecom must accept 0.1.7, got {requirement}"
    assert not requirement.specifier.contains("0.1.6"), f"channel-wecom must exclude 0.1.6, got {requirement}"
