"""Boxlite microVM-based SandboxExecutor implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.sandbox.interfaces import ExecResult, SandboxExecutor, SandboxInitError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class BoxliteExecutor(SandboxExecutor):
    """
    A single boxlite Box whose lifecycle is tied to AgentLoop (per-loop instance).

    Uses the raw boxlite.Box API (via raven's boxlite runtime) rather than
    SimpleBox so that both one-shot exec() and streaming start_process() can share
    the same running VM. SimpleBox.exec() returns ExecResult directly and does not
    expose the Execution object needed for stdin/stdout streaming.

    The workspace directory is mounted at /workspace (rw); all commands default to
    /workspace. The Box is eagerly created inside start() so VM startup errors
    surface before the agent loop begins.

    BoxOptions.volumes expects List[Tuple[str, str, str]]; lists are coerced to
    tuples at construction time. BoxOptions.env expects List[Tuple[str, str]];
    exec-level env dicts are converted to tuples before being passed to Box.exec().
    Box.exec() does not accept cwd or timeout parameters (SimpleBox-only kwargs);
    cwd is injected via the shell command and timeout via asyncio.wait_for().
    """

    WORKSPACE_MOUNT = "/workspace"

    def __init__(
        self,
        image: str,
        workspace: Path,
        cpus: int = 2,
        memory_mib: int = 2048,
        disk_size_gb: int | None = None,
        allow_net: bool | list[str] = True,
        extra_volumes: list[list[str]] | None = None,
        default_timeout: int = 120,
        verify_timeout: int = 30,
        create_timeout: int = 300,
        owned_ids: set[str] | None = None,
    ):
        self._image = image
        self._workspace = workspace
        self._cpus = cpus
        self._memory_mib = memory_mib
        self._disk_size_gb = disk_size_gb
        self._allow_net = allow_net
        self._extra_volumes = extra_volumes or []
        self._default_timeout = default_timeout
        self._verify_timeout = verify_timeout
        self._create_timeout = create_timeout
        self._owned_ids = owned_ids

        self._box: Any | None = None  # boxlite.Box, lazy-imported
        self._stack = AsyncExitStack()
        self._init_lock = asyncio.Lock()
        self._process_tasks: list[asyncio.Task] = []
        self._process_executions: list[Any] = []  # list[boxlite.Execution]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the working Box and verify it is responsive.

        For allow_net=True (the common case) the working Box is created directly.
        A separate throwaway SimpleBox is used only when allow_net is restricted
        so the image can be pulled via the host's unrestricted network before the
        working Box is created with its restricted sandbox network policy.

        On any failure after the box has been created (box.start(), _verify), we
        run stop() before re-raising so a partially-initialised VM is never left
        behind. The caller's AsyncExitStack will not call __aexit__ when
        __aenter__/start() raises, so cleanup must happen here.
        """
        try:
            await self._start_inner()
        except BaseException:
            # Catch BaseException (not Exception) so cleanup also runs on
            # KeyboardInterrupt / SystemExit / asyncio.CancelledError. A leaked
            # half-started VM is worse than the cleanup itself raising; we
            # re-raise immediately so the original signal still propagates.
            await self._cleanup_after_failed_start()
            raise

    async def _start_inner(self) -> None:
        if self._allow_net is not True:
            await self._pull_image()
        try:
            box = await asyncio.wait_for(self._ensure_box(), timeout=self._create_timeout)
        except asyncio.TimeoutError:
            raise SandboxInitError(
                f"Sandbox VM creation timed out after {self._create_timeout}s "
                "(image pull or VM initialisation took too long).\n"
                "  • Check network connectivity and registry availability\n"
                "  • Increase create_timeout in sandbox config for large images\n"
                "  • Pre-pull the image to skip network delay: boxlite pull <image>"
            )
        await self._verify(box)

    async def _cleanup_after_failed_start(self) -> None:
        """Run any cleanup callbacks queued during a partial start.

        Mirrors stop() but only for the lifecycle pieces that may have been set
        up before the failure (no MCP bridges, no process_executions).
        """
        try:
            await self._stack.aclose()
        except Exception as exc:
            logger.warning("Error during failed-start cleanup: %s", exc)

    async def _pull_image(self) -> None:
        """Pre-pull the OCI image via a throwaway SimpleBox with unrestricted networking."""
        import boxlite

        from raven.sandbox._runtime import get_boxlite_runtime

        async def _do_pull() -> None:
            # Use raven's runtime so the image lands in the same cache the
            # working Box reads from (otherwise SimpleBox would default to ~/.boxlite).
            async with boxlite.SimpleBox(
                image=self._image,
                cpus=1,
                memory_mib=256,
                runtime=get_boxlite_runtime(),
            ) as pull_box:
                result = await pull_box.exec("sh", "-c", "echo ok", timeout=15)
                if result.exit_code != 0 or result.stdout.strip() != "ok":
                    raise SandboxInitError(
                        f"boxlite image pre-pull check returned unexpected result "
                        f"(exit_code={result.exit_code}, stdout={result.stdout!r})"
                    )

        try:
            await asyncio.wait_for(_do_pull(), timeout=self._create_timeout)
        except asyncio.TimeoutError:
            raise SandboxInitError(
                f"Image pre-pull timed out after {self._create_timeout}s.\n"
                "  • Check network connectivity and registry availability\n"
                "  • Increase create_timeout in sandbox config for large images\n"
                "  • Pre-pull the image on a network-connected machine: boxlite pull <image>"
            )
        except SandboxInitError:
            raise
        except Exception as exc:
            raise SandboxInitError(
                f"Cannot initialise sandbox (image pre-pull failed): {exc}\n"
                f"  • Ensure boxlite is installed:  pip install raven[sandbox]\n"
                f"  • macOS: requires Apple Silicon M1+ and macOS 12+\n"
                f"  • Linux: requires /dev/kvm accessible to the current user"
            ) from exc

    async def _verify(self, box: Any) -> None:
        """Run `echo ok` inside the working Box to confirm it is live."""
        execution: Any = None

        async def _check() -> tuple[str, int]:
            nonlocal execution
            execution = await box.exec("sh", ["-c", "echo ok"])
            stdout_str, _ = await asyncio.gather(
                self._collect(execution.stdout()),
                self._collect(execution.stderr()),
            )
            result = await execution.wait()
            return stdout_str, result.exit_code

        try:
            stdout_str, exit_code = await asyncio.wait_for(_check(), timeout=self._verify_timeout)
        except asyncio.TimeoutError:
            if execution is not None:
                try:
                    await execution.kill()
                except Exception:
                    pass
            raise SandboxInitError(
                f"Sandbox verification timed out after {self._verify_timeout}s "
                "— VM may be unresponsive.\n"
                "  • Check available memory and CPU on the host\n"
                "  • Try restarting the boxlite runtime\n"
                "  • Increase verify_timeout in sandbox config if the host is slow"
            )
        except Exception as exc:
            raise SandboxInitError(
                f"Cannot initialise sandbox: {exc}\n"
                f"  • Ensure boxlite is installed:  pip install raven[sandbox]\n"
                f"  • macOS: requires Apple Silicon M1+ and macOS 12+\n"
                f"  • Linux: requires /dev/kvm accessible to the current user"
            ) from exc
        if exit_code != 0 or stdout_str.strip() != "ok":
            raise SandboxInitError(
                f"Sandbox verification returned unexpected result (exit_code={exit_code}, stdout={stdout_str!r})"
            )

    async def stop(self) -> None:
        """Kill MCP server processes, cancel bridge tasks, then clean up the Box."""
        for exec_ in self._process_executions:
            try:
                await exec_.kill()
            except Exception:
                pass
        self._process_executions.clear()

        for task in self._process_tasks:
            task.cancel()
        if self._process_tasks:
            await asyncio.gather(*self._process_tasks, return_exceptions=True)
        self._process_tasks.clear()

        await self._stack.aclose()
        self._box = None
        logger.info("Sandbox stopped")

    async def _ensure_box(self) -> Any:
        async with self._init_lock:
            if self._box is not None:
                return self._box

            import boxlite

            # boxlite 0.8.2: volumes are dicts, not tuples
            volumes = [
                {"host": str(self._workspace), "guest": self.WORKSPACE_MOUNT, "readonly": False},
                *[{"host": e[0], "guest": e[1], "readonly": e[2] == "ro"} for e in self._extra_volumes],
            ]
            # boxlite 0.8.2: network is a string field; allow_net is a separate list field
            # (NetworkSpec does not exist in this version)
            extra_kwargs: dict = {}
            if self._allow_net is False:
                extra_kwargs["network"] = "none"
            elif isinstance(self._allow_net, list):
                extra_kwargs["allow_net"] = self._allow_net
            # else: allow_net is True → fully open, no kwargs needed

            options = boxlite.BoxOptions(
                image=self._image,
                cpus=self._cpus,
                memory_mib=self._memory_mib,
                disk_size_gb=self._disk_size_gb,
                volumes=volumes,
                **extra_kwargs,
            )
            from raven.sandbox._runtime import get_boxlite_runtime

            runtime = get_boxlite_runtime()
            self._box = await runtime.create(options)
            # Register cleanup *before* start() so a failure inside start() still
            # tears the box down — otherwise a partially-started VM leaks.
            if self._owned_ids is not None:
                self._owned_ids.add(self._box.id)
            self._stack.push_async_callback(self._cleanup_box)
            await self._box.start()  # create() does not auto-start; start() is required
            logger.info("Sandbox started (image=%s)", self._image)
            return self._box

    async def _cleanup_box(self) -> None:
        if self._box is not None:
            box_id = self._box.id
            try:
                from raven.sandbox._runtime import get_boxlite_runtime

                await self._box.stop()
                try:
                    # Box has no remove() in 0.8.2 — use the runtime
                    await get_boxlite_runtime().remove(box_id)
                except Exception:
                    pass  # box may already be gone after stop()
            except Exception as exc:
                logger.warning("Error cleaning up sandbox box: %s", exc)
            finally:
                # Drop ownership only after the VM is fully torn down. If we
                # discarded earlier, a debug client racing the cleanup would
                # see a still-running VM marked unowned and may try to rm it.
                if self._owned_ids is not None:
                    self._owned_ids.discard(box_id)
                self._box = None

    @staticmethod
    async def _collect(stream: AsyncIterator[str]) -> str:
        """Consume an Execution.stdout() or .stderr() async iterator into a single string.

        Assumes boxlite yields decoded str lines, not bytes. Guards against both SDK
        conventions: if lines already include a trailing '\n' they are kept as-is;
        if they don't, '\n' is appended.
        """
        lines = [line async for line in stream]
        if not lines:
            return ""
        return "".join(line if line.endswith("\n") else line + "\n" for line in lines)

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        box = await self._ensure_box()
        effective_timeout = self._default_timeout if timeout is None else timeout
        vm_cwd = self._translate_cwd(cwd)

        # boxlite 0.8.2: Box.exec() accepts cwd directly — no shell injection needed
        env_tuples = list(env.items()) if env else None

        execution: Any = None

        async def _run() -> ExecResult:
            nonlocal execution
            if env_tuples:
                execution = await box.exec("sh", ["-c", command], env_tuples, cwd=vm_cwd)
            else:
                execution = await box.exec("sh", ["-c", command], cwd=vm_cwd)
            stdout_str, stderr_str = await asyncio.gather(
                self._collect(execution.stdout()),
                self._collect(execution.stderr()),
            )
            result = await execution.wait()
            return ExecResult(
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=result.exit_code,
            )

        try:
            return await asyncio.wait_for(_run(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            if execution is not None:
                try:
                    await execution.kill()
                except Exception:
                    pass
            return ExecResult(
                stdout="",
                stderr=f"Command timed out after {effective_timeout}s",
                exit_code=-1,
            )

    # ------------------------------------------------------------------
    # Process spawning (MCP stdio servers)
    # ------------------------------------------------------------------

    @property
    def supports_process_spawning(self) -> bool:
        return True

    async def start_process(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> tuple[Any, Any]:
        """Start a long-running process inside the VM and return MCP-compatible streams.

        Returns (read_stream, write_stream):
          read_stream  — anyio MemoryObjectReceiveStream[JSONRPCMessage | Exception]
          write_stream — anyio MemoryObjectSendStream[JSONRPCMessage]

        Both streams are compatible with the MCP SDK's ClientSession constructor.
        The caller owns write_send.aclose() — typically handled by MCP ClientSession.__aexit__.
        """
        import anyio
        from mcp.shared.message import SessionMessage
        from mcp.types import JSONRPCMessage

        box = await self._ensure_box()

        env_tuples = list(env.items()) if env else None
        if env_tuples:
            execution = await box.exec(command, list(args), env_tuples)
        else:
            execution = await box.exec(command, list(args))
        self._process_executions.append(execution)

        read_send, read_recv = anyio.create_memory_object_stream(max_buffer_size=16)
        write_send, write_recv = anyio.create_memory_object_stream(max_buffer_size=16)

        stdout_iter = execution.stdout()
        stderr_iter = execution.stderr()
        stdin_writer = execution.stdin()

        async def _stdout_bridge() -> None:
            # MCP SDK 1.x: read stream carries SessionMessage (wrapping JSONRPCMessage).
            # boxlite stdout yields chunks (not guaranteed to be line-aligned for large
            # messages). Buffer and split on '\n' to reconstruct complete JSON lines.
            buf = ""
            try:
                async for chunk in stdout_iter:
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rpc_msg = JSONRPCMessage.model_validate_json(line)
                            await read_send.send(SessionMessage(message=rpc_msg))
                        except Exception as parse_exc:
                            # Non-JSON stdout (e.g. startup banners). Log and skip.
                            logger.debug(
                                "MCP stdout [%s]: skipping non-JSON line %r (%s)",
                                command,
                                line[:80],
                                parse_exc,
                            )
                # Flush any remaining content after stream ends (no trailing newline)
                line = buf.strip()
                if line:
                    try:
                        rpc_msg = JSONRPCMessage.model_validate_json(line)
                        await read_send.send(SessionMessage(message=rpc_msg))
                    except Exception:
                        pass
            except (anyio.ClosedResourceError, anyio.EndOfStream):
                pass
            except Exception as exc:
                logger.error("MCP stdout bridge error: %s", exc)
            finally:
                await read_send.aclose()

        async def _stderr_bridge() -> None:
            """VM stderr → logger (WARNING)."""
            try:
                async for raw_line in stderr_iter:
                    line = raw_line.strip()
                    if line:
                        logger.warning("MCP server stderr [%s]: %s", command, line)
            except (anyio.ClosedResourceError, anyio.EndOfStream):
                pass
            except Exception as exc:
                logger.error("MCP stderr bridge error: %s", exc)

        async def _stdin_bridge() -> None:
            # MCP SDK 1.x: write stream carries SessionMessage; serialize the inner
            # JSONRPCMessage as newline-delimited JSON to the VM process stdin.
            try:
                async for session_msg in write_recv:
                    rpc_msg = session_msg.message if isinstance(session_msg, SessionMessage) else session_msg
                    line = rpc_msg.model_dump_json(exclude_none=True, by_alias=True) + "\n"
                    await stdin_writer.send_input(line.encode("utf-8"))
            except (anyio.ClosedResourceError, anyio.EndOfStream):
                pass
            except Exception as exc:
                logger.error("MCP stdin bridge error: %s", exc)
            finally:
                await write_recv.aclose()

        # No await between create_task and extend — asyncio is single-threaded so no
        # context switch can occur here. stop() is therefore guaranteed to see all three tasks.
        task1 = asyncio.create_task(_stdout_bridge())
        task2 = asyncio.create_task(_stderr_bridge())
        task3 = asyncio.create_task(_stdin_bridge())
        self._process_tasks.extend([task1, task2, task3])

        return read_recv, write_send

    def _translate_cwd(self, cwd: str | None) -> str:
        """Map host workspace path → VM /workspace/... path."""
        if cwd is None:
            return self.WORKSPACE_MOUNT
        host_path = Path(cwd).resolve()
        try:
            rel = host_path.relative_to(self._workspace.resolve())
            rel_str = str(rel)
            return self.WORKSPACE_MOUNT if rel_str == "." else f"{self.WORKSPACE_MOUNT}/{rel_str}"
        except ValueError:
            logger.warning("cwd '%s' is outside workspace; falling back to /workspace", cwd)
            return self.WORKSPACE_MOUNT
