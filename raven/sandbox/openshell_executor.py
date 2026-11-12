"""SandboxExecutor backed by an NVIDIA OpenShell sandbox on a local gateway (Docker driver).

One executor owns one sandbox. The workspace and any extra volumes are bind
mounted into it, its filesystem and network reach come from a policy built
here, and every command reaches it through the gateway's exec API.

Every call into the OpenShell SDK sits behind ``OpenShellControlPlane`` so the
executor can be exercised without a gateway, and so an SDK change touches one
class.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import queue
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from raven.sandbox.denials import NetworkDenial, parse_denials
from raven.sandbox.interfaces import ExecResult, SandboxExecutor, SandboxInitError
from raven.sandbox.mounts import WORKSPACE_MOUNT, MountTable
from raven.sandbox.network_policy import (
    NetworkGrant,
    compile_network_policy,
    grant_rules,
    network_from_allow_net,
    policy_digest,
)
from raven.utils.pid import pid_alive

logger = logging.getLogger(__name__)

_READ_ONLY_BASELINE = ("/bin", "/usr", "/lib", "/proc", "/dev/urandom", "/etc", "/var/log")
_READ_WRITE_BASELINE = ("/tmp", "/dev/null")
# How long one exec call may take past the command's own budget before the
# transport gives up on it.
_TIMEOUT_GRACE_SECONDS = 10.0
# How long a command that ran out of budget may keep reporting before the
# call is killed. The gateway enforces the same budget and answers with its
# own exit 124, so this only covers the report, not the command.
_KILL_GRACE_SECONDS = 1.0
# A sandbox ships its log lines to the gateway in batches, a quarter second to
# about two seconds after they happen, so a failed command's denials may not be
# readable yet when it ends. The sandbox logs the close of each command's relay
# after everything the command did: once a close at least that late is
# readable, so are the command's denials. A read that does not answer in time
# is given up rather than waited on.
_DENIAL_READ_TIMEOUT = 2.0
_DENIAL_WAIT_SECONDS = 3.0
_DENIAL_POLL_SECONDS = 0.15
_RELAY_CLOSED = "ssh relay closed"
# How much earlier than the caller sees a command end the sandbox may log its close.
_RELAY_CLOSE_SLACK = 0.25
# How long a changed network policy may take to load in a running sandbox:
# about 1 s with the gateway's push delivery, about 11 s with poll.
_POLICY_WAIT_SECONDS = 15.0
_POLICY_POLL_SECONDS = 0.25
_SERVER_TIMEOUT_EXIT = 124
_OWNER_LABEL = "raven.owner"
_PID_LABEL = "raven.pid"


_reaped = False
_pending_deletes: set[asyncio.Task] = set()


class ExecStream(Protocol):
    """A running command: iterating yields ``("stdout"|"stderr", bytes)`` and finally ``("exit", int)``."""

    def __iter__(self) -> Iterator[tuple[str, Any]]: ...

    def cancel(self) -> None: ...


class InteractiveStream(ExecStream, Protocol):
    def write(self, data: bytes) -> None: ...

    def close_input(self) -> None: ...


class OpenShellControlPlane(Protocol):
    """The gateway operations the executor needs. Implementations are synchronous."""

    def create(
        self,
        *,
        name: str,
        image: str,
        policy: dict,
        driver_config: dict,
        resources: dict,
        labels: dict[str, str],
    ) -> None: ...

    def wait_ready(self, name: str, timeout_seconds: float) -> None: ...

    def exec(
        self, name: str, argv: list[str], *, workdir: str, env: dict[str, str], timeout_seconds: int | None
    ) -> ExecStream: ...

    def interactive(self, name: str, argv: list[str], *, workdir: str, env: dict[str, str]) -> InteractiveStream: ...

    def delete(self, name: str) -> None: ...

    def list_labeled(self, selector: str) -> list[tuple[str, dict[str, str]]]: ...

    def ocsf_lines(self, name: str, since: float) -> list[tuple[float, str]]: ...

    def merge_rules(self, name: str, add: dict[str, dict], remove: list[str]) -> int: ...

    def wait_policy(self, name: str, version: int, timeout: float) -> bool: ...


def sandbox_policy(
    *, network_policies: dict[str, dict], mounts: list[tuple[str, str, bool]], uid: int | None, gid: int | None = None
) -> dict:
    """The OpenShell policy for one sandbox, in the field names of its ``SandboxPolicy`` proto.

    A policy sent with a sandbox replaces OpenShell's default policy rather
    than merging with it, so the default's read-only system paths are
    restated here.
    """
    read_only = list(_READ_ONLY_BASELINE)
    read_write = list(_READ_WRITE_BASELINE)
    for _host, guest, is_read_only in mounts:
        (read_only if is_read_only else read_write).append(guest)
    policy: dict = {
        "version": 1,
        "filesystem": {"include_workdir": True, "read_only": read_only, "read_write": read_write},
        "landlock": {"compatibility": "best_effort"},
    }
    if uid is not None and uid > 0:
        # OpenShell refuses a root workload, so a root host keeps the image's
        # user; a non-root host runs as itself so bind-mounted files stay
        # writable. A host without a uid leaves the identity to the driver.
        policy["process"] = {"run_as_user": str(uid), "run_as_group": str(gid if gid else uid)}
    if network_policies:
        policy["network_policies"] = network_policies
    return policy


def _host_uid() -> int | None:
    """The host user's uid, or None on a platform that has none."""
    getter = getattr(os, "getuid", None)
    return getter() if getter is not None else None


