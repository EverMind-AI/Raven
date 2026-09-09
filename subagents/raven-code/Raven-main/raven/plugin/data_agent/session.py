"""The per-workspace data-agent session: a client for the kernel process.

One session owns one kernel subprocess (see ``kernel_program.py``), which in
turn owns the single DuckDB connection with every configured source attached
read-only. All tools and the model's ``python`` cells go through this client,
so TEMP tables, registered views and Python variables live on one plane.

The kernel interpreter is configurable (``kernel_python`` in the plugin
config slice): in an evaluation sandbox it points at the task image's Python
-- the one with duckdb/pymongo/pandas installed -- keeping the raven runtime
free of binary data dependencies. It defaults to the host interpreter.

Crash policy: a request that times out SIGKILLs the kernel; the next request
restarts it, re-attaches the sources, and the response says explicitly that
every Python variable from earlier cells is gone. Silent state loss is the
one thing a stateful kernel must never do.

The tools are contributed as separate plugin factories but must share one
session. They key into a process-level table by workspace path, so the second
factory to run reuses the session the first created.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_KERNEL_SOURCE = Path(__file__).with_name("kernel_program.py")
_DEFAULT_TIMEOUT = 120.0
_BOOTSTRAP_TIMEOUT = 600.0

_SESSIONS: dict[str, "DataAgentSession"] = {}
_SESSIONS_LOCK = threading.Lock()


class KernelError(RuntimeError):
    """The kernel could not serve a request (dead interpreter, bad launch)."""


@dataclass(frozen=True)
class SourceSpec:
    """One configured store to attach to the plane.

    ``kind`` is sqlite | duckdb | postgres | mongo. ``ref`` is a file path
    (sqlite/duckdb), a libpq DSN (postgres), or a mongo URI (mongo). ``alias``
    is the schema name the model sees, e.g. ``metadata.articles``.
    """

    alias: str
    kind: str
    ref: str
    read_only: bool = True

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "SourceSpec":
        kind = str(d.get("kind", "")).lower()
        if kind not in {"sqlite", "duckdb", "postgres", "mongo"}:
            raise ValueError(f"unsupported source kind {kind!r} (alias {d.get('alias')!r})")
        ref = d.get("ref") or d.get("path") or d.get("dsn") or d.get("uri")
        if not ref:
            raise ValueError(f"source {d.get('alias')!r} has no ref/path/dsn/uri")
        alias = d.get("alias") or d.get("name")
        if not alias:
            raise ValueError(f"source with ref {ref!r} has no alias")
        return SourceSpec(alias=str(alias), kind=kind, ref=str(ref), read_only=bool(d.get("read_only", True)))

    def as_dict(self) -> dict[str, Any]:
        return {"alias": self.alias, "kind": self.kind, "ref": self.ref, "read_only": self.read_only}


@dataclass
class DataAgentSession:
    """Kernel client plus artifact dir for one workspace."""

    workspace: Path
    sources: tuple[SourceSpec, ...]
    kernel_python: str = sys.executable
    extension_dir: str | None = None
    _proc: Any = None
    _lines: "queue.Queue[str | None]" = field(default_factory=queue.Queue)
    _reader: Any = None
    _ever_started: bool = False
    _bootstrap_notes: list[str] = field(default_factory=list)
    _request_id: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def artifact_dir(self) -> Path:
        d = self.workspace / "derived"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- lifecycle ----------------------------------------------------------

    def _spawn(self) -> None:
        program = self.workspace / ".data_agent" / "kernel_program.py"
        program.parent.mkdir(parents=True, exist_ok=True)
        program.write_text(_KERNEL_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            self._proc = subprocess.Popen(
                [self.kernel_python, str(program)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(self.workspace),
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise KernelError(f"cannot launch kernel with {self.kernel_python!r}: {exc}") from exc
        self._lines = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, args=(self._proc, self._lines), daemon=True)
        self._reader.start()
        resp = self._roundtrip(
            {
                "op": "bootstrap",
                "sources": [s.as_dict() for s in self.sources],
                "artifact_dir": str(self.artifact_dir),
                "extension_dir": self.extension_dir,
            },
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        if not resp.get("ok"):
            self._kill()
            raise KernelError(f"kernel bootstrap failed: {resp.get('error')}")
        self._bootstrap_notes = list(resp.get("notes") or [])

    @staticmethod
    def _read_loop(proc: Any, lines: "queue.Queue[str | None]") -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def _kill(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait()
            self._proc = None

    def close(self) -> None:
        with self._lock:
            self._kill()

    # -- request path -------------------------------------------------------

    def _roundtrip(self, payload: dict, timeout: float) -> dict:
        self._request_id += 1
        payload["id"] = self._request_id
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise KernelError(f"kernel pipe is closed: {exc}") from exc
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise KernelError(f"kernel did not answer within {timeout:.0f}s")
        if line is None:
            raise KernelError("kernel process exited")
        return json.loads(line)

    def request(self, payload: dict, timeout: float = _DEFAULT_TIMEOUT) -> dict:
        """Send one op to the kernel, restarting it if it is dead or wedged.

        The response dict gains a ``session_notes`` list carrying bootstrap
        output on first use and the variables-lost warning after a restart.
        """
        with self._lock:
            notes: list[str] = []
            if self._proc is None or self._proc.poll() is not None:
                was_started = self._ever_started
                self._kill()
                try:
                    self._spawn()
                except KernelError as exc:
                    return {"ok": False, "error": str(exc), "session_notes": notes}
                self._ever_started = True
                notes.extend(self._bootstrap_notes)
                if was_started:
                    notes.append(
                        "KERNEL RESTARTED: the previous kernel died, every Python variable from "
                        "earlier cells is gone. Data sources were re-attached; re-run setup cells."
                    )
            try:
                resp = self._roundtrip(payload, timeout=timeout)
            except KernelError as exc:
                self._kill()
                resp = {
                    "ok": False,
                    "error": (
                        f"{exc}. The kernel was killed; every Python variable is lost. The next call "
                        "starts a fresh kernel with sources re-attached."
                    ),
                }
            resp.setdefault("session_notes", []).extend(notes)
            return resp

    def sql(
        self,
        capped_query: str,
        *,
        raw_query: str | None = None,
        preview: int = 50,
        save_as: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> dict:
        payload: dict[str, Any] = {"op": "sql", "query": capped_query, "preview": preview}
        if save_as:
            payload["save_as"] = save_as
            payload["raw_query"] = raw_query or capped_query
        return self.request(payload, timeout=timeout)

    def exec_code(self, code: str, timeout: float = _DEFAULT_TIMEOUT) -> dict:
        return self.request({"op": "exec", "code": code}, timeout=timeout)

    def put_rows(self, name: str, cols: list[str], rows: list[list], timeout: float = _DEFAULT_TIMEOUT) -> dict:
        return self.request({"op": "put_rows", "name": name, "cols": cols, "rows": rows}, timeout=timeout)


def get_session(
    workspace: Path,
    sources: tuple[SourceSpec, ...],
    kernel_python: str | None = None,
    extension_dir: str | None = None,
) -> DataAgentSession:
    """Return the shared session for a workspace, creating it once.

    Keyed by resolved workspace path so every tool factory for the same
    workspace shares one kernel. ``sources`` / ``kernel_python`` /
    ``extension_dir`` apply only when the session is first created; later
    callers reuse the plane.
    """
    key = str(workspace.resolve())
    with _SESSIONS_LOCK:
        existing = _SESSIONS.get(key)
        if existing is None:
            existing = DataAgentSession(
                workspace=workspace,
                sources=sources,
                kernel_python=kernel_python or sys.executable,
                extension_dir=extension_dir,
            )
            _SESSIONS[key] = existing
        return existing


def parse_sources(config: dict[str, Any]) -> tuple[SourceSpec, ...]:
    """Read the plugin's source list into SourceSpecs.

    Two config shapes, combinable: ``sources`` (inline list of dicts) and
    ``sources_file`` (path to a JSON list written by the environment --
    an eval harness materializes it per task, where a static config cannot
    know each task's stores).
    """
    raw = config.get("sources") or []
    if not isinstance(raw, list):
        raise ValueError("data-agent 'sources' must be a list of source dicts")
    items = list(raw)
    sources_file = config.get("sources_file")
    if sources_file:
        loaded = json.loads(Path(sources_file).read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError(f"data-agent sources_file {sources_file!r} must hold a JSON list")
        items.extend(loaded)
    return tuple(SourceSpec.from_dict(item) for item in items)
