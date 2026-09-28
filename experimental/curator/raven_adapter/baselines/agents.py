"""Invoke existing product renderers in an isolated preparation process."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from .prepare import prepare

_PREFIXES = {
    "raven-code": "CODE",
    "raven-research": "RESEARCH_NG",
    "raven-oncall": "ONCALL",
    "raven-design": "DESIGN",
    "raven-ppt": "PPT",
}


def prepare_agent(directory: Path, *, root: Path, workdir: Path, task=None, mode=None, environment=None, config=None):
    directory, root = Path(directory).resolve(), Path(root).resolve()
    if directory.name not in _PREFIXES or not (directory / "run.py").is_file():
        raise ValueError(f"unsupported agent launcher: {directory}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = subprocess.run(
        [sys.executable, "-m", __name__],
        input=json.dumps(
            {
                "directory": str(directory),
                "root": str(root),
                "mode": mode,
                "config": str(Path(config).resolve()) if config else None,
            }
        ),
        text=True,
        capture_output=True,
        timeout=120,
        env={**os.environ, **(environment or {})},
        cwd=Path(__file__).resolve().parents[4],
    )
    if result.returncode:
        raise ValueError(f"agent preparation failed: {result.stderr[-4000:]}")
    preparation = json.loads((root / "prepared.json").read_text())
    rendered = Path(preparation["config"])
    baseline = prepare(rendered, workdir=workdir, task=task, source_roots=(directory,), mode=mode)
    baseline.inherit_model = preparation["inherit_model"]
    return baseline


def _render(request):
    from ..materialize import _write

    directory, root = Path(request["directory"]), Path(request["root"])
    prefix = _PREFIXES[directory.name]
    os.environ[f"{prefix}_STATE_ROOT"] = str(root / "state")
    os.environ.setdefault(f"{prefix}_ACP_HOME", str(root / "home"))
    spec = importlib.util.spec_from_file_location("curator_product_launcher", directory / "run.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    source = Path(request["config"]) if request.get("config") else directory / "config.json"
    if directory.name == "raven-code":
        rendered = launcher.render_acp_config(source, mode=request.get("mode"))
    else:
        rendered = launcher.render_config(source)
    own_key = next(iter(getattr(launcher, "REQUIRED_SECRETS", ())), None)
    inherited = own_key is None or not launcher.env_value(own_key)
    _write(root / "prepared.json", json.dumps({"config": str(rendered), "inherit_model": inherited}).encode())


if __name__ == "__main__":
    _render(json.load(sys.stdin))