def _host_gid() -> int | None:
    """The host user's gid, or None on a platform that has none."""
    getter = getattr(os, "getgid", None)
    return getter() if getter is not None else None


def _owner_digest() -> str:
    return hashlib.sha256(f"{socket.gethostname()}:{_host_uid()}".encode()).hexdigest()[:14]


async def drain_pending_deletes() -> None:
    """Wait for the background sandbox deletions started on the running loop.

    The registry is process-wide; a deletion another loop started can only be
    awaited there, and one stranded on a closed loop never finishes.
    """
    loop = asyncio.get_running_loop()
    while mine := [task for task in _pending_deletes if task.get_loop() is loop]:
        await asyncio.gather(*mine, return_exceptions=True)


def _spawn_delete(control_plane: OpenShellControlPlane, name: str) -> None:
    async def run() -> None:
        try:
            await asyncio.to_thread(control_plane.delete, name)
        except Exception as exc:
            logger.warning("Could not delete OpenShell sandbox %s: %s", name, exc)

    task = asyncio.get_running_loop().create_task(run())
    _pending_deletes.add(task)
    task.add_done_callback(_pending_deletes.discard)


def _drain(stream: ExecStream) -> ExecResult:
    stdout, stderr, exit_code = bytearray(), bytearray(), -1
    for kind, payload in stream:
        if kind == "stdout":
            stdout += payload
        elif kind == "stderr":
            stderr += payload
        elif kind == "exit":
            exit_code = int(payload)
    return ExecResult(
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        exit_code=exit_code,
    )


async def _drain_to_result(stream: ExecStream) -> ExecResult:
    """Drain a command's output, ending quietly on a stream that broke or was cancelled.

    A command nobody waits for any more -- a timeout, a cancelled caller --
    leaves the stream raising on its way out, and a task that ends on an
    exception nobody reads is reported by asyncio as an unretrieved error.
    """
    try:
        return await asyncio.to_thread(_drain, stream)
    except Exception as exc:
        logger.debug("OpenShell command stream ended with %s", exc)
        return ExecResult(stdout="", stderr="", exit_code=-1)


async def _collect(drain: asyncio.Task, grace: float) -> ExecResult | None:
    """The result of a drain that is already running, or None if it does not land in time."""
    try:
        return await asyncio.wait_for(asyncio.shield(drain), grace)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None


