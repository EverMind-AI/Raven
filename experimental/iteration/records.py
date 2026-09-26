"""Read one worker root's iteration, analysis and curation records as a single joined run."""

import json
from pathlib import Path


def runs(root: Path) -> list[Path]:
    """Iteration record files under a worker root, oldest first."""
    directory = Path(root) / "iteration"
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime)


def load(path: Path) -> dict:
    """The run record with each round's analysis and curation records read in beside it."""
    path = Path(path)
    root = path.parents[1]
    run = json.loads(path.read_text())
    run["initial_curation"] = [_read(root / "curation" / name) for name in run.get("initial_curation", [])]
    for item in run["rounds"]:
        for exchanges in item.get("sessions", {}).values():
            for exchange in exchanges:
                execution = exchange["execution"]
                execution["deliverables"] = [_here(path, root) for path in execution.get("deliverables", [])]
        item["analysis"] = [_read(root / "analysis" / name) for name in item.get("analysis", [])]
        item["curation"] = [_read(root / "curation" / name) for name in item.get("curation", [])]
    return run


def _here(path: str, root: Path) -> str:
    """A kept deliverable's path under this worker root, so a moved or copied run still finds its files."""
    parts = Path(path).parts
    return str(root.joinpath(*parts[parts.index("deliverables") :])) if "deliverables" in parts else path


def _read(path: Path) -> dict:
    if not path.is_file():
        return {"missing": path.name}
    return {"file": path.name, **json.loads(path.read_text())}
