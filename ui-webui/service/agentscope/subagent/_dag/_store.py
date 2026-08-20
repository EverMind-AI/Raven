# -*- coding: utf-8 -*-
"""Backend-backed storage for one DAG run's message files."""

import json
import time
import uuid
from typing import Any

_DAG_DIR = ".ravenx_dag"


def make_run_id() -> str:
    """Build a unique, sortable run id.

    Returns:
        `str`:
            ``"<UTC timestamp>-<8 hex>"``, e.g.
            ``"20260717T031500Z-1a2b3c4d"``.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


class DagRunStore:
    """Owns the on-disk layout for a single DAG run.

    All I/O goes through the workspace ``backend`` so it works for
    local, Docker, E2B, and remote backends alike.
    """

    def __init__(self, backend: Any, workdir: str, run_id: str) -> None:
        """Initialize the store.

        Args:
            backend (`BackendBase`):
                The session workspace backend.
            workdir (`str`):
                The session working directory (run dirs live under it).
            run_id (`str`):
                The unique id for this run.
        """
        self._backend = backend
        self._workdir = workdir
        self.run_id = run_id

    @property
    def run_dir(self) -> str:
        """The run-scoped directory ``<workdir>/.ravenx_dag/<run_id>``."""
        return self._backend.join_path(self._workdir, _DAG_DIR, self.run_id)

    def prompt_path(self, node_id: str) -> str:
        """Path of a node's rendered prompt file.

        Args:
            node_id (`str`):
                The node id.

        Returns:
            `str`:
                ``<run_dir>/<node_id>.prompt.md``.
        """
        return self._backend.join_path(self.run_dir, f"{node_id}.prompt.md")

    def output_path(self, node_id: str) -> str:
        """Path of a node's captured output file.

        Args:
            node_id (`str`):
                The node id.

        Returns:
            `str`:
                ``<run_dir>/<node_id>.out.md``.
        """
        return self._backend.join_path(self.run_dir, f"{node_id}.out.md")

    async def init(self, graph_json: str) -> None:
        """Create the run dir (implicitly) and persist ``graph.json``.

        Args:
            graph_json (`str`):
                The submitted graph spec, serialized as JSON.
        """
        await self.write_text(
            self._backend.join_path(self.run_dir, "graph.json"),
            graph_json,
        )

    async def write_text(self, path: str, text: str) -> None:
        """Write UTF-8 text to ``path`` (parent dirs auto-created).

        Args:
            path (`str`):
                Destination path in the backend environment.
            text (`str`):
                The text to write.
        """
        await self._backend.write_file(path, text.encode("utf-8"))

    async def read_text(self, path: str) -> str:
        """Read UTF-8 text from ``path``.

        Args:
            path (`str`):
                Path in the backend environment.

        Returns:
            `str`:
                The decoded contents.
        """
        data = await self._backend.read_file(path)
        return data.decode("utf-8", errors="replace")

    async def write_manifest(self, manifest: dict) -> None:
        """Persist the run manifest as ``manifest.json``.

        Args:
            manifest (`dict`):
                Node id to status/metadata mapping.
        """
        await self.write_text(
            self._backend.join_path(self.run_dir, "manifest.json"),
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

    async def append_index(self, entry: dict) -> None:
        """Append one run entry to the session-level ``index.json``.

        Args:
            entry (`dict`):
                A small summary of this run for later discovery.
        """
        path = self._backend.join_path(self._workdir, _DAG_DIR, "index.json")
        entries: list = []
        if await self._backend.file_exists(path):
            raw = await self.read_text(path)
            try:
                entries = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                entries = []
            if not isinstance(entries, list):
                entries = []
        entries.append(entry)
        await self.write_text(
            path,
            json.dumps(entries, ensure_ascii=False, indent=2),
        )
