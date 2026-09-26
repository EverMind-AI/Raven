"""Materialize immutable artifact packages and patch native configuration values."""

import importlib
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..harness import Artifact
from ..harness.artifact import relative_path
from .inspection import Baseline, fingerprint

PLUGIN_ID = "experimental-curator"


def merge(base: Any, change: Any) -> Any:
    if isinstance(base, dict) and isinstance(change, dict):
        result = deepcopy(base)
        for key, value in change.items():
            result[key] = merge(result[key], value) if key in result else deepcopy(value)
        return result
    return deepcopy(change)


def extend_artifact(active: Artifact, proposed: Artifact) -> Artifact:
    missing = set(proposed.remove) - active.values.keys()
    if missing:
        raise ValueError(f"cannot retire targets that are not currently authored: {sorted(missing)}")
    values = {name: value for name, value in active.values.items() if name not in proposed.remove}
    values = merge(values, proposed.values)
    for target, paths in proposed.remove_paths.items():
        held = active.values.get(target)
        if not isinstance(held, dict) or set(paths) - held.keys():
            raise ValueError(f"cannot retire content that is not currently authored: {target}")
        for path in paths:
            values[target].pop(path, None)
    return Artifact(values=values, files={**active.files, **proposed.files})


def write_package(root: Path, artifact: Artifact) -> Path:
    identity = fingerprint(artifact.model_dump(mode="json"))
    package = root / f"_curator_{identity[:20]}"
    expected = {**artifact.files}
    expected.setdefault("__init__.py", "")
    if package.exists():
        found = {
            path.relative_to(package).as_posix(): path.read_text()
            for path in package.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        if found != expected:
            raise ValueError("artifact package content changed after materialization")
        return package
    package.mkdir(parents=True)
    for name, content in expected.items():
        path = package / relative_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return package


def qualified_reference(reference: str, package: Path) -> str:
    module, separator, function = reference.partition(":")
    if not separator:
        raise ValueError(f"invalid factory reference: {reference}")
    relative = Path(*module.split("."))
    if (package / relative).with_suffix(".py").is_file() or (package / relative / "__init__.py").is_file():
        module = f"{package.name}.{module}"
    return f"{module}:{function}"


def load_object(reference: str, package: Path):
    root = str(package.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    module, _, name = qualified_reference(reference, package).partition(":")
    try:
        loaded = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != module:
            raise
        held = sorted(
            path.relative_to(package).as_posix()
            for path in package.rglob("*.py")
            if "__pycache__" not in path.parts and path != package / "__init__.py"
        )
        raise ModuleNotFoundError(
            f"No module named '{module}': the reference {reference} names a module that is neither installed nor "
            f"in the authored package, which holds {', '.join(held) or 'no source files'}. Put the module's complete "
            f"source in the artifact's files, for example files['{module.replace('.', '/')}.py'].",
            name=module,
        ) from exc
    return getattr(loaded, name)


def load_factory(reference: str, package: Path):
    value = load_object(reference, package)
    if not callable(value):
        raise TypeError(f"factory is not callable: {reference}")
    return value


def native_settings(baseline: Baseline, artifact: Artifact, declaration) -> Baseline:
    data = baseline.export()
    for name, value in artifact.values.items():
        binding = declaration.target(name).binding
        if binding.startswith("config.") or binding.startswith("raven_config."):
            root, *parts = binding.split(".")
            node = data["config" if root == "config" else "extensions"]
            for part in parts[:-1]:
                node = node[part]
            node[parts[-1]] = merge(node[parts[-1]], value)
    effective = Baseline.restore(data)
    # A tool the host disables stays disabled (no sandbox for a shell, nobody to answer a question): an authored
    # disabled_tools list only adds to the host's.
    held = baseline.config.tools.disabled_tools
    tools = effective.config.tools
    tools.disabled_tools = [*held, *(name for name in tools.disabled_tools if name not in held)]
    return effective


CONTENT_ROOTS = {
    "bootstrap_files": ".",
    "skill_files": "skills",
    "playbook_files": "playbooks",
}


def content_base(home: Path, binding: str) -> Path:
    return home / CONTENT_ROOTS[binding]


def content_updates(home: Path, artifact: Artifact, declaration) -> dict[Path, str]:
    updates = {}
    for name, value in artifact.values.items():
        binding = declaration.target(name).binding
        if binding not in CONTENT_ROOTS:
            continue
        base = content_base(home, binding)
        for relative, text in value.items():
            path = base / relative_path(relative)
            _check_parent(home, path)
            updates[path] = text
    return updates


def _check_parent(home: Path, path: Path) -> None:
    if not path.parent.resolve().is_relative_to(home.absolute()):
        raise ValueError(f"content parent escapes agent home: {path}")


def save_content(home: Path, artifact: Artifact, declaration) -> dict:
    saved = {}
    for path in content_updates(home, artifact, declaration):
        if path.is_symlink():
            saved[path] = (os.readlink(path), 0)
        elif path.exists():
            saved[path] = (path.read_bytes(), path.stat().st_mode & 0o777)
        else:
            saved[path] = (None, 0)
    return saved


def _write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".curator-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def restore_content(home: Path, saved: dict) -> None:
    for path, (content, mode) in saved.items():
        _check_parent(home, path)
        if content is None:
            path.unlink(missing_ok=True)
        elif isinstance(content, str):
            path.unlink(missing_ok=True)
            path.symlink_to(content)
        else:
            _write(path, content, mode)


@contextmanager
def install_content(home: Path, artifact: Artifact, declaration):
    saved = save_content(home, artifact, declaration)
    try:
        for path, text in content_updates(home, artifact, declaration).items():
            previous, mode = saved[path]
            _write(path, text.encode(), mode if isinstance(previous, bytes) else 0o600)
        yield
    except BaseException:
        restore_content(home, saved)
        raise


def copy_local_state(baseline: Baseline, destination: Path, exclude=()) -> Baseline:
    """Copy validation inputs while retaining home/workdir ancestry and avoiding self-copy."""
    home, workdir = baseline.config.workspace_path, baseline.workdir
    excluded = {Path(path).resolve() for path in (*exclude, destination)}

    def ignore(path, names):
        return [name for name in names if (Path(path) / name).resolve() in excluded]

    def copy(source, target):
        if source.exists():
            shutil.copytree(source, target, ignore=ignore)
        else:
            target.mkdir(parents=True)

    if home.is_relative_to(workdir):
        copied_workdir = destination / "work"
        copied_home = copied_workdir / home.relative_to(workdir)
        copy(workdir, copied_workdir)
        copied_home.mkdir(parents=True, exist_ok=True)
    elif workdir.is_relative_to(home):
        copied_home = destination / "agent"
        copied_workdir = copied_home / workdir.relative_to(home)
        copy(home, copied_home)
        copied_workdir.mkdir(parents=True, exist_ok=True)
    else:
        copied_home, copied_workdir = destination / "agent", destination / "work"
        copy(home, copied_home)
        copy(workdir, copied_workdir)
    result = Baseline.restore(baseline.export())
    result.config.agents.defaults.workspace = str(copied_home)
    result.workdir = copied_workdir
    return result


def edited_content(home, artifact, declaration):
    """Return authored paths whose current content belongs to an outside editor."""
    return {
        path
        for path, text in content_updates(home, artifact, declaration).items()
        if path.is_symlink() or not path.is_file() or path.read_text() != text
    }


def release_edited(home, active, submitted, proposed, declaration):
    """Preserve outside edits; refuse an attempted overwrite and relinquish unchanged bindings."""
    edited = edited_content(home, active, declaration)
    if not edited:
        return proposed
    clash = sorted(str(path) for path in edited & content_updates(home, submitted, declaration).keys())
    if clash:
        raise ValueError(
            f"authored content was edited outside curation and now belongs to its editor; leave it out of the candidate: {clash}"
        )
    values = {}
    for name, value in proposed.values.items():
        binding = declaration.target(name).binding
        if binding in CONTENT_ROOTS:
            base = content_base(home, binding)
            value = {relative: text for relative, text in value.items() if base / relative_path(relative) not in edited}
            if not value:
                continue
        values[name] = value
    return Artifact(values=values, files=proposed.files)


def retired_content(home, active, proposed, declaration, originals):
    """Restore only previously captured paths still owned by this deployment."""
    old, retained = content_updates(home, active, declaration), content_updates(home, proposed, declaration)
    edited = edited_content(home, active, declaration)
    result = {}
    for path in old.keys() - retained.keys():
        if path in edited:
            continue
        if path not in originals:
            raise ValueError(f"original content was not captured: {path}")
        result[path] = originals[path]
    return result
