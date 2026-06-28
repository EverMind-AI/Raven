"""Unix domain socket debug server for sandbox VM inspection and interaction."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path

from raven.sandbox._async_utils import cancel_and_collect as _cancel_and_collect

logger = logging.getLogger(__name__)


class SandboxDebugServerError(RuntimeError):
    """Raised when the debug server cannot start (e.g. socket already in use)."""


class SandboxDebugServer:
    """
    Listens on a Unix domain socket and serves sandbox debug commands.

    Single-client server: at most one client connection is accepted at a time
    (list / exec / shell). A second concurrent connection is rejected with a
    clear error message. This avoids two debug clients racing on the same VM
    and matches the semantics of an interactive debugger attached to a process.

    Protocol: newline-delimited JSON. Binary payloads use base64 in the "data"
    field. The line-length cap (max_message_bytes) is enforced via the
    StreamReader buffer limit passed to asyncio.start_unix_server().
    """

    # How long start() waits when probing an existing socket file to decide
    # whether it belongs to a live server (refuse) or is stale (unlink).
    _PROBE_TIMEOUT_SEC = 0.5

    def __init__(
        self,
        socket_path: Path,
        owned_ids: set[str],
        max_message_bytes: int = 1048576,
    ) -> None:
        self._socket_path = socket_path
        self._owned_ids = owned_ids
        self._max_message_bytes = max_message_bytes
        self._server: asyncio.AbstractServer | None = None
        self._active_client: asyncio.StreamWriter | None = None

    @staticmethod
    def resolve_socket_path(debug_socket: str, data_dir: Path) -> Path:
        """Resolve debug_socket to an absolute Path, creating parent dirs.

        Relative paths are joined with data_dir. Absolute paths are used as-is.
        Parent directories are created automatically in both cases.
        """
        p = Path(debug_socket)
        if not p.is_absolute():
            p = data_dir / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    async def start(self) -> None:
        """Bind the Unix socket; refuse to clobber a live socket from another process.

        If the socket file already exists we probe it: if a server is listening,
        raise SandboxDebugServerError so the caller can surface a clear message
        ("another raven process owns the socket"). If the connection fails
        (ECONNREFUSED / no listener) we treat it as a stale file and unlink it.
        """
        if self._socket_path.exists():
            if await self._probe_alive():
                raise SandboxDebugServerError(
                    f"Sandbox debug socket already in use at {self._socket_path}: "
                    "another raven process is running with debug enabled. "
                    "Stop it, or set tools.sandbox.debug.socket to a different path."
                )
            self._socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self._socket_path),
            limit=self._max_message_bytes,
        )
        os.chmod(self._socket_path, 0o600)
        logger.info("Sandbox debug server listening at %s", self._socket_path)

    async def _probe_alive(self) -> bool:
        """Return True if a server is currently accepting on self._socket_path."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._socket_path)),
                timeout=self._PROBE_TIMEOUT_SEC,
            )
        except (ConnectionRefusedError, FileNotFoundError, asyncio.TimeoutError, OSError):
            return False
        try:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except Exception:
            pass
        return True

    async def stop(self) -> None:
        """Stop accepting connections and remove the socket file."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._socket_path.unlink(missing_ok=True)
        logger.info("Sandbox debug server stopped")

    # ------------------------------------------------------------------
    # Per-connection dispatch
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle exactly one client connection: read one command, dispatch, close.

        Single-client invariant: if another client is already attached, this
        connection is rejected immediately with an explanatory error so two
        debug CLIs can never race on the same VM.
        """
        if self._active_client is not None:
            try:
                await _send(writer, {
                    "type": "error",
                    "message": (
                        "Sandbox debug server already has an active client. "
                        "Only one sandbox CLI may connect at a time — "
                        "wait for the other session to finish, or stop it."
                    ),
                })
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            return

        self._active_client = writer
        try:
            try:
                line = await reader.readline()
            except ValueError:
                await _send(writer, {"type": "error", "message": "Message too large."})
                return

            if not line:
                return

            try:
                msg = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                await _send(writer, {"type": "error", "message": "Invalid JSON."})
                return

            if not isinstance(msg, dict):
                await _send(writer, {"type": "error", "message": "Invalid JSON."})
                return

            cmd = msg.get("cmd")
            if cmd == "list":
                await self._handle_list(writer)
            elif cmd == "exec":
                await self._handle_exec(msg, reader, writer)
            elif cmd == "shell":
                await self._handle_shell(msg, reader, writer)
            else:
                await _send(writer, {"type": "error", "message": f"Unknown command: '{cmd}'."})
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            logger.exception("SandboxDebugServer: unexpected error handling client")
        finally:
            if self._active_client is writer:
                self._active_client = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    async def _handle_list(self, writer: asyncio.StreamWriter) -> None:
        try:
            import boxlite  # noqa: F401 — availability probe
        except ImportError:
            await _send(writer, {"type": "error", "message": "boxlite is not installed."})
            return

        from raven.sandbox._runtime import get_boxlite_runtime

        try:
            runtime = get_boxlite_runtime()
            boxes = await runtime.list_info()
        except Exception as exc:
            await _send(writer, {"type": "error", "message": f"Failed to list VMs: {exc}"})
            return

        vms = []
        for info in boxes:
            vms.append({
                "id": info.id,
                "name": getattr(info, "name", None),
                "owned": info.id in self._owned_ids,
                "status": info.state.status,
                "image": info.image,
                "cpus": info.cpus,
                "memory_mib": info.memory_mib,
                "created_at": getattr(info, "created_at", None),
            })
        await _send(writer, {"type": "vm_list", "vms": vms})

    # ------------------------------------------------------------------
    # VM resolution (shared by exec and shell)
    # ------------------------------------------------------------------

    async def _resolve_vm(
        self,
        vm_ref: str | None,
        writer: asyncio.StreamWriter,
        boxes: list,
    ):
        """Resolve vm_ref to a BoxInfo, sending an error and returning None on failure.

        boxes is the result of runtime.list() — passed in so callers can reuse it.
        """
        if vm_ref is None:
            # Auto-select: owned + running
            candidates = [b for b in boxes if b.id in self._owned_ids and b.state.status == "running"]
            if len(candidates) == 0:
                await _send(writer, {"type": "error", "message": "No running VMs. Start raven agent/gateway first."})
                return None
            if len(candidates) > 1:
                await _send(writer, {"type": "error", "message": "Multiple running VMs; use --vm to specify one."})
                return None
            return candidates[0]

        # ID match (all VMs)
        by_id = [b for b in boxes if b.id == vm_ref]
        if by_id:
            box = by_id[0]
            if box.id not in self._owned_ids:
                await _send(writer, {"type": "error", "message": f"VM {box.id} is not owned by this process."})
                return None
            if box.state.status != "running":
                await _send(writer, {"type": "error", "message": f"VM is not running: {box.id}."})
                return None
            return box

        # Name match (owned VMs only)
        by_name = [b for b in boxes if getattr(b, "name", None) == vm_ref]
        if len(by_name) > 1:
            await _send(writer, {"type": "error", "message": f"Ambiguous: multiple VMs named '{vm_ref}', use VM ID instead."})
            return None
        if len(by_name) == 1:
            box = by_name[0]
            if box.id not in self._owned_ids:
                await _send(writer, {"type": "error", "message": f"VM {box.id} is not owned by this process."})
                return None
            if box.state.status != "running":
                await _send(writer, {"type": "error", "message": f"VM is not running: {box.id}."})
                return None
            return box

        await _send(writer, {"type": "error", "message": f"No VM found with ID or name '{vm_ref}'."})
        return None

    async def _attach_box(self, vm_ref: str | None, writer: asyncio.StreamWriter):
        """Resolve vm_ref and return a live boxlite.Box, or None on failure.

        On any failure (boxlite missing / list_info / resolution / get) an error
        message is sent to the client; the caller must just return when None is
        returned.
        """
        try:
            import boxlite  # noqa: F401 — availability probe (consistent with _handle_list)
        except ImportError:
            await _send(writer, {"type": "error", "message": "boxlite is not installed."})
            return None

        from raven.sandbox._runtime import get_boxlite_runtime

        try:
            runtime = get_boxlite_runtime()
            boxes = await runtime.list_info()
        except Exception as exc:
            await _send(writer, {"type": "error", "message": f"Failed to list VMs: {exc}"})
            return None

        box_info = await self._resolve_vm(vm_ref, writer, boxes)
        if box_info is None:
            return None

        try:
            return await runtime.get(box_info.id)
        except Exception as exc:
            await _send(writer, {"type": "error", "message": f"Failed to attach to VM: {exc}"})
            return None

    # ------------------------------------------------------------------
    # exec
    # ------------------------------------------------------------------

    async def _handle_exec(
        self,
        msg: dict,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        program = msg.get("program")
        if not program:
            await _send(writer, {"type": "error", "message": "exec: 'program' must be a non-empty string."})
            return
        args = msg.get("args") or []

        box = await self._attach_box(msg.get("vm_ref"), writer)
        if box is None:
            return

        try:
            execution = await box.exec(program, list(args))
        except Exception as exc:
            await _send(writer, {"type": "error", "message": f"Failed to start execution: {exc}"})
            return

        async def _stream_stdout():
            try:
                async for chunk in execution.stdout():
                    data = base64.b64encode(chunk.encode() if isinstance(chunk, str) else chunk).decode()
                    await _send(writer, {"type": "stdout", "data": data})
            except (ConnectionResetError, BrokenPipeError):
                pass

        async def _stream_stderr():
            try:
                async for chunk in execution.stderr():
                    data = base64.b64encode(chunk.encode() if isinstance(chunk, str) else chunk).decode()
                    await _send(writer, {"type": "stderr", "data": data})
            except (ConnectionResetError, BrokenPipeError):
                pass

        async def _watch_disconnect():
            # P1.3: client disconnect detector. The exec protocol does not expect
            # any client→server traffic after the initial command, so a successful
            # readline() (stray data) is ignored, and an empty result means the
            # client closed its half of the socket — at which point we must stop
            # waiting on the long-running VM process and kill it.
            while True:
                try:
                    line = await reader.readline()
                except (ConnectionResetError, BrokenPipeError, ValueError):
                    return
                if not line:
                    return

        async def _both_streams():
            await asyncio.gather(_stream_stdout(), _stream_stderr())

        async def _do_wait():
            # boxlite's Execution.wait() returns a Future (not a coroutine), so
            # create_task() can't take it directly — wrap in an async fn that
            # awaits it. This also keeps unit-test AsyncMock paths working.
            return await execution.wait()

        streams_task = asyncio.create_task(_both_streams())
        wait_task = asyncio.create_task(_do_wait())
        watcher = asyncio.create_task(_watch_disconnect())

        try:
            done, _ = await asyncio.wait(
                {wait_task, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            if wait_task in done:
                # Process exited first — drain remaining stdout/stderr (bounded so
                # a misbehaving stream can't keep the connection open forever),
                # then send the exit code.
                try:
                    await asyncio.wait_for(asyncio.shield(streams_task), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:
                    pass
                try:
                    result = wait_task.result()
                    await _send(writer, {"type": "exit", "code": result.exit_code})
                except (ConnectionResetError, BrokenPipeError):
                    pass
                except Exception as exc:
                    try:
                        await _send(writer, {"type": "error", "message": f"Execution failed: {exc}"})
                    except (ConnectionResetError, BrokenPipeError):
                        pass
            else:
                # Client disconnected first — stop the VM process so we don't leak it.
                try:
                    await execution.kill()
                except Exception:
                    pass
        finally:
            for t in (streams_task, wait_task, watcher):
                t.cancel()
            await asyncio.gather(streams_task, wait_task, watcher, return_exceptions=True)

    # ------------------------------------------------------------------
    # shell
    # ------------------------------------------------------------------

    async def _handle_shell(
        self,
        msg: dict,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        shell_path = msg.get("shell")
        if not shell_path:
            await _send(writer, {"type": "error", "message": "shell: 'shell' must be a non-empty string."})
            return

        box = await self._attach_box(msg.get("vm_ref"), writer)
        if box is None:
            return

        try:
            execution = await box.exec(shell_path, [], tty=True)
        except Exception as exc:
            await _send(writer, {"type": "error", "message": f"Failed to start shell: {exc}"})
            return

        await _send(writer, {"type": "ready"})

        done_event = asyncio.Event()
        stdout_task: asyncio.Task | None = None

        async def _stdout_task_fn():
            try:
                async for chunk in execution.stdout():
                    raw = chunk.encode() if isinstance(chunk, str) else chunk
                    await _send(writer, {"type": "stdout", "data": base64.b64encode(raw).decode()})
            except (ConnectionResetError, BrokenPipeError):
                pass

        async def _wait_task():
            try:
                result = await execution.wait()
                # P1.4: drain remaining stdout before announcing exit. Without this,
                # the client breaks on `exit` and loses the last few stdout chunks
                # that are still queued in stdout_task. Bound the wait so a stuck
                # stdout iterator can't deadlock the session forever.
                if stdout_task is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(stdout_task), timeout=1.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                    except Exception:
                        pass
                await _send(writer, {"type": "exit", "code": result.exit_code})
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                done_event.set()

        # Outstanding fire-and-forget resize tasks. Tracked here so the
        # outer cleanup can cancel/await them — otherwise a slow resize_tty
        # pending at teardown would keep `execution` alive past the handler.
        resize_tasks: list[asyncio.Task] = []

        async def _stdin_task():
            stdin_writer = execution.stdin()

            async def _do_resize(r: int, c: int) -> None:
                # Resize is fire-and-forget — never block stdin forwarding on it,
                # since a slow/hung resize_tty (e.g. during shell startup) would
                # otherwise stall every keystroke behind it.
                try:
                    await execution.resize_tty(rows=r, cols=c)
                except Exception:
                    pass

            # P1.2: any path out of this loop — clean EOF, exception, cancellation —
            # must fire done_event so an idle shell whose client just walked away
            # is torn down instead of becoming an orphan inside the VM.
            #
            # Race each readline() against done_event.wait() so an exit triggered
            # by _wait_task is observed immediately instead of after a polling
            # tick.
            done_wait = asyncio.create_task(done_event.wait())
            try:
                while True:
                    read_task = asyncio.create_task(reader.readline())
                    finished, _ = await asyncio.wait(
                        {read_task, done_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if done_wait in finished:
                        await _cancel_and_collect(read_task)
                        break
                    line = read_task.result()
                    if not line:
                        break
                    try:
                        client_msg = json.loads(line.decode())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    ccmd = client_msg.get("cmd")
                    if ccmd == "stdin":
                        raw = base64.b64decode(client_msg.get("data", ""))
                        if raw:
                            await stdin_writer.send_input(raw)
                    elif ccmd == "resize":
                        rows = client_msg.get("rows", 0)
                        cols = client_msg.get("cols", 0)
                        if rows >= 1 and cols >= 1:
                            resize_tasks.append(
                                asyncio.create_task(_do_resize(rows, cols))
                            )
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                await _cancel_and_collect(done_wait)
                done_event.set()

        stdout_task = asyncio.create_task(_stdout_task_fn())
        tasks = [
            stdout_task,
            asyncio.create_task(_wait_task()),
            asyncio.create_task(_stdin_task()),
        ]
        try:
            await done_event.wait()
        finally:
            for t in (*tasks, *resize_tasks):
                t.cancel()
            await asyncio.gather(*tasks, *resize_tasks, return_exceptions=True)
            try:
                await execution.kill()
            except Exception:
                pass


# ------------------------------------------------------------------
# Shared framing helper (used by exec/shell handlers too)
# ------------------------------------------------------------------

async def _send(writer: asyncio.StreamWriter, obj: dict) -> None:
    writer.write((json.dumps(obj) + "\n").encode())
    await writer.drain()
