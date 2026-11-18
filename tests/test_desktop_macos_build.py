"""The macOS bundle declares and compiles against one minimum system version."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "desktop" / "macos" / "build.sh"
WORKFLOW = REPO / ".github" / "workflows" / "desktop.yml"

SCRIPT_TARGET = re.compile(r'^MACOS_DEPLOYMENT_TARGET="?(?P<version>[0-9.]+)"?$', re.M)
SCRIPT_TARGET_LINE = 'TARGET="$(uname -m)-apple-macosx$MACOS_DEPLOYMENT_TARGET"'
PLIST_TARGET = re.compile(r"<key>LSMinimumSystemVersion</key><string>(?P<version>[0-9.]+)</string>")
TARGET_FLAG = '-target "$TARGET"'
CI_TARGET = re.compile(r"-apple-macosx([0-9.]+)")


def test_the_compiled_app_and_bundle_declare_the_same_version() -> None:
    build = BUILD.read_text(encoding="utf-8")
    script_version = SCRIPT_TARGET.search(build)
    plist_version = PLIST_TARGET.search(build)

    assert script_version, "build.sh does not name its macOS deployment target"
    assert plist_version, "build.sh does not write LSMinimumSystemVersion"
    assert script_version.group("version") == plist_version.group("version")
    assert SCRIPT_TARGET_LINE in build
    assert build.count("/usr/bin/swiftc") == build.count(TARGET_FLAG), (
        "every swiftc invocation in build.sh must use the deployment target"
    )


def test_ci_compiles_and_checks_the_declared_version() -> None:
    build = BUILD.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    defined = SCRIPT_TARGET.search(build)
    ci_versions = set(CI_TARGET.findall(workflow))

    assert defined, "build.sh does not name its macOS deployment target"
    assert ci_versions == {defined.group("version")}
    assert workflow.count("/usr/bin/swiftc") == workflow.count(
        f'-target "$(uname -m)-apple-macosx{defined.group("version")}"'
    ), "every swiftc invocation in Desktop CI must use the deployment target"
    assert "/usr/bin/vtool -show-build" in workflow
