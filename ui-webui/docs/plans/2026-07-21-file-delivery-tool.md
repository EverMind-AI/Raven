# File Delivery Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the main (leader) agent a `DeliverFiles` tool that hands workspace files to the user, with a web UI that renders them as a downloadable card plus an aggregated Deliverables panel.

**Architecture:** The tool returns a text summary plus a structured `ravenx_delivery` manifest on `ToolResponse.metadata`, which rides the existing `ToolResultEndEvent.metadata` rail to the frontend `tool_result` block. A new session-scoped FastAPI route serves workspace-confined files (realpath-containment guard). The frontend reads the manifest in a dedicated tool renderer and a Deliverables panel; downloads go through an authenticated (`X-User-ID`) fetch → Blob → save helper.

**Tech Stack:** Backend — Python 3.11+, FastAPI, pytest, `agentscope` framework primitives (`ToolBase`, `ToolChunk`, `TextBlock`, `PermissionDecision`). Frontend — React 19, Vite, TypeScript, Tailwind v4 + shadcn/ui, `sonner` toasts, `@agentscope-ai/agentscope` SDK types.

**Design spec:** `docs/superpowers/specs/2026-07-21-file-delivery-tool-design.md`

## Global Constraints

- **Encapsulation:** internal files/classes/functions are `_`-prefixed; public API is exposed only through `__init__.py`. `DeliverFiles` is exported from `src/agentscope/app/_tool/__init__.py`.
- **Lazy imports:** third-party libs imported at point of use. To avoid an `agentscope.subagent` → `agentscope.app` import cycle, import `DeliverFiles` **inside** the factory function, not at module top.
- **Docstrings:** English only, strict `Args:`/`Returns:` template with backtick-typed params.
- **Black line length 79**; flake8, pylint, mypy all run in pre-commit. Run `pre-commit run --all-files` before finishing.
- **Tests:** compare **whole data structures**, not field-by-field; use `AnyString`/`AnyValue` from `tests/utils.py` for nondeterministic fields. Test files are `tests/*_test.py`.
- **Commits:** Conventional Commits — `feat/fix/test(scope): description`. End every commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Manifest stays byte-free:** the tool delivers paths + short strings only (never file bytes), so the result never trips context offload (which would strip `metadata` from history).
- **Frontend has no unit-test runner** (no vitest/jest in `examples/web_ui/frontend/package.json`). Per spec §9, frontend tasks are gated by `pnpm build` (TypeScript typecheck) + `pnpm lint`; behavioral verification is the end-to-end smoke in Task 9. Bootstrapping Vitest is out of scope (possible follow-up).
- **Branch:** all work on `feat/file-delivery-tool` (already created).

---

## Design Amendment (2026-07-21) — backend-abstracted file I/O

**Supersedes the inline code in Task 1 (Steps 1 & 3) and Task 2 (Steps 1, 3, 4).** Per the Task 1 review + user decision, the tool and download route must NOT use the host `os` module / `FileResponse`-from-path (correct only for `LocalWorkspaceManager`). They resolve existence/size and read bytes through `workspace.get_backend()` (`BackendBase`), which works for every backend (Local/Docker/E2B/K8s/Daytona/OpenSandbox). Containment is *logical* (reject absolute + normalized `..`-escape) since `BackendBase` has no `realpath`; the symlink trade-off is accepted and documented (spec §11.7). Also fixes review finding F2 (always emit the `ravenx_delivery` manifest, even on resolution failure).

`BackendBase` API used (from `src/agentscope/tool/_builtin/_backend.py`): `isabs(path)`, `normpath(path)`, `abspath(path, *, cwd)`, `basename(path)`, `async file_exists(path)->bool`, `async is_dir(path)->bool`, `async read_file(path)->bytes`. There is no size primitive → derive `size = len(read_file(...))` (also confirms readability).

### Amended Task 1 — `src/agentscope/app/_tool/_deliver_files.py` (full file)

```python
# -*- coding: utf-8 -*-
"""A tool that delivers workspace files to the user as downloadable
artifacts."""
import mimetypes
from typing import Any, AsyncGenerator
from urllib.parse import quote

from ...message import TextBlock, ToolResultState
from ...permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from ...tool import BackendBase, ToolBase, ToolChunk


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
```

### Amended Task 1 — `tests/deliver_files_tool_test.py` (full file)

The fakes return a **real `LocalBackend`** so tests exercise the real backend I/O path against a temp dir; the `_RaisingStorage` case exercises the resolution-failure branch.

```python
# -*- coding: utf-8 -*-
# pylint: disable=unused-argument
"""Tests for the DeliverFiles tool."""

import asyncio
import os
import tempfile
import unittest
from typing import AsyncGenerator

from agentscope.app._tool import DeliverFiles
from agentscope.message import ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext
from agentscope.tool import LocalBackend, ToolChunk


class _FakeConfig:
    """Minimal session config carrying a workspace id."""

    def __init__(self, workspace_id: str | None, work_dir: str | None) -> None:
        self.workspace_id = workspace_id
        self.work_dir = work_dir


class _FakeSession:
    """Minimal session record with a ``config`` attribute."""

    def __init__(self, workspace_id: str | None, work_dir: str | None) -> None:
        self.config = _FakeConfig(workspace_id, work_dir)


class _FakeStorage:
    """Storage returning one fixed session."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def get_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> _FakeSession:
        """Return the fixed session."""
        return self._session


class _RaisingStorage:
    """Storage whose session lookup fails (unresolvable workspace)."""

    async def get_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> _FakeSession:
        """Raise to exercise the resolution-failure branch."""
        raise RuntimeError("boom")


class _FakeWorkspace:
    """Workspace exposing a real LocalBackend + a fixed workdir."""

    def __init__(self, workdir: str) -> None:
        self.workdir = workdir

    def get_backend(self) -> LocalBackend:
        """Return a real local backend (host I/O against the workdir)."""
        return LocalBackend()


class _FakeWorkspaceManager:
    """Workspace manager returning a fixed workspace."""

    def __init__(self, workdir: str) -> None:
        self._workdir = workdir

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
        *,
        workdir: str | None = None,
    ) -> _FakeWorkspace:
        """Return the fixed workspace."""
        return _FakeWorkspace(self._workdir)


async def _collect(
    gen: AsyncGenerator[ToolChunk, None],
) -> list[ToolChunk]:
    """Drain an async generator to a list."""
    return [chunk async for chunk in gen]


def _make_tool(workdir: str, storage: object | None = None) -> DeliverFiles:
    """Build a DeliverFiles tool rooted at ``workdir``."""
    return DeliverFiles(
        storage=storage or _FakeStorage(_FakeSession("ws1", None)),
        workspace_manager=_FakeWorkspaceManager(workdir),
        user_id="u1",
        agent_id="a1",
        session_id="s1",
    )


class DeliverFilesTest(unittest.TestCase):
    """The tool validates paths and builds a delivery manifest."""

    def test_delivers_valid_files(self) -> None:
        """Valid files produce a SUCCESS manifest with derived fields."""
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "report.pdf"), "wb") as fh:
                fh.write(b"PDF12")
            os.mkdir(os.path.join(root, "out"))
            with open(os.path.join(root, "out", "data.csv"), "wb") as fh:
                fh.write(b"a,b\n1,2\n")
            chunks = asyncio.run(
                _collect(
                    _make_tool(root).call(
                        files=[
                            {"path": "report.pdf", "title": "Q3 Report"},
                            {"path": "out/data.csv"},
                        ],
                        message="Here you go",
                    ),
                ),
            )
        last = chunks[-1]
        self.assertEqual(last.state, ToolResultState.SUCCESS)
        self.assertEqual(
            last.metadata,
            {
                "ravenx_delivery": {
                    "message": "Here you go",
                    "files": [
                        {
                            "path": "report.pdf",
                            "name": "report.pdf",
                            "title": "Q3 Report",
                            "description": None,
                            "size": 5,
                            "media_type": "application/pdf",
                            "download_path": (
                                "/sessions/s1/files/download"
                                "?path=report.pdf&agent_id=a1"
                            ),
                        },
                        {
                            "path": "out/data.csv",
                            "name": "data.csv",
                            "title": None,
                            "description": None,
                            "size": 8,
                            "media_type": "text/csv",
                            "download_path": (
                                "/sessions/s1/files/download"
                                "?path=out/data.csv&agent_id=a1"
                            ),
                        },
                    ],
                    "invalid": [],
                },
            },
        )

    def test_rejects_traversal_and_absolute(self) -> None:
        """`..` escapes and absolute paths land in ``invalid``."""
        with tempfile.TemporaryDirectory() as root:
            chunks = asyncio.run(
                _collect(
                    _make_tool(root).call(
                        files=[
                            {"path": "../secret.txt"},
                            {"path": "/etc/hostname"},
                        ],
                    ),
                ),
            )
        last = chunks[-1]
        self.assertEqual(last.state, ToolResultState.ERROR)
        self.assertEqual(
            last.metadata,
            {
                "ravenx_delivery": {
                    "message": None,
                    "files": [],
                    "invalid": [
                        {
                            "path": "../secret.txt",
                            "reason": "path escapes the workspace",
                        },
                        {
                            "path": "/etc/hostname",
                            "reason": "absolute paths are not allowed",
                        },
                    ],
                },
            },
        )

    def test_missing_file_is_invalid(self) -> None:
        """A path that does not resolve to a file is invalid."""
        with tempfile.TemporaryDirectory() as root:
            chunks = asyncio.run(
                _collect(_make_tool(root).call(files=[{"path": "nope.txt"}])),
            )
        last = chunks[-1]
        self.assertEqual(last.state, ToolResultState.ERROR)
        self.assertEqual(
            last.metadata["ravenx_delivery"]["invalid"],
            [{"path": "nope.txt", "reason": "not a file"}],
        )

    def test_directory_is_invalid(self) -> None:
        """A directory path is rejected as not-a-file."""
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "sub"))
            chunks = asyncio.run(
                _collect(_make_tool(root).call(files=[{"path": "sub"}])),
            )
        self.assertEqual(
            chunks[-1].metadata["ravenx_delivery"]["invalid"],
            [{"path": "sub", "reason": "not a file"}],
        )

    def test_unresolvable_workspace_errors(self) -> None:
        """A failed workspace resolution yields an empty-manifest ERROR."""
        tool = DeliverFiles(
            storage=_RaisingStorage(),
            workspace_manager=_FakeWorkspaceManager("/nope"),
            user_id="u1",
            agent_id="a1",
            session_id="s1",
        )
        chunks = asyncio.run(_collect(tool.call(files=[{"path": "x.txt"}])))
        last = chunks[-1]
        self.assertEqual(last.state, ToolResultState.ERROR)
        self.assertEqual(
            last.metadata,
            {
                "ravenx_delivery": {
                    "message": None,
                    "files": [],
                    "invalid": [],
                },
            },
        )

    def test_check_permissions_allows(self) -> None:
        """Delivery is auto-allowed (read-only, workspace-confined)."""
        decision = asyncio.run(
            _make_tool("/tmp").check_permissions({}, PermissionContext()),
        )
        self.assertEqual(decision.behavior, PermissionBehavior.ALLOW)
```

