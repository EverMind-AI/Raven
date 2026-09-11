"""Factories for the tool face code-flow contributes (raven-plugin.toml names them).

Every factory reads the flow slice's ``tools`` section and declines (returns
None) when that section leaves the face off or the slice does not parse, so a
product that wants the host's own tools simply says so and gets them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import ValidationError

from code_flow.config import FlowConfig, ToolsConfig
from code_flow.tools.exec import SPILL_SUBDIR, CodeExecTool, CodeExecutor
from code_flow.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from code_flow.tools.read_state import owner_for
from code_flow.tools.search import GlobTool
from code_flow.tools.todo import STORES, TodoTool

if TYPE_CHECKING:
    from raven.plugins.context import PluginContext


def _face(ctx: "PluginContext", seat: str) -> ToolsConfig | None:
    """The tool face's own section of the flow slice, or None to decline."""
    try:
        flow = FlowConfig.from_slice(dict(ctx.config or {}))
    except ValidationError as exc:
        logger.warning("code-flow: config slice is malformed; declining {}: {}", seat, exc)
        return None
    if not flow.tools.enabled:
        return None
    return flow.tools


def _fence(ctx: "PluginContext", cfg: ToolsConfig) -> tuple[Path, ...]:
    """The roots these tools may reach, mirroring what the host grants its own.

    The host computes ``allowed_dirs = (workspace,) if restrict_to_workspace``
    (raven/agent/loop/wiring.py) and an empty tuple means the fence is OFF --
    which is what these replacements used to pass, whatever the product asked
    for. The section carries the answer because a plugin factory cannot read
    the host field; the launcher renders it in.
    """
    return (Path(ctx.services.workspace),) if cfg.restrict_to_workspace else ()


def make_read_file(ctx: "PluginContext") -> ReadFileTool | None:
    cfg = _face(ctx, "read_file")
    if not cfg:
        return None
    return ReadFileTool(workspace=ctx.services.workspace, allowed_dirs=_fence(ctx, cfg), read_owner=owner_for(ctx))


def make_write_file(ctx: "PluginContext") -> WriteFileTool | None:
    cfg = _face(ctx, "write_file")
    if not cfg:
        return None
    return WriteFileTool(
        workspace=ctx.services.workspace,
        allowed_dirs=_fence(ctx, cfg),
        syntax_note=cfg.python_syntax_note,
        read_owner=owner_for(ctx),
    )


def make_edit_file(ctx: "PluginContext") -> EditFileTool | None:
    cfg = _face(ctx, "edit_file")
    if not cfg:
        return None
    return EditFileTool(
        workspace=ctx.services.workspace,
        allowed_dirs=_fence(ctx, cfg),
        syntax_note=cfg.python_syntax_note,
        read_owner=owner_for(ctx),
        require_read=cfg.require_read_before_edit,
    )


def make_list_dir(ctx: "PluginContext") -> ListDirTool | None:
    cfg = _face(ctx, "list_dir")
    if not cfg:
        return None
    return ListDirTool(workspace=ctx.services.workspace, allowed_dirs=_fence(ctx, cfg))


def make_glob(ctx: "PluginContext") -> GlobTool | None:
    cfg = _face(ctx, "glob")
    if not cfg:
        return None
    return GlobTool(workspace=ctx.services.workspace, allowed_dirs=_fence(ctx, cfg))


def make_exec(ctx: "PluginContext") -> CodeExecTool | None:
    """The shell, with the clamped ceiling, the partial-output marker and the
    30,000-character budget with spill. Served only where the host's own exec
    would run on the host too (``tools.sandbox.backend == "none"``): the
    replacement runs on the host, and a configured sandbox must not be
    bypassed by a same-name tool -- so under any other backend this declines
    and the host's exec keeps the shell."""
    cfg = _face(ctx, "exec")
    if not cfg:
        return None
    if cfg.exec.sandbox_backend != "none":
        logger.info(
            "code-flow: tools.sandbox.backend is {!r}; the host's exec keeps the shell", cfg.exec.sandbox_backend
        )
        return None
    home = Path(ctx.services.workspace)
    executor = CodeExecutor(max_timeout=cfg.exec.max_timeout, spill_dir=home / SPILL_SUBDIR)
    return CodeExecTool(
        timeout=cfg.exec.timeout,
        working_dir=str(home),
        restrict_to_workspace=cfg.restrict_to_workspace,
        path_append=cfg.exec.path_append,
        executor=executor,
        extra_allowed_dirs=(home,),
        max_timeout=cfg.exec.max_timeout,
    )


def make_todo(ctx: "PluginContext") -> TodoTool | None:
    """The checklist, on the store the flow hook binds per session: one store
    per Agent home, so the hook factory and this one meet on the same object."""
    return TodoTool(STORES.for_home(ctx.services.workspace)) if _face(ctx, "todo") else None


__all__ = [
    "make_edit_file",
    "make_exec",
    "make_glob",
    "make_list_dir",
    "make_read_file",
    "make_todo",
    "make_write_file",
]
