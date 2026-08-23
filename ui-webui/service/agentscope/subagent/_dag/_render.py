# -*- coding: utf-8 -*-
"""Render a DAG node's prompt template into concrete prompt text."""

from typing import Any

from ._errors import DagValidationError
from ._graph import DagNodeSpec
from ._paths import check_confined
from ._placeholders import Placeholder, iter_placeholders


async def render_prompt(
    node: DagNodeSpec,
    *,
    backend: Any,
    cwd: str,
    output_paths: dict[str, str],
) -> str:
    """Render ``node.prompt_template`` into the node's prompt text.

    The template is rebuilt in a single left-to-right pass over the
    placeholder spans, so a resolved value that itself contains
    ``{{ ... }}`` text is never re-scanned or re-substituted. Each
    distinct placeholder is resolved once and reused for duplicates.

    Args:
        node (`DagNodeSpec`):
            The node whose template is rendered.
        backend (`BackendBase`):
            Backend used to read referenced files.
        cwd (`str`):
            Directory that relative reference paths resolve against.
        output_paths (`dict[str, str]`):
            Mapping of completed dependency id to its output file path.

    Returns:
        `str`:
            The fully substituted prompt text.

    Raises:
        `DagValidationError`:
            When ``inputs.<key>.path`` targets a non-file input, or a
            ``ref``/file-input path escapes the session workdir.
    """
    template = node.prompt_template
    parts: list[str] = []
    last = 0
    cache: dict[str, str] = {}
    for start, end, ph in iter_placeholders(template):
        parts.append(template[last:start])
        if ph.raw not in cache:
            cache[ph.raw] = await _resolve(
                ph,
                node,
                backend=backend,
                cwd=cwd,
                output_paths=output_paths,
            )
        parts.append(cache[ph.raw])
        last = end
    parts.append(template[last:])
    return "".join(parts)


async def _resolve(
    ph: Placeholder,
    node: DagNodeSpec,
    *,
    backend: Any,
    cwd: str,
    output_paths: dict[str, str],
) -> str:
    """Resolve one placeholder to its replacement string.

    Args:
        ph (`Placeholder`):
            The placeholder to resolve.
        node (`DagNodeSpec`):
            The owning node (for input lookup).
        backend (`BackendBase`):
            Backend used to read files.
        cwd (`str`):
            Directory for relative path resolution.
        output_paths (`dict[str, str]`):
            Dependency id to output file path.

    Returns:
        `str`:
            The replacement text.

    Raises:
        `DagValidationError`:
            On ``input_path`` for a literal (non-file) input, or a
            ``ref``/file path that escapes the session workdir.
    """
    if ph.kind == "input":
        return await _input_value(ph.name, node, backend, cwd)
    if ph.kind == "input_path":
        spec = node.inputs.get(ph.name)
        if not isinstance(spec, dict) or "file" not in spec:
            raise DagValidationError(
                f"input '{ph.name}' has no file path to reference",
            )
        check_confined(str(spec["file"]), what="input path")
        return backend.abspath(str(spec["file"]), cwd=cwd)
    if ph.kind == "output":
        return await _read(backend, output_paths[ph.name], cwd)
    if ph.kind == "output_path":
        return output_paths[ph.name]
    if ph.kind == "ref":
        check_confined(ph.name, what="ref")
        return await _read(backend, ph.name, cwd)
    # ph.kind == "ref_path"
    check_confined(ph.name, what="ref_path")
    return backend.abspath(ph.name, cwd=cwd)


async def _input_value(
    key: str,
    node: DagNodeSpec,
    backend: Any,
    cwd: str,
) -> str:
    """Return an input's value: literal, or file contents for a file spec.

    Args:
        key (`str`):
            The input key.
        node (`DagNodeSpec`):
            The owning node.
        backend (`BackendBase`):
            Backend used to read a file input.
        cwd (`str`):
            Directory for relative path resolution.

    Returns:
        `str`:
            The literal string or the referenced file's contents.

    Raises:
        `DagValidationError`:
            When a file input's path escapes the session workdir.
    """
    spec = node.inputs.get(key)
    if isinstance(spec, dict) and "file" in spec:
        check_confined(str(spec["file"]), what="input file")
        return await _read(backend, str(spec["file"]), cwd)
    return str(spec)


async def _read(backend: Any, path: str, cwd: str) -> str:
    """Read a backend file as UTF-8 text, resolving against ``cwd``.

    Args:
        backend (`BackendBase`):
            Backend used for the read.
        path (`str`):
            A relative or absolute path in the backend environment.
        cwd (`str`):
            Directory a relative ``path`` resolves against.

    Returns:
        `str`:
            The decoded file contents.
    """
    resolved = backend.abspath(path, cwd=cwd)
    data = await backend.read_file(resolved)
    return data.decode("utf-8", errors="replace")