### Amended Task 2 — routes in `src/agentscope/app/_router/_session.py`

Imports to add (Step 3): `import io`, `import mimetypes`, `import zipfile` (NOT `os`); `from fastapi.responses import Response, StreamingResponse` (replace the existing `StreamingResponse`-only line; `FileResponse` is NOT used); and `from ...tool import BackendBase`.

Helpers + handlers (Step 4) — backend-abstracted, logical containment, in-memory `Response`:

```python
async def _resolve_session_backend(
    user_id: str,
    agent_id: str,
    session_id: str,
    storage: "StorageBase",
    workspace_manager: "WorkspaceManagerBase",
) -> tuple[BackendBase, str]:
    """Resolve a session's workspace backend + root directory.

    Args:
        user_id (`str`):
            The requesting user's id (from the ``X-User-ID`` header).
        agent_id (`str`):
            The agent the session belongs to.
        session_id (`str`):
            The session id.
        storage (`StorageBase`):
            The application storage backend.
        workspace_manager (`WorkspaceManagerBase`):
            The application workspace manager.

    Returns:
        `tuple[BackendBase, str]`:
            The workspace's `(backend, workdir)`.
    """
    session = await storage.get_session(user_id, agent_id, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found.",
        )
    workspace = await workspace_manager.get_workspace(
        user_id,
        agent_id,
        session_id,
        session.config.workspace_id,
        workdir=session.config.work_dir,
    )
    return workspace.get_backend(), workspace.workdir


def _resolve_safe_path(backend: BackendBase, root: str, rel: str) -> str:
    """Resolve a workspace-relative path, rejecting escapes (logical).

    Args:
        backend (`BackendBase`):
            The workspace backend (provides path semantics).
        root (`str`):
            The workspace root directory (backend-side path).
        rel (`str`):
            The candidate workspace-relative path.

    Returns:
        `str`:
            The absolute backend path of the file inside the workspace.
    """
    if not rel:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A file path is required.",
        )
    if backend.isabs(rel):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Absolute paths are not allowed.",
        )
    norm = backend.normpath(rel)
    if norm == ".." or norm.startswith("../"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="File path escapes the workspace.",
        )
    return backend.abspath(norm, cwd=root)


@session_router.get(
    "/{session_id}/files/download",
    summary="Download a workspace-confined file",
)
async def download_file(
    session_id: str,
    path: str = Query(description="Workspace-relative path of the file."),
    agent_id: str = Query(description="Agent the session belongs to."),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
) -> Response:
    """Serve a single delivered file as a download attachment.

    Args:
        session_id (`str`):
            The session whose workspace holds the file.
        path (`str`):
            The workspace-relative path of the file.
        agent_id (`str`):
            The agent the session belongs to.
        user_id (`str`):
            The requesting user's id.
        storage (`StorageBase`):
            The application storage backend.
        workspace_manager (`WorkspaceManagerBase`):
            The application workspace manager.

    Returns:
        `Response`:
            The file bytes with an attachment ``Content-Disposition``.
    """
    backend, root = await _resolve_session_backend(
        user_id,
        agent_id,
        session_id,
        storage,
        workspace_manager,
    )
    abs_path = _resolve_safe_path(backend, root, path)
    if not await backend.file_exists(abs_path) or await backend.is_dir(
        abs_path,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found.",
        )
    data = await backend.read_file(abs_path)
    name = backend.basename(abs_path)
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
        },
    )


@session_router.get(
    "/{session_id}/files/download-archive",
    summary="Download several workspace-confined files as a zip",
)
async def download_archive(
    session_id: str,
    paths: list[str] = Query(
        default=[],
        description="Workspace-relative paths to include in the archive.",
    ),
    agent_id: str = Query(description="Agent the session belongs to."),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
) -> Response:
    """Zip several delivered files and serve them as one download.

    Args:
        session_id (`str`):
            The session whose workspace holds the files.
        paths (`list[str]`):
            The workspace-relative paths to include.
        agent_id (`str`):
            The agent the session belongs to.
        user_id (`str`):
            The requesting user's id.
        storage (`StorageBase`):
            The application storage backend.
        workspace_manager (`WorkspaceManagerBase`):
            The application workspace manager.

    Returns:
        `Response`:
            A ``application/zip`` attachment; missing/invalid paths are
            skipped, and a 404 is raised if none remain.
    """
    backend, root = await _resolve_session_backend(
        user_id,
        agent_id,
        session_id,
        storage,
        workspace_manager,
    )
    buffer = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in paths:
            try:
                abs_path = _resolve_safe_path(backend, root, rel)
            except HTTPException:
                continue
            if not await backend.file_exists(abs_path) or await backend.is_dir(
                abs_path,
            ):
                continue
            try:
                data = await backend.read_file(abs_path)
            except Exception:  # noqa: BLE001
                continue
            archive.writestr(backend.basename(abs_path), data)
            added += 1
    if added == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No deliverable files found.",
        )
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="deliverables.zip"',
        },
    )
```

### Amended Task 2 — `tests/session_files_download_test.py` (full file)

The fake workspace's `get_backend()` returns a real `LocalBackend`, so the routes exercise the real backend I/O path against a temp dir. `test_symlink_escape_is_forbidden` passes via the `_escapes_via_symlink` recheck (Symlink-escape hardening below).

