"""The raven-core wheel builds and stands alone in a clean venv.

Real resources: uv build, uv venv, a wheel install and a subprocess import.
The closure guard (tests/test_kernel_closure.py) already proves the source
tree's kernel imports nothing else; this smoke proves the *artifact* does --
built from the contract roster, installed with nothing but its own pins.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def test_the_kernel_wheel_builds_and_imports_alone(tmp_path):
    out = tmp_path / "dist"
    build = subprocess.run(
        [sys.executable, "scripts/build_core_wheel.py", "--out-dir", str(out), "--stage-dir", str(tmp_path / "stage")],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(out.glob("raven_core-*.whl"))
    assert len(wheels) == 1, sorted(out.iterdir())

    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True, timeout=120)
    python = venv / "bin" / "python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheels[0])],
        check=True,
        capture_output=True,
        timeout=600,
    )

    probe = (
        "import json, sys\n"
        "import raven.contracts, raven.home, raven.spine, raven.tracing\n"
        "import raven\n"
        "loaded = sorted(m for m in sys.modules if m == 'raven' or m.startswith('raven.'))\n"
        "print(json.dumps({'loaded': loaded, 'version': raven.__version__}))\n"
    )
    # Isolated mode: `python -c` puts the cwd on sys.path, and this test runs
    # with the checkout as cwd -- without -I the probe imports the source tree
    # and proves nothing about the wheel.
    check = subprocess.run(
        [str(python), "-I", "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert check.returncode == 0, check.stderr
    info = json.loads(check.stdout)
    allowed = ("raven", "raven.contracts", "raven.home", "raven.spine", "raven.tracing")
    stray = [m for m in info["loaded"] if not any(m == a or m.startswith(a + ".") for a in allowed)]
    assert not stray, stray
    assert info["version"] not in ("", "0.0.0+unknown")