class OpenShellExecutor(SandboxExecutor):
    """One OpenShell sandbox whose lifetime is this executor's ``start()`` to ``stop()``."""

    def __init__(
        self,
        image: str,
        workspace: Path,
        cpus: int = 2,
        memory_mib: int = 2048,
        allow_net: bool | list[str] = True,
        extra_volumes: list[list[str]] | None = None,
        default_timeout: int = 120,
        verify_timeout: int = 30,
        create_timeout: int = 300,
        *,
        control_plane: OpenShellControlPlane | None = None,
        gateway: str | None = None,
        gateway_workspace: str = "default",
        network_source: Callable[[], dict[str, dict]] | None = None,
    ) -> None:
        self._image = image
        self._mounts = MountTable(Path(workspace), extra_volumes or [])
        self._cpus = cpus
        self._memory_mib = memory_mib
        # The network policies this sandbox should run under, read when it
        # starts; ``allow_net`` is the fixed policy of a caller that names none.
        if network_source is None:
            fixed = compile_network_policy(network_from_allow_net(allow_net))
            network_source = lambda: fixed  # noqa: E731
        self._network_source = network_source
        self._applied_digest: str | None = None
        self._applied_policies: dict[str, dict] = {}
        self._last_source: dict[str, dict] | None = None
        # Rules granted for this sandbox only ("allow once"), kept across syncs.
        self._local_rules: dict[str, dict] = {}
        self._default_timeout = default_timeout
        self._verify_timeout = verify_timeout
        self._create_timeout = create_timeout
        self._control_plane = control_plane
        self._gateway = gateway
        self._gateway_workspace = gateway_workspace
        self._name: str | None = None
        self._start_lock = asyncio.Lock()
        self._processes: list[InteractiveStream] = []
        self._tasks: list[asyncio.Task] = []

    @property
    def supports_process_spawning(self) -> bool:
        return True

    @property
    def supports_network_grants(self) -> bool:
        return True

    def _plane(self) -> OpenShellControlPlane:
        if self._control_plane is None:
            self._control_plane = SDKControlPlane(gateway=self._gateway, workspace=self._gateway_workspace)
        return self._control_plane

    async def start(self) -> None:
        async with self._start_lock:
            if self._name is None:
                await self._start_locked()

    async def _start_locked(self) -> None:
        try:
            plane = await asyncio.to_thread(self._plane)
            await self._reap_orphans(plane)
            name = f"rv{secrets.token_hex(6)}"
            network_policies = self._network_source()
            await asyncio.to_thread(
                plane.create,
                name=name,
                image=self._image,
                policy=sandbox_policy(
                    network_policies=network_policies, mounts=self._mounts.mounts(), uid=_host_uid(), gid=_host_gid()
                ),
                driver_config=self._driver_config(),
                resources={"limits": {"cpu": str(self._cpus), "memory": f"{self._memory_mib}Mi"}},
                labels={_OWNER_LABEL: _owner_digest(), _PID_LABEL: str(os.getpid())},
            )
        except SandboxInitError:
            raise
        except Exception as exc:
            raise SandboxInitError(
                f"Cannot create an OpenShell sandbox: {exc}\n"
                "  - Check the gateway: openshell status\n"
                "  - The gateway must run as a single replica; raven addresses the replica its "
                "client is bound to\n"
                "  - The host needs Linux 6.2+ (Landlock ABI 3) and Docker 28+, with the gateway "
                "allowing bind mounts (enable_bind_mounts = true)"
            ) from exc
        try:
            try:
                await asyncio.to_thread(plane.wait_ready, name, self._create_timeout)
            except Exception as exc:
                raise SandboxInitError(self._not_ready_advice(name, exc)) from exc
            await self._verify(plane, name)
        except BaseException:
            _spawn_delete(plane, name)
            raise
        self._name = name
        self._applied_policies = dict(network_policies)
        self._applied_digest = policy_digest(network_policies)
        self._last_source = network_policies
        logger.info("OpenShell sandbox %s started (image=%s)", name, self._image)

    def _not_ready_advice(self, name: str, exc: Exception) -> str:
        advice = f"OpenShell sandbox {name} did not become ready: {exc}"
        if "bind source path does not exist" in str(exc) and any(
            host == "/tmp" or host.startswith("/tmp/") for host, _guest, _ro in self._mounts.mounts()
        ):
            advice += (
                "\n  - The packaged gateway service runs with systemd PrivateTmp, so it cannot see "
                "directories under /tmp; use a workspace outside /tmp"
            )
        return advice

    async def _verify(self, plane: OpenShellControlPlane, name: str) -> None:
        probe = f"if test -w {WORKSPACE_MOUNT}; then echo ok; else id -u; echo not-writable; fi"
        stream = await asyncio.to_thread(
            plane.exec, name, ["sh", "-c", probe], workdir="/", env={}, timeout_seconds=self._verify_timeout
        )
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_drain, stream), self._verify_timeout)
        except asyncio.TimeoutError as exc:
            stream.cancel()
            raise SandboxInitError(f"OpenShell sandbox {name} did not answer within {self._verify_timeout}s") from exc
        lines = result.stdout.split()
        if lines == ["ok"]:
            return
        if "not-writable" in lines:
            uid = lines[0] if lines else "?"
            advice = [
                "  - Run raven as a non-root user that owns the workspace, or",
                f"  - make the workspace writable by uid {uid}",
            ]
            if _host_uid() == 0:
                advice.insert(
                    0,
                    "  - raven is running as root, and OpenShell refuses a root workload, so the "
                    f"sandbox runs as the image's user (uid {uid})",
                )
            raise SandboxInitError(
                f"The sandbox user (uid {uid}) cannot write the workspace {self._mounts.workspace}.\n"
                + "\n".join(advice)
            )
        raise SandboxInitError(
            f"OpenShell sandbox {name} verification failed (exit_code={result.exit_code}, "
            f"stdout={result.stdout!r}, stderr={result.stderr!r})"
        )

    async def _reap_orphans(self, plane: OpenShellControlPlane) -> None:
        """Delete sandboxes this host user created from processes that no longer exist.

        OpenShell has no sandbox expiry, so a raven killed before ``stop()``
        leaves its sandboxes running; the next raven on the host collects them.
        """
        global _reaped
        if _reaped:
            return
        try:
            found = await asyncio.to_thread(plane.list_labeled, f"{_OWNER_LABEL}={_owner_digest()}")
        except Exception as exc:
            logger.warning("Could not list OpenShell sandboxes for cleanup: %s", exc)
            return
        _reaped = True
        for name, labels in found:
            pid = labels.get(_PID_LABEL, "")
            if pid.isdigit() and int(pid) != os.getpid() and not pid_alive(int(pid)):
                logger.info("Deleting OpenShell sandbox %s left by exited process %s", name, pid)
                _spawn_delete(plane, name)

    def _driver_config(self) -> dict:
        mounts = [
            {"type": "bind", "source": host, "target": guest, "read_only": read_only}
            for host, guest, read_only in self._mounts.mounts()
        ]
        return {"docker": {"mounts": mounts}}

    async def _running(self) -> tuple[OpenShellControlPlane, str]:
        await self.start()
        if self._name is None:
            raise SandboxInitError("The OpenShell sandbox stopped before the command could run")
        return self._plane(), self._name

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        plane, name = await self._running()
        await self._sync_network(plane, name)
        since = time.time()
        result = await self._run(plane, name, command, cwd, timeout, env)
        denials = await self._denials(plane, name, since, wait=result.exit_code > 0)
        return replace(result, denials=tuple(denials)) if denials else result

    async def sync_network(self) -> bool:
        """Bring the running sandbox's network policy up to its source; False when it is not running or did not load."""
        if self._name is None:
            return False
        return await self._sync_network(self._plane(), self._name)

    async def grant_network(self, grants: Sequence[NetworkGrant]) -> bool:
        """Open ``grants`` in this sandbox only, until it stops; False when it is not running or did not load."""
        if self._name is None:
            return False
        self._local_rules.update(grant_rules(grants))
        self._last_source = None
        return await self._sync_network(self._plane(), self._name)

    async def _sync_network(self, plane: OpenShellControlPlane, name: str) -> bool:
        try:
            source = self._network_source()
        except Exception as exc:  # noqa: BLE001 - keep running under the policy already loaded
            logger.warning("Could not read the network policy for OpenShell sandbox %s: %s", name, exc)
            return False
        if source is self._last_source:
            return True
        desired = {**source, **self._local_rules}
        digest = policy_digest(desired)
        if digest == self._applied_digest:
            self._last_source = source
            return True
        add = {rule: body for rule, body in desired.items() if self._applied_policies.get(rule) != body}
        # A rule whose body changed is removed and added again: OpenShell merges
        # an added rule into one of the same name, so a narrowed rule sent as an
        # add alone would keep what it dropped.
        remove = [rule for rule in self._applied_policies if rule not in desired or rule in add]
        try:
            version = await asyncio.to_thread(plane.merge_rules, name, add, remove)
            loaded = await asyncio.to_thread(plane.wait_policy, name, version, _POLICY_WAIT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - retried before the next command
            logger.warning("Could not update the network policy of OpenShell sandbox %s: %s", name, exc)
            return False
        if not loaded:
            logger.warning("OpenShell sandbox %s did not load network policy version %s in time", name, version)
            return False
        self._applied_policies = desired
        self._applied_digest = digest
        self._last_source = source
        return True

    async def denials_since(
        self, since: float, *, until: float | None = None, wait: bool = False
    ) -> list[NetworkDenial]:
        """Connections this sandbox refused since ``since``; none when it is not running.

        With ``wait``, the log is read again until each refusal names the
        program and the port, or the wait runs out: a long-lived process has
        no relay close to wait for.
        """
        if self._name is None:
            return []
        plane, name = self._plane(), self._name
        deadline = time.monotonic() + (_DENIAL_WAIT_SECONDS if wait else 0.0)
        denials: list[NetworkDenial] = []
        while True:
            try:
                lines = await asyncio.wait_for(asyncio.to_thread(plane.ocsf_lines, name, since), _DENIAL_READ_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - a missing denial list must not cost the call's result
                logger.debug("Could not read network denials for OpenShell sandbox %s: %s", name, exc)
                return denials
            denials = parse_denials(message for at, message in lines if until is None or at <= until)
            if (denials and all(denial.port is not None for denial in denials)) or time.monotonic() >= deadline:
                return denials
            await asyncio.sleep(_DENIAL_POLL_SECONDS)

    async def _denials(
        self, plane: OpenShellControlPlane, name: str, since: float, *, wait: bool
    ) -> list[NetworkDenial]:
        """The network denials logged for this sandbox since ``since``; none when the log cannot be read.

        With ``wait``, the log is read until the close of the command's relay has
        arrived, or until the wait runs out.
        """
        closed_after = time.time() - _RELAY_CLOSE_SLACK
        deadline = time.monotonic() + (_DENIAL_WAIT_SECONDS if wait else 0.0)
        denials: list[NetworkDenial] = []
        while True:
            try:
                lines = await asyncio.wait_for(asyncio.to_thread(plane.ocsf_lines, name, since), _DENIAL_READ_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - a missing denial list must not cost the command's result
                logger.debug("Could not read network denials for OpenShell sandbox %s: %s", name, exc)
                return denials
            denials = parse_denials(message for _, message in lines)
            closed = any(at >= closed_after and _RELAY_CLOSED in message for at, message in lines)
            if closed or time.monotonic() >= deadline:
                return denials
            await asyncio.sleep(_DENIAL_POLL_SECONDS)

    async def _run(
        self,
        plane: OpenShellControlPlane,
        name: str,
        command: str,
        cwd: str | None,
        timeout: int | None,
        env: dict[str, str] | None,
    ) -> ExecResult:
        effective = self._default_timeout if timeout is None else timeout
        stream = await asyncio.to_thread(
            plane.exec,
            name,
            ["sh", "-c", command],
            workdir=self._mounts.translate_cwd(cwd),
            env=dict(env or {}),
            timeout_seconds=effective or None,
        )
        drain = asyncio.ensure_future(_drain_to_result(stream))
        started = asyncio.get_running_loop().time()
        try:
            # timeout=0 times out at once, as it does for the direct executor.
            result = await asyncio.wait_for(asyncio.shield(drain), effective)
        except asyncio.TimeoutError:
            # The gateway enforces the same budget and reports it as exit 124,
            # so the command is already over; give that report a moment to
            # arrive before the call is killed.
            receipt = await _collect(drain, _KILL_GRACE_SECONDS if effective > 0 else 0)
            stream.cancel()
            note = f"Command timed out after {effective}s"
            if receipt is None:
                return ExecResult(stdout="", stderr=note, exit_code=-1)
            if receipt.exit_code == _SERVER_TIMEOUT_EXIT:
                return ExecResult(
                    stdout=receipt.stdout, stderr=f"{receipt.stderr}\n{note}" if receipt.stderr else note, exit_code=-1
                )
            if receipt.exit_code < 0:
                return ExecResult(stdout=receipt.stdout, stderr=note, exit_code=-1)
            return receipt
        except asyncio.CancelledError:
            stream.cancel()
            drain.cancel()
            raise
        # The gateway enforces the timeout itself and reports it as exit 124;
        # only an exit at the deadline is that, a quick 124 is the command's own.
        if result.exit_code == _SERVER_TIMEOUT_EXIT and asyncio.get_running_loop().time() - started >= effective:
            note = f"Command timed out after {effective}s"
            return ExecResult(
                stdout=result.stdout, stderr=f"{result.stderr}\n{note}" if result.stderr else note, exit_code=-1
            )
        return result

    async def start_process(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> tuple[Any, Any]:
        """Start a long-running process in the sandbox and bridge it to MCP SDK streams.

        ``command``, ``args``, ``env`` values and ``cwd`` are host paths; they
        are translated to their guest mounts here, and a cwd outside every
        mount is refused.
        """
        import anyio
        from mcp.shared.message import SessionMessage
        from mcp.types import JSONRPCMessage
        from pydantic import TypeAdapter

        workdir = self._mounts.translate_process_cwd(cwd)
        plane, name = await self._running()
        translate = self._mounts.translate_process_path
        argv = [translate(command), *(translate(arg) for arg in args)]
        guest_env = {key: translate(value) for key, value in (env or {}).items()}
        stream = await asyncio.to_thread(plane.interactive, name, argv, workdir=workdir, env=guest_env)
        self._processes.append(stream)

        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()

        def pump() -> None:
            try:
                for event in stream:
                    loop.call_soon_threadsafe(events.put_nowait, event)
            except Exception as exc:
                loop.call_soon_threadsafe(events.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(events.put_nowait, ("end", None))

        threading.Thread(target=pump, name=f"openshell-{name}-{command}", daemon=True).start()

        rpc = TypeAdapter(JSONRPCMessage)
        read_send, read_recv = anyio.create_memory_object_stream(max_buffer_size=16)
        write_send, write_recv = anyio.create_memory_object_stream(max_buffer_size=16)

        async def reader() -> None:
            buffer = b""
            try:
                while True:
                    kind, payload = await events.get()
                    if kind == "stdout":
                        buffer += payload
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            await self._forward(line, rpc, read_send, SessionMessage, command)
                    elif kind == "stderr":
                        for line in payload.decode("utf-8", errors="replace").splitlines():
                            if line.strip():
                                logger.warning("MCP server stderr [%s]: %s", command, line.strip())
                    elif kind == "error":
                        logger.error("MCP stream error [%s]: %s", command, payload)
                    elif kind == "end":
                        break
                if buffer.strip():
                    await self._forward(buffer, rpc, read_send, SessionMessage, command)
            except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream):
                pass
            finally:
                await read_send.aclose()

        async def writer() -> None:
            try:
                async for message in write_recv:
                    rpc_message = message.message if isinstance(message, SessionMessage) else message
                    stream.write((rpc_message.model_dump_json(exclude_none=True, by_alias=True) + "\n").encode("utf-8"))
            except (anyio.ClosedResourceError, anyio.EndOfStream):
                pass
            finally:
                stream.close_input()
                await write_recv.aclose()

        self._tasks.extend([asyncio.create_task(reader()), asyncio.create_task(writer())])
        return read_recv, write_send

    @staticmethod
    async def _forward(line: bytes, rpc, read_send, session_message, command: str) -> None:
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            message = rpc.validate_json(text)
        except Exception:
            logger.debug("MCP stdout [%s]: skipping non-JSON line %r", command, text[:80])
            return
        await read_send.send(session_message(message=message))

    async def stop(self) -> None:
        """Stop bridged processes and delete the sandbox in the background.

        Deletion blocks for seconds on the gateway, so it is not awaited here;
        ``drain_pending_deletes`` waits for it, and a sandbox orphaned by a
        crash is collected by the next process's ``start()``.
        """
        for stream in self._processes:
            stream.cancel()
        self._processes.clear()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        name, self._name = self._name, None
        if name is not None:
            _spawn_delete(self._plane(), name)
            logger.info("OpenShell sandbox %s stopping", name)


def build_sandbox_spec(*, image: str, policy: dict, driver_config: dict, resources: dict) -> Any:
    """The ``SandboxSpec`` proto for one sandbox; ``ParseDict`` fills the Struct-typed fields from dicts."""
    from google.protobuf.json_format import ParseDict
    from openshell._proto import openshell_pb2

    return ParseDict(
        {
            "template": {"image": image, "driver_config": driver_config, "resources": resources},
            "policy": policy,
        },
        openshell_pb2.SandboxSpec(),
    )


class _GrpcStream:
    """Adapts a server-streaming ``ExecSandboxEvent`` call to ``ExecStream``."""

    def __init__(self, call: Any, inputs: queue.Queue | None = None) -> None:
        self._call = call
        self._inputs = inputs

    def __iter__(self) -> Iterator[tuple[str, Any]]:
        for event in self._call:
            payload = event.WhichOneof("payload")
            if payload == "stdout":
                yield "stdout", bytes(event.stdout.data)
            elif payload == "stderr":
                yield "stderr", bytes(event.stderr.data)
            elif payload == "exit":
                yield "exit", int(event.exit.exit_code)

    def cancel(self) -> None:
        self._call.cancel()
        if self._inputs is not None:
            self._inputs.put(None)

    def write(self, data: bytes) -> None:
        if self._inputs is not None:
            self._inputs.put(data)

    def close_input(self) -> None:
        if self._inputs is not None:
            self._inputs.put(None)


class SDKControlPlane:
    """``OpenShellControlPlane`` over the ``openshell`` Python SDK and the CLI's registered gateway."""

    def __init__(self, *, gateway: str | None, workspace: str) -> None:
        try:
            from openshell import SandboxClient
        except ImportError as exc:
            raise SandboxInitError(
                "The OpenShell SDK is not installed in this raven's environment. "
                "Install the sandbox extra, then register a gateway with the openshell CLI."
            ) from exc
        self._client = SandboxClient.from_active_cluster(cluster=gateway)
        self._workspace = workspace

    def create(self, *, name, image, policy, driver_config, resources, labels) -> None:
        spec = build_sandbox_spec(image=image, policy=policy, driver_config=driver_config, resources=resources)
        self._client.create(workspace=self._workspace, spec=spec, name=name, labels=labels)

    def wait_ready(self, name: str, timeout_seconds: float) -> None:
        try:
            self._client.wait_ready(name, workspace=self._workspace, timeout_seconds=timeout_seconds)
        except Exception as exc:
            reasons = self._failed_conditions(name)
            raise RuntimeError(f"{exc}: {reasons}" if reasons else str(exc)) from exc

    def _failed_conditions(self, name: str) -> str:
        """The gateway's own account of why a sandbox is not ready; the SDK error omits it."""
        from openshell._proto import datamodel_pb2, openshell_pb2

        try:
            response = self._client._stub.GetSandbox(
                openshell_pb2.GetSandboxRequest(
                    workspace_scope=datamodel_pb2.WorkspaceSelector(workspace=self._workspace), name=name
                ),
                timeout=10,
            )
        except Exception:
            return ""
        return "; ".join(
            f"{c.reason}: {c.message}" for c in response.sandbox.status.conditions if c.status == "False" and c.message
        )

    def _request(self, name: str, argv: list[str], workdir: str, env: dict[str, str], timeout_seconds: int | None):
        from openshell._proto import datamodel_pb2, openshell_pb2

        request = openshell_pb2.ExecSandboxRequest(
            workspace_scope=datamodel_pb2.WorkspaceSelector(workspace=self._workspace),
            sandbox=name,
            command=list(argv),
            workdir=workdir,
            environment=dict(env),
            no_login_shell=True,
        )
        if timeout_seconds:
            request.execution_timeout.seconds = int(timeout_seconds)
        return request

    def exec(self, name, argv, *, workdir, env, timeout_seconds) -> ExecStream:
        request = self._request(name, argv, workdir, env, timeout_seconds)
        deadline = timeout_seconds + _TIMEOUT_GRACE_SECONDS if timeout_seconds else None
        # The SDK's own exec wrapper hides the call object; the raw stub keeps
        # ``cancel()``, which is what stops a timed-out or abandoned command.
        # The wrapper also resolves the replica that owns the sandbox and
        # retries an unrouted request there; the raw stub addresses the replica
        # this client is bound to, so the gateway runs as a single replica.
        return _GrpcStream(self._client._stub.ExecSandbox(request, timeout=deadline))

    def interactive(self, name, argv, *, workdir, env) -> InteractiveStream:
        from openshell._proto import openshell_pb2

        inputs: queue.Queue = queue.Queue()
        start = openshell_pb2.ExecSandboxInput(start=self._request(name, argv, workdir, env, None))

        def requests() -> Iterator[Any]:
            yield start
            while (data := inputs.get()) is not None:
                yield openshell_pb2.ExecSandboxInput(stdin=data)

        # The same single replica applies here: the SDK's wrapper would route
        # the session to the replica that owns the sandbox.
        return _GrpcStream(self._client._stub.ExecSandboxInteractive(requests()), inputs)

    def delete(self, name: str) -> None:
        self._client.delete(name, workspace=self._workspace, allow_missing=True)

    def list_labeled(self, selector: str) -> list[tuple[str, dict[str, str]]]:
        refs = self._client.list_all(workspace=self._workspace, label_selector=selector)
        return [(ref.name, dict(ref.labels)) for ref in refs]

    def merge_rules(self, name: str, add: dict[str, dict], remove: list[str]) -> int:
        """Remove, then add, named network rules in one policy update; returns the new policy version."""
        from google.protobuf.json_format import ParseDict
        from openshell._proto import datamodel_pb2, openshell_pb2, sandbox_pb2

        # Removals first, so a rule both removed and added is replaced, not dropped.
        operations = [
            openshell_pb2.PolicyMergeOperation(remove_rule=openshell_pb2.RemoveNetworkRule(rule_name=rule_name))
            for rule_name in remove
        ]
        operations += [
            openshell_pb2.PolicyMergeOperation(
                add_rule=openshell_pb2.AddNetworkRule(
                    rule_name=rule_name, rule=ParseDict(rule, sandbox_pb2.NetworkPolicyRule())
                )
            )
            for rule_name, rule in add.items()
        ]
        response = self._client._stub.UpdateConfig(
            openshell_pb2.UpdateConfigRequest(
                workspace_scope=datamodel_pb2.WorkspaceSelector(workspace=self._workspace),
                sandbox=name,
                merge_operations=operations,
            ),
            timeout=_POLICY_WAIT_SECONDS,
        )
        return int(response.version)

    def wait_policy(self, name: str, version: int, timeout: float) -> bool:
        """Whether the sandbox has loaded policy ``version`` within ``timeout``; False at once when it failed to."""
        from openshell._proto import datamodel_pb2, openshell_pb2

        deadline = time.monotonic() + timeout
        while True:
            status = self._client._stub.GetSandboxPolicyStatus(
                openshell_pb2.GetSandboxPolicyStatusRequest(
                    workspace_scope=datamodel_pb2.WorkspaceSelector(workspace=self._workspace),
                    sandbox=name,
                    version=version,
                ),
                timeout=_DENIAL_READ_TIMEOUT,
            )
            if status.active_version >= version:
                return True
            if status.revision.status == openshell_pb2.POLICY_STATUS_FAILED or time.monotonic() >= deadline:
                return False
            time.sleep(_POLICY_POLL_SECONDS)

    def ocsf_lines(self, name: str, since: float) -> list[tuple[float, str]]:
        """The sandbox's OCSF log lines since ``since`` (epoch seconds), as event time and message."""
        from google.protobuf.timestamp_pb2 import Timestamp
        from openshell._proto import datamodel_pb2, openshell_pb2

        response = self._client._stub.GetSandboxLogs(
            openshell_pb2.GetSandboxLogsRequest(
                workspace_scope=datamodel_pb2.WorkspaceSelector(workspace=self._workspace),
                sandbox=name,
                since_time=Timestamp(seconds=int(since), nanos=int((since - int(since)) * 1_000_000_000)),
                lines=500,
            ),
            timeout=_DENIAL_READ_TIMEOUT,
        )
        return [
            (line.event_time.ToMicroseconds() / 1_000_000, line.message)
            for line in response.logs
            if line.target == "ocsf"
        ]