```python
# -*- coding: utf-8 -*-
# pylint: disable=unused-argument
"""The session file-download routes serve workspace-confined files."""
import io
import os
import tempfile
import unittest
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentscope.app._router._session import session_router
from agentscope.tool import LocalBackend

HEADERS = {"X-User-ID": "u1"}
_DEFAULT = object()


class _FakeConfig:
    """Session config with a workspace id + optional work_dir."""

    def __init__(self, workspace_id: str, work_dir: str | None) -> None:
        self.workspace_id = workspace_id
        self.work_dir = work_dir


class _FakeSession:
    """Session record with a ``config`` attribute."""

    def __init__(self, workspace_id: str, work_dir: str | None) -> None:
        self.config = _FakeConfig(workspace_id, work_dir)


class _FakeStorage:
    """Storage returning a fixed session (or None)."""

    def __init__(self, session: _FakeSession | None) -> None:
        self._session = session

    async def get_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> _FakeSession | None:
        """Return the fixed session."""
        return self._session


class _FakeWorkspace:
    """Workspace exposing a real LocalBackend + a fixed workdir."""

    def __init__(self, workdir: str) -> None:
        self.workdir = workdir

    def get_backend(self) -> LocalBackend:
        """Return a real local backend (host I/O against the workdir)."""
        return LocalBackend()


class _FakeWorkspaceManager:
    """Workspace manager returning a fixed workspace."""

    def __init__(self, workdir: str) -> None:
        self._workdir = workdir

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
        *,
        workdir: str | None = None,
    ) -> _FakeWorkspace:
        """Return the fixed workspace."""
        return _FakeWorkspace(self._workdir)


def _client(root: str, session: object = _DEFAULT) -> TestClient:
    """Build a TestClient for the session router rooted at ``root``.

    Pass ``session=None`` to simulate an unknown session.
    """
    app = FastAPI()
    sess = _FakeSession("ws1", None) if session is _DEFAULT else session
    app.state.storage = _FakeStorage(sess)  # type: ignore[arg-type]
    app.state.workspace_manager = _FakeWorkspaceManager(root)
    app.include_router(session_router)
    return TestClient(app)


class DownloadFileTest(unittest.TestCase):
    """GET /sessions/{id}/files/download."""

    def test_downloads_workspace_file(self) -> None:
        """A valid workspace-relative path returns the file bytes."""
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "report.pdf"), "wb") as fh:
                fh.write(b"PDFDATA")
            resp = _client(root).get(
                "/sessions/s1/files/download",
                params={"path": "report.pdf", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"PDFDATA")
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertIn("report.pdf", resp.headers["content-disposition"])

    def test_traversal_is_forbidden(self) -> None:
        """A `..` escape returns 403."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(root).get(
                "/sessions/s1/files/download",
                params={"path": "../secret.txt", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 403)

    def test_absolute_path_is_forbidden(self) -> None:
        """An absolute path returns 403."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(root).get(
                "/sessions/s1/files/download",
                params={"path": "/etc/hostname", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 403)

    def test_symlink_escape_is_forbidden(self) -> None:
        """A symlink inside the workspace pointing out returns 403."""
        with tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as outside:
            target = os.path.join(outside, "secret.txt")
            with open(target, "wb") as fh:
                fh.write(b"secret")
            os.symlink(target, os.path.join(root, "link.txt"))
            resp = _client(root).get(
                "/sessions/s1/files/download",
                params={"path": "link.txt", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 403)

    def test_missing_file_is_404(self) -> None:
        """A path with no file returns 404."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(root).get(
                "/sessions/s1/files/download",
                params={"path": "nope.txt", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 404)

    def test_unknown_session_is_404(self) -> None:
        """A session that does not exist returns 404."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(root, session=None).get(
                "/sessions/s1/files/download",
                params={"path": "x.txt", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 404)

    def test_missing_user_header_is_401(self) -> None:
        """A request without X-User-ID returns 401."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(root).get(
                "/sessions/s1/files/download",
                params={"path": "x.txt", "agent_id": "a1"},
            )
        self.assertEqual(resp.status_code, 401)


class DownloadArchiveTest(unittest.TestCase):
    """GET /sessions/{id}/files/download-archive."""

    def test_zips_requested_files(self) -> None:
        """Requested files are zipped; missing ones are skipped."""
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "a.txt"), "wb") as fh:
                fh.write(b"AAA")
            with open(os.path.join(root, "b.txt"), "wb") as fh:
                fh.write(b"BBB")
            resp = _client(root).get(
                "/sessions/s1/files/download-archive",
                params=[
                    ("paths", "a.txt"),
                    ("paths", "b.txt"),
                    ("paths", "gone.txt"),
                    ("agent_id", "a1"),
                ],
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            self.assertEqual(sorted(zf.namelist()), ["a.txt", "b.txt"])
            self.assertEqual(zf.read("a.txt"), b"AAA")

    def test_archive_all_missing_is_404(self) -> None:
        """When no requested file exists the archive is 404."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(root).get(
                "/sessions/s1/files/download-archive",
                params=[("paths", "gone.txt"), ("agent_id", "a1")],
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 404)
```

### Symlink-escape hardening (2026-07-21)

Logical `..` containment cannot see through symlinks; the `os`-based design's `realpath` check could. To keep the backend abstraction *and* the symlink defense, add a shared module-level helper in `src/agentscope/app/_tool/_deliver_files.py` and reuse it from the download route. It engages only for the host-local backend (`LocalBackend`) — sandbox backends confine symlinks to their container.

Add to `_deliver_files.py`: `import os` at the top, and `LocalBackend` to the existing `from ...tool import ...` line (→ `from ...tool import BackendBase, LocalBackend, ToolBase, ToolChunk`). Then this module-level helper:

```python
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
```

In the tool's `_validate`, after the `file_exists`/`is_dir` check passes and **before** `read_file`, add:

```python
        if _escapes_via_symlink(backend, root, abs_path):
            return None, {
                "path": path,
                "reason": "path escapes the workspace",
            }
```

In the download route (`_session.py`), import the helper — `from .._tool._deliver_files import _escapes_via_symlink` — and in `_resolve_safe_path`, after computing `abs = backend.abspath(norm, cwd=root)` and **before** returning it, add:

```python
    if _escapes_via_symlink(backend, root, abs):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="File path escapes the workspace.",
        )
```

Tests to add/keep: a tool test `test_rejects_symlink_escape` (create a file outside `root`, `os.symlink` it to a name inside `root`, deliver that name → `invalid` with `reason == "path escapes the workspace"`) and the route `test_symlink_escape_is_forbidden` (same setup → **403**).

---

### Task 1: `DeliverFiles` tool

**Files:**
- Create: `src/agentscope/app/_tool/_deliver_files.py`
- Modify: `src/agentscope/app/_tool/__init__.py`
- Test: `tests/deliver_files_tool_test.py`

**Interfaces:**
- Produces: `DeliverFiles(storage, workspace_manager, user_id, agent_id, session_id)` — a `ToolBase` with `name="DeliverFiles"`, `is_read_only=True`, `is_concurrency_safe=True`. `call(files: list[dict], message: str | None = None)` yields one terminal `ToolChunk` with `metadata={"ravenx_delivery": {"message", "files": [{path, name, title, description, size, media_type, download_path}], "invalid": [{path, reason}]}}`. `download_path` format: `/sessions/{session_id}/files/download?path={rel}&agent_id={agent_id}` (URL-quoted). Consumed by Task 3 (factory) and by the frontend renderer/panel (Tasks 4–8) which read the manifest shape verbatim.

- [ ] **Step 1: Write the failing test**

