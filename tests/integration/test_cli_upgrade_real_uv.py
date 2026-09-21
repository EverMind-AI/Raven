"""The upgrade flow against a real uv: the running tool replaces itself in place,
and the companion package installed beside it survives.

uv replaces a tool's requirement set with what one `uv tool install` names,
so a helper that named raven alone uninstalled every plugin the installer had
put in. The new release directory carries raven-plugins.txt; the helper
installs from it, and the companion here stands in for those plugins.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

UV_PATH = shutil.which("uv")


def _build_fixture(source_root: Path, output_root: Path, version: str, uv_path: Path) -> Path:
    package_root = source_root / "upgrade_fixture"
    package_root.mkdir(parents=True)
    (source_root / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""
            [project]
            name = "raven"
            version = "{version}"
            requires-python = ">=3.12"
            dependencies = ["httpx", "rich", "typer"]

            [project.optional-dependencies]
            channels = []

            [project.scripts]
            raven = "upgrade_fixture:main"

            [build-system]
            requires = ["hatchling"]
            build-backend = "hatchling.build"

            [tool.hatch.build.targets.wheel]
            packages = ["upgrade_fixture"]
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (package_root / "__init__.py").write_text(
        textwrap.dedent(
            f'''\
            import os
            import sys


            VERSION = "{version}"


            def main():
                if sys.argv[1:] == ["--version"]:
                    print(VERSION)
                    return 0
                if sys.argv[1:] == ["--companion"]:
                    import upgrade_companion  # noqa: F401

                    print("companion present")
                    return 0
                if sys.argv[1:] != ["upgrade"]:
                    return 2

                sys.path.insert(0, os.environ["RAVEN_UPGRADE_SOURCE"])
                from raven.updates import upgrade as upgrade_commands

                target = upgrade_commands._uv_tool_target()
                if target is None:
                    raise RuntimeError("fixture uv receipt was not detected")
                release = upgrade_commands.ReleaseInfo(
                    version="2.0.0",
                    wheel_url=os.environ["RAVEN_UPGRADE_WHEEL"],
                )
                upgrade_commands._handoff_upgrade(release, VERSION, target)
            '''
        ),
        encoding="utf-8",
    )
    output_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(uv_path), "build", "--wheel", "--out-dir", str(output_root)],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return next(output_root.glob(f"raven-{version}-*.whl"))


def _build_companion(source_root: Path, output_root: Path, uv_path: Path) -> Path:
    """A plugin-shaped bystander: installed beside the tool, named by nothing
    but the release's plugin list."""
    (source_root / "upgrade_companion").mkdir(parents=True)
    (source_root / "upgrade_companion" / "__init__.py").write_text("", encoding="utf-8")
    (source_root / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "upgrade-companion"
            version = "1.0.0"
            requires-python = ">=3.12"

            [build-system]
            requires = ["hatchling"]
            build-backend = "hatchling.build"

            [tool.hatch.build.targets.wheel]
            packages = ["upgrade_companion"]
            """
        ).lstrip(),
        encoding="utf-8",
    )
    subprocess.run(
        [str(uv_path), "build", "--wheel", "--out-dir", str(output_root)],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return next(output_root.glob("upgrade_companion-1.0.0-*.whl"))


@pytest.mark.skipif(UV_PATH is None, reason="uv is required for the real self-upgrade test")
def test_running_uv_tool_replaces_itself_in_custom_directories(tmp_path: Path) -> None:
    external_tools = tmp_path / "external tools"
    external_tools.mkdir()
    external_uv = external_tools / Path(UV_PATH).name
    shutil.copy2(UV_PATH, external_uv)
    wheels = tmp_path / "wheels"
    old_wheel = _build_fixture(tmp_path / "old", wheels, "1.0.0", external_uv)
    new_wheel = _build_fixture(tmp_path / "new", wheels, "2.0.0", external_uv)
    companion = _build_companion(tmp_path / "companion", wheels, external_uv)
    # The release directory the helper reads: the plugin list sits beside the
    # wheel, exactly as release.yml lays it out. No constraints file, so the
    # unpinned path is the one exercised.
    (wheels / "raven-plugins.txt").write_text(f"upgrade-companion @ {companion.resolve().as_uri()}\n", encoding="utf-8")
    tool_dir = tmp_path / "custom tools"
    bin_dir = tmp_path / "custom bin"
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(external_tools) + os.pathsep + env["PATH"],
            "UV_TOOL_DIR": str(tool_dir),
            "UV_TOOL_BIN_DIR": str(bin_dir),
            "RAVEN_UPGRADE_SOURCE": str(Path(__file__).parents[2]),
            "RAVEN_UPGRADE_WHEEL": new_wheel.resolve().as_uri(),
        }
    )
    subprocess.run(
        [str(external_uv), "tool", "install", "--force", "--with", str(companion), str(old_wheel)],
        check=True,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    executable = bin_dir / ("raven.exe" if sys.platform == "win32" else "raven")

    def companion_present() -> bool:
        probe = subprocess.run(
            [str(executable), "--companion"], check=False, env=env, capture_output=True, text=True, timeout=30
        )
        return probe.returncode == 0 and "companion present" in probe.stdout

    assert companion_present(), (
        "the fixture must start with the companion installed, or the assertion below proves nothing"
    )

    completed = subprocess.run(
        [str(executable), "upgrade"],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Raven upgraded: 1.0.0 -> 2.0.0" in completed.stdout
    version = subprocess.run(
        [str(executable), "--version"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert version.stdout.strip() == "2.0.0"
    assert companion_present(), "the upgrade must keep the packages installed beside the tool"


def test_windows_workflow_isolates_upgrade_test_from_shared_conftests() -> None:
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "pytest --noconftest tests/integration/test_cli_upgrade_real_uv.py -q" in workflow
