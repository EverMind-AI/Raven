# -*- coding: utf-8 -*-
"""A tool that delivers workspace files to the user as downloadable
artifacts."""
import mimetypes
import os
from typing import Any, AsyncGenerator
from urllib.parse import quote

from ...message import TextBlock, ToolResultState
from ...permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from ...tool import BackendBase, LocalBackend, ToolBase, ToolChunk


def _escapes_via_symlink(
    backend: BackendBase,
    root: str,
    abs_path: str,
) -> bool:
    """Return ``True`` if ``abs_path`` escapes ``root`` via a symlink.

    Logical (``..``) containment cannot see through symlinks. Only the
    host-local backend (:class:`LocalBackend`) reads the host filesystem
    and thus follows symlinks off the workspace; sandbox backends confine
    reads to their container, so logical containment already suffices
    there. For the local case, resolve symlinks with ``os.path.realpath``
    and require the result to stay under the real workspace root.

    Args:
        backend (`BackendBase`):
            The workspace backend performing the file I/O.
        root (`str`):
            The workspace root directory.
        abs_path (`str`):
            The already-normalized absolute path to check.

    Returns:
        `bool`:
            ``True`` if the resolved real path escapes the workspace root.
    """
    if not isinstance(backend, LocalBackend):
        return False
    real_root = os.path.realpath(root)
    real = os.path.realpath(abs_path)
    try:
        return os.path.commonpath([real_root, real]) != real_root
    except ValueError:
        return True


