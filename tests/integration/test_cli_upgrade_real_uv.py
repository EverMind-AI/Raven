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
                if sys.argv[1:] != ["upgrade"]:
                    return 2

                sys.path.insert(0, os.environ["RAVEN_UPGRADE_SOURCE"])
                from raven.cli import upgrade_commands

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


@pytest.mark.skipif(UV_PATH is None, reason="uv is required for the real self-upgrade test")
def test_running_uv_tool_replaces_itself_in_custom_directories(tmp_path: Path) -> None:
    external_tools = tmp_path / "external tools"
    external_tools.mkdir()
    external_uv = external_tools / Path(UV_PATH).name
    shutil.copy2(UV_PATH, external_uv)
    wheels = tmp_path / "wheels"
    old_wheel = _build_fixture(tmp_path / "old", wheels, "1.0.0", external_uv)
    new_wheel = _build_fixture(tmp_path / "new", wheels, "2.0.0", external_uv)
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
        [str(external_uv), "tool", "install", "--force", str(old_wheel)],
        check=True,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    executable = bin_dir / ("raven.exe" if sys.platform == "win32" else "raven")

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


def test_windows_workflow_isolates_upgrade_test_from_shared_conftests() -> None:
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "pytest --noconftest tests/integration/test_cli_upgrade_real_uv.py -q" in workflow