Create `tests/deliver_files_tool_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the DeliverFiles tool."""

import asyncio
import os
import tempfile
import unittest
from typing import Any, AsyncGenerator

from agentscope.app._tool import DeliverFiles
from agentscope.message import ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext
from agentscope.tool import ToolChunk


class _FakeConfig:
    """Minimal session config carrying a workspace id."""

    def __init__(self, workspace_id: str | None, work_dir: str | None) -> None:
        self.workspace_id = workspace_id
        self.work_dir = work_dir


class _FakeSession:
    """Minimal session record with a ``config`` attribute."""

    def __init__(self, workspace_id: str | None, work_dir: str | None) -> None:
        self.config = _FakeConfig(workspace_id, work_dir)


class _FakeStorage:
    """Storage returning one fixed session."""

    def __init__(self, session: _FakeSession | None) -> None:
        self._session = session

    async def get_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> _FakeSession | None:
        """Return the fixed session."""
        return self._session


class _FakeWorkspace:
    """Workspace exposing a fixed on-disk workdir."""

    def __init__(self, workdir: str) -> None:
        self.workdir = workdir


class _FakeWorkspaceManager:
    """Workspace manager returning a fixed workspace."""

    def __init__(self, workdir: str) -> None:
        self._workdir = workdir

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
        *,
        workdir: str | None = None,
    ) -> _FakeWorkspace:
        """Return the fixed workspace."""
        return _FakeWorkspace(self._workdir)


async def _collect(
    gen: AsyncGenerator[ToolChunk, None],
) -> list[ToolChunk]:
    """Drain an async generator to a list."""
    return [chunk async for chunk in gen]


def _make_tool(workdir: str) -> DeliverFiles:
    """Build a DeliverFiles tool rooted at ``workdir``."""
    return DeliverFiles(
        storage=_FakeStorage(_FakeSession("ws1", None)),
        workspace_manager=_FakeWorkspaceManager(workdir),
        user_id="u1",
        agent_id="a1",
        session_id="s1",
    )


class DeliverFilesTest(unittest.TestCase):
    """The tool validates paths and builds a delivery manifest."""

    def test_delivers_valid_files(self) -> None:
        """Valid files produce a SUCCESS manifest with derived fields."""
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "report.pdf"), "wb") as fh:
                fh.write(b"PDF12")
            os.mkdir(os.path.join(root, "out"))
            with open(os.path.join(root, "out", "data.csv"), "wb") as fh:
                fh.write(b"a,b\n1,2\n")
            tool = _make_tool(os.path.realpath(root))
            chunks = asyncio.run(
                _collect(
                    tool.call(
                        files=[
                            {"path": "report.pdf", "title": "Q3 Report"},
                            {"path": "out/data.csv"},
                        ],
                        message="Here you go",
                    ),
                ),
            )
        last = chunks[-1]
        self.assertEqual(last.state, ToolResultState.SUCCESS)
        self.assertEqual(
            last.metadata,
            {
                "ravenx_delivery": {
                    "message": "Here you go",
                    "files": [
                        {
                            "path": "report.pdf",
                            "name": "report.pdf",
                            "title": "Q3 Report",
                            "description": None,
                            "size": 5,
                            "media_type": "application/pdf",
                            "download_path": (
                                "/sessions/s1/files/download"
                                "?path=report.pdf&agent_id=a1"
                            ),
                        },
                        {
                            "path": "out/data.csv",
                            "name": "data.csv",
                            "title": None,
                            "description": None,
                            "size": 8,
                            "media_type": "text/csv",
                            "download_path": (
                                "/sessions/s1/files/download"
                                "?path=out/data.csv&agent_id=a1"
                            ),
                        },
                    ],
                    "invalid": [],
                },
            },
        )

    def test_rejects_traversal_and_absolute(self) -> None:
        """`..` escapes and absolute paths land in ``invalid``."""
        with tempfile.TemporaryDirectory() as root:
            tool = _make_tool(os.path.realpath(root))
            chunks = asyncio.run(
                _collect(
                    tool.call(
                        files=[
                            {"path": "../secret.txt"},
                            {"path": "/etc/hostname"},
                        ],
                    ),
                ),
            )
        last = chunks[-1]
        self.assertEqual(last.state, ToolResultState.ERROR)
        self.assertEqual(
            last.metadata["ravenx_delivery"]["invalid"],
            [
                {
                    "path": "../secret.txt",
                    "reason": "path escapes the workspace",
                },
                {
                    "path": "/etc/hostname",
                    "reason": "absolute paths are not allowed",
                },
            ],
        )
        self.assertEqual(last.metadata["ravenx_delivery"]["files"], [])

    def test_missing_file_is_invalid(self) -> None:
        """A path that does not resolve to a file is invalid."""
        with tempfile.TemporaryDirectory() as root:
            tool = _make_tool(os.path.realpath(root))
            chunks = asyncio.run(
                _collect(tool.call(files=[{"path": "nope.txt"}])),
            )
        last = chunks[-1]
        self.assertEqual(last.state, ToolResultState.ERROR)
        self.assertEqual(
            last.metadata["ravenx_delivery"]["invalid"],
            [{"path": "nope.txt", "reason": "not a file"}],
        )

    def test_unresolvable_workspace_errors(self) -> None:
        """A missing session yields a terminal ERROR chunk."""
        tool = DeliverFiles(
            storage=_FakeStorage(None),
            workspace_manager=_FakeWorkspaceManager("/nope"),
            user_id="u1",
            agent_id="a1",
            session_id="s1",
        )
        # session is None -> get_workspace still returns a workspace, so
        # resolution succeeds against "/nope"; a nonexistent path is invalid.
        chunks = asyncio.run(
            _collect(tool.call(files=[{"path": "x.txt"}])),
        )
        self.assertEqual(chunks[-1].state, ToolResultState.ERROR)

    def test_check_permissions_allows(self) -> None:
        """Delivery is auto-allowed (read-only, workspace-confined)."""
        tool = _make_tool("/tmp")
        decision = asyncio.run(
            tool.check_permissions({}, PermissionContext()),
        )
        self.assertEqual(decision.behavior, PermissionBehavior.ALLOW)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/deliver_files_tool_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'DeliverFiles' from 'agentscope.app._tool'`

- [ ] **Step 3: Create the tool**

Create `src/agentscope/app/_tool/_deliver_files.py`:

```python
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
from ...tool import ToolBase, ToolChunk


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
                on-disk workspace directory.
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

    async def _resolve_root(self) -> str | None:
        """Resolve the absolute workspace root directory for the session.

        Returns:
            `str | None`:
                The realpath of the workspace directory, or `None` if it
                cannot be resolved.
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
            return os.path.realpath(workspace.workdir)
        except Exception:  # noqa: BLE001  # pragma: no cover - defensive
            return None

    def _validate(
        self,
        root: str,
        entry: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Validate one file entry against the workspace root.

        Args:
            root (`str`):
                The realpath of the workspace root.
            entry (`dict[str, Any]`):
                A `{path, title?, description?}` entry.

        Returns:
            `tuple[dict[str, Any] | None, dict[str, Any] | None]`:
                A `(valid, invalid)` pair; exactly one element is set.
        """
        path = entry.get("path", "")
        if not isinstance(path, str) or not path:
            return None, {"path": path, "reason": "missing path"}
        if os.path.isabs(path):
            return None, {
                "path": path,
                "reason": "absolute paths are not allowed",
            }
        abs_path = os.path.realpath(os.path.join(root, path))
        try:
            escapes = os.path.commonpath([root, abs_path]) != root
        except ValueError:
            escapes = True
        if escapes:
            return None, {
                "path": path,
                "reason": "path escapes the workspace",
            }
        if not os.path.isfile(abs_path):
            return None, {"path": path, "reason": "not a file"}
        rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
        name = os.path.basename(abs_path)
        media_type = (
            mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        download_path = (
            f"/sessions/{quote(self._session_id)}/files/download"
            f"?path={quote(rel)}&agent_id={quote(self._agent_id)}"
        )
        return (
            {
                "path": rel,
                "name": name,
                "title": entry.get("title"),
                "description": entry.get("description"),
                "size": os.path.getsize(abs_path),
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
        root = await self._resolve_root()
        if root is None:
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
            )
            return

        valid: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in files:
            ok, bad = self._validate(root, entry)
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
```

- [ ] **Step 4: Export from the package `__init__.py`**

Modify `src/agentscope/app/_tool/__init__.py` — add the import (alphabetical) and `__all__` entry:

```python
from ._agent_create import AgentCreate, DEFAULT_SUB_AGENT_TEMPLATE
from ._agent_invite import AgentInvite
from ._deliver_files import DeliverFiles
from ._team_create import TeamCreate
from ._team_delete import TeamDelete
from ._team_say import TeamSay

__all__ = [
    "AgentCreate",
    "AgentInvite",
    "DEFAULT_SUB_AGENT_TEMPLATE",
    "DeliverFiles",
    "TeamCreate",
    "TeamDelete",
    "TeamSay",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/deliver_files_tool_test.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/app/_tool/_deliver_files.py \
        src/agentscope/app/_tool/__init__.py \
        tests/deliver_files_tool_test.py
git commit -m "feat(delivery): add DeliverFiles tool with workspace-confined manifest

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Download route(s)

**Files:**
- Modify: `src/agentscope/app/_router/_session.py` (add imports, two module-level helpers, two GET handlers)
- Test: `tests/session_files_download_test.py`

**Interfaces:**
- Produces: `GET /sessions/{session_id}/files/download?path=<rel>&agent_id=<aid>` → `FileResponse` (`Content-Disposition: attachment`). `GET /sessions/{session_id}/files/download-archive?paths=<rel>&paths=<rel>&agent_id=<aid>` → zip `Response`. Guard: absolute/escape → 403, unknown session → 404, missing file → 404, empty path → 400, missing `X-User-ID` → 401. These URLs are exactly what Task 1's `download_path` targets and what the Task 5 helper fetches.

- [ ] **Step 1: Write the failing test**

Create `tests/session_files_download_test.py`:

```python
# -*- coding: utf-8 -*-
# pylint: disable=unused-argument
"""The session file-download routes serve workspace-confined files."""
import io
import os
import tempfile
import unittest
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentscope.app._router._session import session_router

HEADERS = {"X-User-ID": "u1"}


class _FakeConfig:
    """Session config with a workspace id + optional work_dir."""

    def __init__(self, workspace_id: str, work_dir: str | None) -> None:
        self.workspace_id = workspace_id
        self.work_dir = work_dir


class _FakeSession:
    """Session record with a ``config`` attribute."""

    def __init__(self, workspace_id: str, work_dir: str | None) -> None:
        self.config = _FakeConfig(workspace_id, work_dir)


class _FakeStorage:
    """Storage returning a fixed session (or None)."""

    def __init__(self, session: _FakeSession | None) -> None:
        self._session = session

    async def get_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> _FakeSession | None:
        """Return the fixed session."""
        return self._session


class _FakeWorkspace:
    """Workspace exposing a fixed workdir."""

    def __init__(self, workdir: str) -> None:
        self.workdir = workdir


class _FakeWorkspaceManager:
    """Workspace manager returning a fixed workspace."""

    def __init__(self, workdir: str) -> None:
        self._workdir = workdir

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
        *,
        workdir: str | None = None,
    ) -> _FakeWorkspace:
        """Return the fixed workspace."""
        return _FakeWorkspace(self._workdir)


def _client(root: str, session: _FakeSession | None = None) -> TestClient:
    """Build a TestClient for the session router rooted at ``root``."""
    app = FastAPI()
    app.state.storage = _FakeStorage(
        session if session is not None else _FakeSession("ws1", None),
    )
    app.state.workspace_manager = _FakeWorkspaceManager(root)
    app.include_router(session_router)
    return TestClient(app)