class DeliverFiles(ToolBase):
    """Deliver one or more workspace files to the user as downloadable
    deliverables.

    The web UI renders the delivered files as a card with one-click
    download and collects them in a Deliverables panel. Use this when you
    have produced final output files (reports, datasets, figures, ...) that
    the user should receive. Paths are relative to your workspace.
    """

    name: str = "DeliverFiles"
    description: str = (
        "Deliver one or more workspace files to the user as downloadable "
        "deliverables. The web UI shows them as a card with one-click "
        "download and collects them in a Deliverables panel. Provide "
        "workspace-relative file paths; optionally give each a "
        "human-friendly title and a one-line description."
    )
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "minItems": 1,
                "description": "The files to deliver to the user.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Workspace-relative path of the file to "
                                "deliver."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": (
                                "Optional human-friendly title shown in "
                                "the UI."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "Optional one-line note about the file."
                            ),
                        },
                    },
                    "required": ["path"],
                },
            },
            "message": {
                "type": "string",
                "description": (
                    "Optional note accompanying the whole delivery."
                ),
            },
        },
        "required": ["files"],
    }

    def __init__(
        self,
        storage: Any,
        workspace_manager: Any,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> None:
        """Initialize the file-delivery tool.

        Args:
            storage (`Any`):
                The application storage backend, used to resolve the
                session's workspace id.
            workspace_manager (`Any`):
                The workspace manager, used to resolve the session's
                workspace backend + root directory.
            user_id (`str`):
                The id of the user owning the session.
            agent_id (`str`):
                The id of the agent owning the session.
            session_id (`str`):
                The id of the current session.
        """
        super().__init__()
        self._storage = storage
        self._workspace_manager = workspace_manager
        self._user_id = user_id
        self._agent_id = agent_id
        self._session_id = session_id

    async def _resolve(self) -> tuple[BackendBase | None, str | None]:
        """Resolve the session's workspace backend + root directory.

        Returns:
            `tuple[BackendBase | None, str | None]`:
                The `(backend, workdir)` pair, or `(None, None)` if the
                workspace cannot be resolved.
        """
        try:
            session = await self._storage.get_session(
                self._user_id,
                self._agent_id,
                self._session_id,
            )
            workspace_id = session.config.workspace_id if session else None
            work_dir = session.config.work_dir if session else None
            workspace = await self._workspace_manager.get_workspace(
                self._user_id,
                self._agent_id,
                self._session_id,
                workspace_id,
                workdir=work_dir,
            )
            return workspace.get_backend(), workspace.workdir
        except Exception:  # noqa: BLE001  # pragma: no cover - defensive
            return None, None

    async def _validate(
        self,
        backend: BackendBase,
        root: str,
        entry: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Validate one file entry against the workspace root.

        Args:
            backend (`BackendBase`):
                The workspace backend used for path + file operations.
            root (`str`):
                The workspace root directory (backend-side path).
            entry (`dict[str, Any]`):
                A `{path, title?, description?}` entry.

        Returns:
            `tuple[dict[str, Any] | None, dict[str, Any] | None]`:
                A `(valid, invalid)` pair; exactly one element is set.
        """
        path = entry.get("path", "")
        if not isinstance(path, str) or not path:
            return None, {"path": path, "reason": "missing path"}
        if backend.isabs(path):
            return None, {
                "path": path,
                "reason": "absolute paths are not allowed",
            }
        norm = backend.normpath(path)
        if norm == ".." or norm.startswith("../"):
            return None, {
                "path": path,
                "reason": "path escapes the workspace",
            }
        abs_path = backend.abspath(norm, cwd=root)
        try:
            if not await backend.file_exists(abs_path) or await backend.is_dir(
                abs_path,
            ):
                return None, {"path": path, "reason": "not a file"}
            if _escapes_via_symlink(backend, root, abs_path):
                return None, {
                    "path": path,
                    "reason": "path escapes the workspace",
                }
            size = len(await backend.read_file(abs_path))
        except Exception as exc:  # noqa: BLE001
            return None, {"path": path, "reason": f"unreadable: {exc}"}
        name = backend.basename(abs_path)
        media_type = (
            mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        download_path = (
            f"/sessions/{quote(self._session_id)}/files/download"
            f"?path={quote(norm)}&agent_id={quote(self._agent_id)}"
        )
        return (
            {
                "path": norm,
                "name": name,
                "title": entry.get("title"),
                "description": entry.get("description"),
                "size": size,
                "media_type": media_type,
                "download_path": download_path,
            },
            None,
        )

    async def call(  # type: ignore[override]
        self,
        files: list[dict[str, Any]],
        message: str | None = None,
    ) -> AsyncGenerator[ToolChunk, None]:
        """Validate and deliver the given files to the user.

        Args:
            files (`list[dict[str, Any]]`):
                The `{path, title?, description?}` entries to deliver.
            message (`str | None`, defaults to `None`):
                An optional note accompanying the whole delivery.

        Returns:
            `AsyncGenerator[ToolChunk, None]`:
                A single terminal chunk with a text summary and the
                `ravenx_delivery` manifest in its metadata.
        """
        backend, root = await self._resolve()
        if backend is None or root is None:
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "File delivery failed: could not resolve the "
                            "session workspace."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
                metadata={
                    "ravenx_delivery": {
                        "message": message,
                        "files": [],
                        "invalid": [],
                    },
                },
            )
            return

        valid: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in files:
            ok, bad = await self._validate(backend, root, entry)
            if bad is not None:
                invalid.append(bad)
                continue
            assert ok is not None
            if ok["path"] in seen:
                continue
            seen.add(ok["path"])
            valid.append(ok)

        parts: list[str] = []
        if valid:
            names = ", ".join(item["name"] for item in valid)
            parts.append(
                f"Delivered {len(valid)} file(s) to the user: {names}.",
            )
        if invalid:
            fails = ", ".join(
                f"{item['path']} ({item['reason']})" for item in invalid
            )
            parts.append(f"Could not deliver: {fails}.")
        summary = " ".join(parts) or "No files delivered."

        yield ToolChunk(
            content=[TextBlock(text=summary)],
            state=(
                ToolResultState.SUCCESS if valid else ToolResultState.ERROR
            ),
            is_last=True,
            metadata={
                "ravenx_delivery": {
                    "message": message,
                    "files": valid,
                    "invalid": invalid,
                },
            },
        )

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """Auto-run: file delivery is read-only and workspace-confined.

        Args:
            tool_input (`dict[str, Any]`):
                The tool input for this invocation.
            context (`PermissionContext`):
                The permission context.

        Returns:
            `PermissionDecision`:
                An ALLOW decision.
        """
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="File delivery runs automatically.",
        )