class DownloadFileTest(unittest.TestCase):
    """GET /sessions/{id}/files/download."""

    def test_downloads_workspace_file(self) -> None:
        """A valid workspace-relative path returns the file bytes."""
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "report.pdf"), "wb") as fh:
                fh.write(b"PDFDATA")
            resp = _client(os.path.realpath(root)).get(
                "/sessions/s1/files/download",
                params={"path": "report.pdf", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"PDFDATA")
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertIn("report.pdf", resp.headers["content-disposition"])

    def test_traversal_is_forbidden(self) -> None:
        """A `..` escape returns 403."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(os.path.realpath(root)).get(
                "/sessions/s1/files/download",
                params={"path": "../secret.txt", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 403)

    def test_absolute_path_is_forbidden(self) -> None:
        """An absolute path returns 403."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(os.path.realpath(root)).get(
                "/sessions/s1/files/download",
                params={"path": "/etc/hostname", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 403)

    def test_symlink_escape_is_forbidden(self) -> None:
        """A symlink inside the workspace pointing out returns 403."""
        with tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as outside:
            target = os.path.join(outside, "secret.txt")
            with open(target, "wb") as fh:
                fh.write(b"secret")
            os.symlink(target, os.path.join(root, "link.txt"))
            resp = _client(os.path.realpath(root)).get(
                "/sessions/s1/files/download",
                params={"path": "link.txt", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 403)

    def test_missing_file_is_404(self) -> None:
        """A path with no file returns 404."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(os.path.realpath(root)).get(
                "/sessions/s1/files/download",
                params={"path": "nope.txt", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 404)

    def test_unknown_session_is_404(self) -> None:
        """A session that does not exist returns 404."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(os.path.realpath(root), session=None).get(
                "/sessions/s1/files/download",
                params={"path": "x.txt", "agent_id": "a1"},
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 404)

    def test_missing_user_header_is_401(self) -> None:
        """A request without X-User-ID returns 401."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(os.path.realpath(root)).get(
                "/sessions/s1/files/download",
                params={"path": "x.txt", "agent_id": "a1"},
            )
        self.assertEqual(resp.status_code, 401)


class DownloadArchiveTest(unittest.TestCase):
    """GET /sessions/{id}/files/download-archive."""

    def test_zips_requested_files(self) -> None:
        """Requested files are zipped; missing ones are skipped."""
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "a.txt"), "wb") as fh:
                fh.write(b"AAA")
            with open(os.path.join(root, "b.txt"), "wb") as fh:
                fh.write(b"BBB")
            resp = _client(os.path.realpath(root)).get(
                "/sessions/s1/files/download-archive",
                params=[
                    ("paths", "a.txt"),
                    ("paths", "b.txt"),
                    ("paths", "gone.txt"),
                    ("agent_id", "a1"),
                ],
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            self.assertEqual(sorted(zf.namelist()), ["a.txt", "b.txt"])
            self.assertEqual(zf.read("a.txt"), b"AAA")

    def test_archive_all_missing_is_404(self) -> None:
        """When no requested file exists the archive is 404."""
        with tempfile.TemporaryDirectory() as root:
            resp = _client(os.path.realpath(root)).get(
                "/sessions/s1/files/download-archive",
                params=[("paths", "gone.txt"), ("agent_id", "a1")],
                headers=HEADERS,
            )
        self.assertEqual(resp.status_code, 404)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/session_files_download_test.py -v`
Expected: FAIL — 404 for `/sessions/s1/files/download` (route not defined yet), so assertions fail.

- [ ] **Step 3: Add imports to `_session.py`**

At the top of `src/agentscope/app/_router/_session.py`, add the stdlib imports and `FileResponse`/`Response`. The existing line `from fastapi.responses import StreamingResponse` becomes:

```python
import io
import mimetypes
import os
import zipfile
```
(add these alongside the existing `import asyncio` / `import json`)

```python
from fastapi.responses import FileResponse, Response, StreamingResponse
```

- [ ] **Step 4: Add the shared helpers + two handlers**

Append to `src/agentscope/app/_router/_session.py` (after the existing handlers). The helpers reuse the verified session→workspace resolution pattern:

```python
async def _resolve_session_root(
    user_id: str,
    agent_id: str,
    session_id: str,
    storage: "StorageBase",
    workspace_manager: "WorkspaceManagerBase",
) -> str:
    """Resolve the realpath of a session's workspace directory.

    Args:
        user_id (`str`):
            The requesting user's id (from the ``X-User-ID`` header).
        agent_id (`str`):
            The agent the session belongs to.
        session_id (`str`):
            The session id.
        storage (`StorageBase`):
            The application storage backend.
        workspace_manager (`WorkspaceManagerBase`):
            The application workspace manager.

    Returns:
        `str`:
            The realpath of the session's workspace directory.
    """
    session = await storage.get_session(user_id, agent_id, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found.",
        )
    workspace = await workspace_manager.get_workspace(
        user_id,
        agent_id,
        session_id,
        session.config.workspace_id,
        workdir=session.config.work_dir,
    )
    return os.path.realpath(workspace.workdir)


def _resolve_safe_path(root: str, rel: str) -> str:
    """Resolve a workspace-relative path, rejecting escapes.

    Args:
        root (`str`):
            The realpath of the workspace root.
        rel (`str`):
            The candidate workspace-relative path.

    Returns:
        `str`:
            The absolute realpath of the file inside the workspace.
    """
    if not rel:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A file path is required.",
        )
    if os.path.isabs(rel):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Absolute paths are not allowed.",
        )
    abs_path = os.path.realpath(os.path.join(root, rel))
    try:
        escapes = os.path.commonpath([root, abs_path]) != root
    except ValueError:
        escapes = True
    if escapes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="File path escapes the workspace.",
        )
    return abs_path


@session_router.get(
    "/{session_id}/files/download",
    summary="Download a workspace-confined file",
)
async def download_file(
    session_id: str,
    path: str = Query(description="Workspace-relative path of the file."),
    agent_id: str = Query(description="Agent the session belongs to."),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
) -> FileResponse:
    """Serve a single delivered file as a download attachment.

    Args:
        session_id (`str`):
            The session whose workspace holds the file.
        path (`str`):
            The workspace-relative path of the file.
        agent_id (`str`):
            The agent the session belongs to.
        user_id (`str`):
            The requesting user's id.
        storage (`StorageBase`):
            The application storage backend.
        workspace_manager (`WorkspaceManagerBase`):
            The application workspace manager.

    Returns:
        `FileResponse`:
            The file bytes with an attachment ``Content-Disposition``.
    """
    root = await _resolve_session_root(
        user_id,
        agent_id,
        session_id,
        storage,
        workspace_manager,
    )
    abs_path = _resolve_safe_path(root, path)
    if not os.path.isfile(abs_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found.",
        )
    media_type = (
        mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
    )
    return FileResponse(
        abs_path,
        filename=os.path.basename(abs_path),
        media_type=media_type,
    )


@session_router.get(
    "/{session_id}/files/download-archive",
    summary="Download several workspace-confined files as a zip",
)
async def download_archive(
    session_id: str,
    paths: list[str] = Query(
        default=[],
        description="Workspace-relative paths to include in the archive.",
    ),
    agent_id: str = Query(description="Agent the session belongs to."),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
) -> Response:
    """Zip several delivered files and serve them as one download.

    Args:
        session_id (`str`):
            The session whose workspace holds the files.
        paths (`list[str]`):
            The workspace-relative paths to include.
        agent_id (`str`):
            The agent the session belongs to.
        user_id (`str`):
            The requesting user's id.
        storage (`StorageBase`):
            The application storage backend.
        workspace_manager (`WorkspaceManagerBase`):
            The application workspace manager.

    Returns:
        `Response`:
            A ``application/zip`` attachment; missing/invalid paths are
            skipped, and a 404 is raised if none remain.
    """
    root = await _resolve_session_root(
        user_id,
        agent_id,
        session_id,
        storage,
        workspace_manager,
    )
    buffer = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in paths:
            try:
                abs_path = _resolve_safe_path(root, rel)
            except HTTPException:
                continue
            if not os.path.isfile(abs_path):
                continue
            archive.write(abs_path, arcname=os.path.basename(abs_path))
            added += 1
    if added == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No deliverable files found.",
        )
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="deliverables.zip"',
        },
    )
```

Note: `StorageBase`, `WorkspaceManagerBase`, `get_current_user_id`, `get_storage`, `get_workspace_manager`, `Query`, `HTTPException`, `status`, and `Depends` are already imported in `_session.py` (verified). Only the stdlib imports and `FileResponse`/`Response` are new.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/session_files_download_test.py -v`
Expected: PASS (9 tests)

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/app/_router/_session.py \
        tests/session_files_download_test.py
git commit -m "feat(delivery): add workspace-confined file download routes

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Register `DeliverFiles` on the leader agent

**Files:**
- Modify: `src/agentscope/subagent/_agent_tools.py` (inside `_factory`)
- Test: `tests/subagent_agent_tools_test.py` (add one test)

**Interfaces:**
- Consumes: `DeliverFiles(storage, workspace_manager, user_id, agent_id, session_id)` from Task 1.
- Produces: every leader tool list now contains exactly one `DeliverFiles` instance, appended unconditionally (available even with zero sub-agents).

- [ ] **Step 1: Write the failing test**

Add this method to `class MakeSubAgentToolFactoryTest` in `tests/subagent_agent_tools_test.py`:

```python
    async def test_appends_deliver_files_tool(self) -> None:
        """A DeliverFiles tool is always appended, even with no configs."""
        from agentscope.app._tool import DeliverFiles

        storage = _FakeStorage([])
        wm = _FakeWorkspaceManager(None)
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        deliver = [t for t in tools if isinstance(t, DeliverFiles)]
        self.assertEqual(len(deliver), 1)
        self.assertEqual(deliver[0].name, "DeliverFiles")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_agent_tools_test.py::MakeSubAgentToolFactoryTest::test_appends_deliver_files_tool -v`
Expected: FAIL — `len(deliver) == 0` (tool not appended yet).

- [ ] **Step 3: Append the tool in the factory**

In `src/agentscope/subagent/_agent_tools.py`, inside the inner `_factory`, right after the `tools: list[ToolBase] = []` line and before the `for record in records:` loop, add a lazy import and append (lazy import avoids an `agentscope.app` import cycle):

```python
        tools: list[ToolBase] = []
        # Leader-only: always offer file delivery to the user. Imported
        # lazily to avoid an agentscope.subagent -> agentscope.app cycle.
        from ..app._tool import DeliverFiles

        tools.append(
            DeliverFiles(
                storage=storage,
                workspace_manager=workspace_manager,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
            ),
        )
        for record in records:
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/subagent_agent_tools_test.py -v`
Expected: PASS (all existing tests + the new one). Note: `test_skips_malformed_config` and `test_falls_back_to_local_backend` assert `len(tools) == 1` — verify these still hold. They will FAIL now because `DeliverFiles` adds one tool.

- [ ] **Step 5: Fix the two count-based assertions**

Two existing tests count total tools. Update them to count only the relevant type (they were really asserting "one CLI tool"):

In `test_skips_malformed_config`, change:
```python
        tools = await factory("u1", "a1", "s1")
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "claude_code")
```
to:
```python
        tools = await factory("u1", "a1", "s1")
        cli = [t for t in tools if isinstance(t, CliSubAgentTool)]
        self.assertEqual(len(cli), 1)
        self.assertEqual(cli[0].name, "claude_code")
```

In `test_falls_back_to_local_backend`, change:
```python
        tools = await factory("u1", "a1", "s1")
        self.assertEqual(len(tools), 1)
        # A LocalBackend was created (not None).
        self.assertIsNotNone(tools[0]._backend)
```
to:
```python
        tools = await factory("u1", "a1", "s1")
        cli = [t for t in tools if isinstance(t, CliSubAgentTool)]
        self.assertEqual(len(cli), 1)
        # A LocalBackend was created (not None).
        self.assertIsNotNone(cli[0]._backend)
```

Also check `test_stateful_config_yields_stateful_tool` (asserts `len(tools) == 1` then `tools[0]`). Update it the same way:
```python
        tools = await factory("u1", "a1", "s1")
        cli = [t for t in tools if isinstance(t, CliSubAgentTool)]
        self.assertEqual(len(cli), 1)
        self.assertTrue(cli[0].is_stateful)
        self.assertIn("instance", cli[0].input_schema["properties"])
```

Re-run: `pytest tests/subagent_agent_tools_test.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/subagent/_agent_tools.py \
        tests/subagent_agent_tools_test.py
git commit -m "feat(delivery): register DeliverFiles on the leader agent

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Frontend delivery types + derive helper

**Files:**
- Create: `examples/web_ui/frontend/src/components/delivery/deriveDeliverables.ts`

**Interfaces:**
- Produces: `DeliveredFile`, `DeliveryManifest` types; `readManifest(metadata) -> DeliveryManifest | null`; `deriveDeliverables(msgs) -> DeliveredFile[]` (dedup by `path`, latest-wins); `parseDeliveryContext(downloadPath) -> {sessionId, agentId} | null`; `downloadAllDeliverables(files) -> Promise<void>`; `DELIVER_FILES_TOOL = 'DeliverFiles'`. Consumed by Tasks 6, 7, 8.

- [ ] **Step 1: Create the derive helper**

Create `examples/web_ui/frontend/src/components/delivery/deriveDeliverables.ts`:

```ts
import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';

import { filesApi } from '@/api';

/** One file delivered to the user, as recorded in a DeliverFiles manifest. */
export interface DeliveredFile {
	path: string;
	name: string;
	title?: string | null;
	description?: string | null;
	size: number;
	media_type: string;
	download_path: string;
}

/** An entry that could not be delivered. */
export interface InvalidDelivery {
	path: string;
	reason: string;
}

/** The `ravenx_delivery` manifest attached to a DeliverFiles tool result. */
export interface DeliveryManifest {
	message?: string | null;
	files: DeliveredFile[];
	invalid: InvalidDelivery[];
}

/** The tool name whose results carry a delivery manifest. */
export const DELIVER_FILES_TOOL = 'DeliverFiles';

/**
 * Read the `ravenx_delivery` manifest from a tool_result block's metadata,
 * returning `null` when it is absent or malformed.
 */
export function readManifest(
	metadata: Record<string, unknown> | undefined,
): DeliveryManifest | null {
	const raw = metadata?.ravenx_delivery;
	if (!raw || typeof raw !== 'object') return null;
	const manifest = raw as Partial<DeliveryManifest>;
	if (!Array.isArray(manifest.files)) return null;
	return {
		message: manifest.message ?? null,
		files: manifest.files as DeliveredFile[],
		invalid: (manifest.invalid as InvalidDelivery[]) ?? [],
	};
}

/**
 * Derive the union of all files delivered across the session transcript.
 *
 * Scans every `tool_result` block from a `DeliverFiles` call, reads its
 * manifest, and unions the files, de-duplicating by `path` with
 * latest-wins (a re-delivered file updates rather than duplicates).
 */
export function deriveDeliverables(msgs: Msg[]): DeliveredFile[] {
	const byPath = new Map<string, DeliveredFile>();
	for (const msg of msgs) {
		const blocks: ContentBlock[] = Array.isArray(msg.content) ? msg.content : [];
		for (const block of blocks) {
			if (block.type !== 'tool_result' || block.name !== DELIVER_FILES_TOOL) {
				continue;
			}
			const manifest = readManifest(block.metadata);
			if (!manifest) continue;
			for (const file of manifest.files) byPath.set(file.path, file);
		}
	}
	return [...byPath.values()];
}

/**
 * Extract the `{sessionId, agentId}` a manifest's `download_path` was built
 * with (`/sessions/{sid}/files/download?...&agent_id=...`), or `null`.
 */
export function parseDeliveryContext(
	downloadPath: string,
): { sessionId: string; agentId: string } | null {
	const match = downloadPath.match(/^\/sessions\/([^/]+)\/files\/download/);
	const sessionId = match ? decodeURIComponent(match[1]) : null;
	const agentId = new URLSearchParams(downloadPath.split('?')[1] ?? '').get(
		'agent_id',
	);
	if (!sessionId || !agentId) return null;
	return { sessionId, agentId };
}

/**
 * Download every given file as a single zip, using the session/agent
 * context recovered from the first file's `download_path`.
 */
export async function downloadAllDeliverables(
	files: DeliveredFile[],
): Promise<void> {
	if (files.length === 0) return;
	const ctx = parseDeliveryContext(files[0].download_path);
	if (!ctx) return;
	await filesApi.downloadArchive(
		ctx.sessionId,
		ctx.agentId,
		files.map((f) => f.path),
	);
}
```

- [ ] **Step 2: Typecheck**

`filesApi` does not exist yet (Task 5) so this will not compile alone. Defer the typecheck to the end of Task 5, which creates `filesApi`. (Task 4 and Task 5 are committed together in Task 5's commit.)

Run (expected to fail on the missing `filesApi` import only): `cd examples/web_ui/frontend && pnpm exec tsc -b`
Expected: error `Module '"@/api"' has no exported member 'filesApi'` — resolved by Task 5.

---

### Task 5: Authenticated download API helper

**Files:**
- Create: `examples/web_ui/frontend/src/api/files.ts`
- Modify: `examples/web_ui/frontend/src/api/index.ts`

**Interfaces:**
- Consumes: `client.stream(path, opts)` from `src/api/client.ts` (sends `X-User-ID`, throws + toasts on non-2xx).
- Produces: `filesApi.downloadFromPath(downloadPath, filename)` and `filesApi.downloadArchive(sessionId, agentId, relPaths)`. Consumed by Tasks 4, 6.

- [ ] **Step 1: Create the API module**

Create `examples/web_ui/frontend/src/api/files.ts`:

```ts
import { client } from './client';

/** Trigger a browser "save as" for an already-fetched blob. */
function saveBlob(blob: Blob, filename: string): void {
	const url = URL.createObjectURL(blob);
	const anchor = document.createElement('a');
	anchor.href = url;
	anchor.download = filename;
	document.body.appendChild(anchor);
	anchor.click();
	anchor.remove();
	URL.revokeObjectURL(url);
}

export const filesApi = {
	/**
	 * Download a single delivered file. `downloadPath` is the backend-issued
	 * canonical URL from the delivery manifest (already carries `agent_id`
	 * and the URL-encoded path). Authenticated via the client's `X-User-ID`
	 * header; a non-2xx response throws and auto-toasts.
	 */
	downloadFromPath: async (
		downloadPath: string,
		filename: string,
	): Promise<void> => {
		const res = await client.stream(downloadPath, { method: 'GET' });
		saveBlob(await res.blob(), filename);
	},

	/**
	 * Download several delivered files as one zip ("Download all").
	 */
	downloadArchive: async (
		sessionId: string,
		agentId: string,
		relPaths: string[],
	): Promise<void> => {
		const params = new URLSearchParams();
		params.set('agent_id', agentId);
		for (const rel of relPaths) params.append('paths', rel);
		const path = `/sessions/${encodeURIComponent(
			sessionId,
		)}/files/download-archive?${params.toString()}`;
		const res = await client.stream(path, { method: 'GET' });
		saveBlob(await res.blob(), 'deliverables.zip');
	},
};
```

- [ ] **Step 2: Add the barrel export**

In `examples/web_ui/frontend/src/api/index.ts`, add after the other named exports:

```ts
export { filesApi } from './files';
```

- [ ] **Step 3: Typecheck (Tasks 4 + 5 together)**

Run: `cd examples/web_ui/frontend && pnpm exec tsc -b`
Expected: PASS (no type errors — `deriveDeliverables.ts` now resolves `filesApi`).

- [ ] **Step 4: Lint**

Run: `cd examples/web_ui/frontend && pnpm lint`
Expected: no errors in the new files.

- [ ] **Step 5: Commit**

```bash
git add examples/web_ui/frontend/src/api/files.ts \
        examples/web_ui/frontend/src/api/index.ts \
        examples/web_ui/frontend/src/components/delivery/deriveDeliverables.ts
git commit -m "feat(delivery): add delivery manifest helpers and download API

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `DeliveredFileRow` shared component

**Files:**
- Create: `examples/web_ui/frontend/src/components/delivery/DeliveredFileRow.tsx`

**Interfaces:**
- Consumes: `DeliveredFile` (Task 4), `filesApi.downloadFromPath` (Task 5), shadcn `Button` (`@/components/ui/button`, `variant="ghost"`, `size="icon-sm"` — same as used in ChatViewport panel actions).
- Produces: `DeliveredFileRow({ file })` — icon + title/name + size + description + authenticated download button. Consumed by Tasks 7, 8.

- [ ] **Step 1: Create the component**

Create `examples/web_ui/frontend/src/components/delivery/DeliveredFileRow.tsx`:

```tsx
import { Download, File, FileAudio, FileImage, FileText, FileVideo } from 'lucide-react';
import { useState } from 'react';

import type { DeliveredFile } from './deriveDeliverables';
import { filesApi } from '@/api';
import { Button } from '@/components/ui/button';

function FileKindIcon({ mediaType, className }: { mediaType: string; className?: string }) {
	switch (mediaType.split('/')[0]) {
		case 'image':
			return <FileImage className={className} />;
		case 'audio':
			return <FileAudio className={className} />;
		case 'video':
			return <FileVideo className={className} />;
		case 'text':
			return <FileText className={className} />;
		default:
			return mediaType === 'application/pdf' ? (
				<FileText className={className} />
			) : (
				<File className={className} />
			);
	}
}

/** Humanize a byte count, e.g. `1536` → `"1.5 KB"`. */
function humanSize(bytes: number): string {
	if (bytes < 1024) return `${bytes} B`;
	const units = ['KB', 'MB', 'GB', 'TB'];
	let value = bytes / 1024;
	let unit = 0;
	while (value >= 1024 && unit < units.length - 1) {
		value /= 1024;
		unit += 1;
	}
	return `${value.toFixed(1)} ${units[unit]}`;
}

/**
 * One delivered-file row: icon, title/name, size, optional description and
 * an authenticated download button. Shared by the inline delivery card and
 * the Deliverables panel.
 */
export function DeliveredFileRow({ file }: { file: DeliveredFile }) {
	const [busy, setBusy] = useState(false);
	const onDownload = async () => {
		setBusy(true);
		try {
			await filesApi.downloadFromPath(file.download_path, file.name);
		} finally {
			setBusy(false);
		}
	};
	return (
		<div className="flex items-center gap-3 rounded-lg border bg-muted/40 px-3 py-2">
			<span className="flex size-9 shrink-0 items-center justify-center rounded-md bg-background text-muted-foreground">
				<FileKindIcon mediaType={file.media_type} className="size-4" />
			</span>
			<span className="flex min-w-0 flex-1 flex-col">
				<span className="truncate text-sm font-medium text-foreground">
					{file.title || file.name}
				</span>
				<span className="truncate text-xs text-muted-foreground">
					{file.name} · {humanSize(file.size)}
				</span>
				{file.description && (
					<span className="truncate text-xs text-muted-foreground">
						{file.description}
					</span>
				)}
			</span>
			<Button
				variant="ghost"
				size="icon-sm"
				disabled={busy}
				onClick={onDownload}
				aria-label={`Download ${file.name}`}
			>
				<Download className="size-4" />
			</Button>
		</div>
	);
}
```

- [ ] **Step 2: Verify the `Button` size token exists**

Run: `cd examples/web_ui/frontend && grep -n "icon-sm" src/components/ui/button.tsx`
Expected: a match (the `icon-sm` size variant). If absent, use `size="icon"` instead.

- [ ] **Step 3: Typecheck + lint**

Run: `cd examples/web_ui/frontend && pnpm exec tsc -b && pnpm lint`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add examples/web_ui/frontend/src/components/delivery/DeliveredFileRow.tsx
git commit -m "feat(delivery): add DeliveredFileRow with authenticated download

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `DeliverFiles` inline tool renderer

**Files:**
- Create: `examples/web_ui/frontend/src/components/chat/tool-renderers/DeliverFilesRenderer.tsx`
- Modify: `examples/web_ui/frontend/src/components/chat/tool-renderers/index.tsx`

**Interfaces:**
- Consumes: `ToolRenderer` type + `toolLabelClass`/`toolArgClass` (`_shared`), `readManifest`/`downloadAllDeliverables` (Task 4), `DeliveredFileRow` (Task 6), shadcn `Button`.
- Produces: a `DeliverFilesRenderer: ToolRenderer` registered under `DeliverFiles` in the `renderers` map. When the manifest is absent, `renderBody` returns `undefined` so the registry falls back to `defaultRenderBody` (the text summary).

- [ ] **Step 1: Create the renderer**

Create `examples/web_ui/frontend/src/components/chat/tool-renderers/DeliverFilesRenderer.tsx`:

```tsx
import { toolArgClass, toolLabelClass } from './_shared';
import type { ToolRenderer } from './types';
import { DeliveredFileRow } from '@/components/delivery/DeliveredFileRow';
import { downloadAllDeliverables, readManifest } from '@/components/delivery/deriveDeliverables';
import { Button } from '@/components/ui/button';

export const DeliverFilesRenderer: ToolRenderer = {
	getDisplayName: () => 'Deliver files',

	renderHeader: (pair) => {
		const manifest = readManifest(pair.result?.metadata);
		const count = manifest?.files.length ?? 0;
		return (
			<>
				<span className={toolLabelClass}>Deliver files</span>
				{count > 0 && (
					<span className={toolArgClass}>
						{count} file{count === 1 ? '' : 's'}
					</span>
				)}
			</>
		);
	},

	renderBody: (pair) => {
		const manifest = readManifest(pair.result?.metadata);
		// No manifest (e.g. before the call runs, or metadata absent) →
		// undefined lets the registry fall back to the default text body.
		if (!manifest || manifest.files.length === 0) return undefined;
		return (
			<div className="flex flex-col gap-2">
				{manifest.message && (
					<p className="text-xs text-muted-foreground">{manifest.message}</p>
				)}
				{manifest.files.map((file) => (
					<DeliveredFileRow key={file.path} file={file} />
				))}
				{manifest.files.length > 1 && (
					<Button
						variant="outline"
						size="sm"
						className="w-fit"
						onClick={() => downloadAllDeliverables(manifest.files)}
					>
						Download all
					</Button>
				)}
				{manifest.invalid.length > 0 && (
					<p className="text-xs text-muted-foreground">
						Could not deliver:{' '}
						{manifest.invalid.map((i) => `${i.path} (${i.reason})`).join(', ')}
					</p>
				)}
			</div>
		);
	},
};
```

- [ ] **Step 2: Register the renderer**

In `examples/web_ui/frontend/src/components/chat/tool-renderers/index.tsx`, add the import (alphabetical, next to the other renderer imports):

```tsx
import { DeliverFilesRenderer } from './DeliverFilesRenderer';
```
and add an entry to the `renderers` map:

```tsx
const renderers: Record<string, ToolRenderer> = {
	Bash: BashRenderer,
	Read: ReadRenderer,
	Write: WriteRenderer,
	Edit: EditRenderer,
	Glob: GlobRenderer,
	Grep: GrepRenderer,
	TaskCreate: TaskCreateRenderer,
	DeliverFiles: DeliverFilesRenderer,
};
```

- [ ] **Step 3: Typecheck + lint**

Run: `cd examples/web_ui/frontend && pnpm exec tsc -b && pnpm lint`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add examples/web_ui/frontend/src/components/chat/tool-renderers/DeliverFilesRenderer.tsx \
        examples/web_ui/frontend/src/components/chat/tool-renderers/index.tsx
git commit -m "feat(delivery): render DeliverFiles tool calls as a download card

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Deliverables panel + ChatViewport wiring

**Files:**
- Create: `examples/web_ui/frontend/src/components/delivery/DeliverablesPanel.tsx`
- Modify: `examples/web_ui/frontend/src/components/panel/PanelDock.tsx` (extend `PanelKey`)
- Modify: `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx` (descriptor, toggle, badge, imports)

**Interfaces:**
- Consumes: `deriveDeliverables`/`downloadAllDeliverables` (Task 4), `DeliveredFileRow` (Task 6), `msgs: Msg[]` from `useMessages` (already in ChatViewport).
- Produces: a `'delivery'` panel showing all session deliverables with a count badge on the toggle.

- [ ] **Step 1: Create the panel component**

Create `examples/web_ui/frontend/src/components/delivery/DeliverablesPanel.tsx`:

```tsx
import type { Msg } from '@agentscope-ai/agentscope/message';
import { useMemo } from 'react';

import { DeliveredFileRow } from './DeliveredFileRow';
import { deriveDeliverables, downloadAllDeliverables } from './deriveDeliverables';
import { Button } from '@/components/ui/button';

/**
 * Read-only right-dock panel aggregating every file delivered across the
 * session. Derived entirely from the transcript (no API call).
 */
export function DeliverablesPanel({ msgs }: { msgs: Msg[] }) {
	const files = useMemo(() => deriveDeliverables(msgs), [msgs]);
	if (files.length === 0) {
		return <p className="p-3 text-xs text-muted-foreground">No deliverables yet.</p>;
	}
	return (
		<div className="flex flex-col gap-2 p-2">
			{files.map((file) => (
				<DeliveredFileRow key={file.path} file={file} />
			))}
			{files.length > 1 && (
				<Button
					variant="outline"
					size="sm"
					className="w-fit"
					onClick={() => downloadAllDeliverables(files)}
				>
					Download all
				</Button>
			)}
		</div>
	);
}
```

- [ ] **Step 2: Extend the `PanelKey` union**

In `examples/web_ui/frontend/src/components/panel/PanelDock.tsx` line 16, add `'delivery'`:

```ts
export type PanelKey = 'plan' | 'mcp' | 'skill' | 'permission' | 'knowledge' | 'subagent' | 'delivery';
```

- [ ] **Step 3: Wire the panel into ChatViewport**

In `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx`:

(a) Add `Package` to the lucide-react icon import (line 3, alongside `Bot`, `Database`, etc.):
```tsx
import { BookText, Bot, ChevronDown, Database, ListTodo, Package, PanelRight, ShieldCheck } from 'lucide-react';
```

(b) Add the component import (next to the `SubagentInstanceMonitor` import ~line 24):
```tsx
import { DeliverablesPanel } from '@/components/delivery/DeliverablesPanel';
import { deriveDeliverables } from '@/components/delivery/deriveDeliverables';
```

(c) Add a memoized count near the other `useMemo`s (after the `panels` memo is fine, or before — it only depends on `msgs`):
```tsx
	const deliverableCount = useMemo(() => deriveDeliverables(msgs).length, [msgs]);
```

(d) Add the `delivery` descriptor to the `panels` `useMemo` object (after the `subagent` entry):
```tsx
			delivery: {
				title: 'Deliverables',
				icon: <Package className="size-4" />,
				content: <DeliverablesPanel msgs={msgs} />,
			},
```
`msgs` is already in the memo's dependency array, so no dependency change is needed.

(e) Add the dropdown checkbox toggle with a count badge, right after the `subagent` `<DropdownMenuCheckboxItem>` block (~line 636):
```tsx
	<DropdownMenuCheckboxItem
		checked={isPanelOpen('delivery')}
		onCheckedChange={() => togglePanel('delivery')}
		onSelect={(e) => e.preventDefault()}
	>
		<Package />
		Deliverables
		{deliverableCount > 0 && (
			<Badge variant="outline" className="ml-auto">
				{deliverableCount}
			</Badge>
		)}
	</DropdownMenuCheckboxItem>
```
(`Badge` is already imported and used in the `permission`/`knowledge` descriptors.)

- [ ] **Step 4: Typecheck + lint + build**

Run: `cd examples/web_ui/frontend && pnpm build && pnpm lint`
Expected: PASS. The `Record<PanelKey, PanelDescriptor>` memo now type-requires the `delivery` key you added — if you forgot it, `tsc` fails here.

- [ ] **Step 5: Commit**

```bash
git add examples/web_ui/frontend/src/components/delivery/DeliverablesPanel.tsx \
        examples/web_ui/frontend/src/components/panel/PanelDock.tsx \
        examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx
git commit -m "feat(delivery): add aggregated Deliverables panel with count badge

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: End-to-end acceptance smoke + backend gate

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend test suite + pre-commit**

Run:
```bash
pytest tests/deliver_files_tool_test.py tests/session_files_download_test.py tests/subagent_agent_tools_test.py -v
pre-commit run --all-files
```
Expected: all tests PASS; pre-commit (black/flake8/pylint/mypy/docstrings) clean. Fix any lint findings in the code (do not disable checks).

- [ ] **Step 2: Boot the stack** (per the RavenX web-UI setup)

- Ensure redis is up (docker) and start the service in the `ravenx` env on port 8001.
- `cd examples/web_ui/frontend && pnpm install && pnpm dev` (Vite on :5173).
- In the UI, set `server_url` / `username` (the `X-User-ID` source) as usual.

- [ ] **Step 3: Drive the delivery flow** (use the `verify` skill / Playwright MCP)

- In a chat session, instruct the leader agent to create a file and deliver it, e.g.: *"Write a file report.md with a one-line summary, then call DeliverFiles to deliver it to me with title 'Summary'."*
- Confirm the inline tool-call row renders the delivery card: title "Summary", filename + size, and a Download button.
- Click **Download** — confirm the browser saves `report.md` with the correct bytes (network tab shows a 200 from `/sessions/.../files/download` with `X-User-ID`).
- Deliver a second file in a later turn; open the **Deliverables** panel from the panel dropdown — confirm both files are listed and the toggle shows a count badge of 2.
- Click **Download all** — confirm a `deliverables.zip` downloads containing both files.
- Negative check: ask the agent to deliver a non-existent path; confirm the card/summary reports it as not delivered and the tool result state is error-styled.

- [ ] **Step 4: Final verification commit (if any fixups were needed)**

```bash
git add -A
git commit -m "test(delivery): fixups from end-to-end acceptance smoke

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

If no fixups were needed, skip this commit.

---

## Self-Review

**Spec coverage:** §4 tool → Task 1; §5 download routes + guard → Task 2; factory attach (§4) → Task 3; §6 download helper + manifest types → Tasks 4–5; `DeliveredFileRow` (§6) → Task 6; inline renderer (§6) → Task 7; Deliverables panel + PanelKey + badge (§7) → Task 8; error handling (§8) covered across tool (Task 1 invalid/ERROR), routes (Task 2 403/404/401), frontend (renderer fallback to text body, `client.stream` auto-toast); testing + acceptance (§9) → Tasks 1–3 (pytest) + Task 9 (pre-commit + e2e smoke). Decisions (§11) all reflected. No spec requirement is unassigned. Deviation from spec §9: frontend unit tests are omitted (no runner exists; gated by typecheck/lint/e2e instead) — this matches the spec's "if the web_ui has a test runner … otherwise rely on the end-to-end smoke".

**Placeholder scan:** no TBD/TODO/"add error handling" — every code step contains full code and exact commands.

**Type consistency:** manifest shape `{message, files:[{path,name,title,description,size,media_type,download_path}], invalid:[{path,reason}]}` is identical in Task 1 (Python), the Task 1 test assertion, and Task 4 (`DeliveredFile`/`DeliveryManifest`). `download_path` format `/sessions/{sid}/files/download?path=..&agent_id=..` matches between Task 1 (producer), Task 2 (route), and Task 4 `parseDeliveryContext`. Tool name literal `"DeliverFiles"` matches across tool `name`, registry key (Task 7), and `DELIVER_FILES_TOOL` (Task 4). `filesApi.downloadFromPath`/`downloadArchive` signatures match between Task 5 (def) and Tasks 4/6 (callers).
