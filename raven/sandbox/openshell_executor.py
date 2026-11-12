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
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from raven.sandbox.interfaces import ExecResult, SandboxExecutor, SandboxInitError
from raven.sandbox.mounts import WORKSPACE_MOUNT, MountTable

logger = logging.getLogger(__name__)

_READ_ONLY_BASELINE = ("/bin", "/usr", "/lib", "/proc", "/dev/urandom", "/etc", "/var/log")
_READ_WRITE_BASELINE = ("/tmp", "/dev/null")
_TIMEOUT_GRACE_SECONDS = 10.0
_SERVER_TIMEOUT_EXIT = 124
_OWNER_LABEL = "raven.owner"
_PID_LABEL = "raven.pid"


# OpenShell has no allow-any-host selector (NVIDIA/OpenShell#3856): "*", "**"
# and two-label wildcards are rejected, hostless allowed_ips endpoints do not
# match names under the Docker driver's policy DNS, and an "a*.*"-style
# pattern passes the gateway but stops the sandbox from activating. "Open"
# is therefore every host of three or more labels on every port, plus the
# two-label names developer tooling reaches directly. One all-ports endpoint
# already takes most of the gateway's 256 KiB policy budget, so the apex names
# get the common ports only.
_OPEN_WILDCARD = "**.*.*"
_ALL_PORTS = list(range(1, 65536))
_COMMON_PORTS = [22, 80, 443, 8080, 8443, 9418]
_APEX_DOMAINS = (
    "pypi.org",
    "python.org",
    "anaconda.org",
    "npmjs.org",
    "npmjs.com",
    "yarnpkg.com",
    "nodejs.org",
    "jsdelivr.net",
    "unpkg.com",
    "github.com",
    "githubusercontent.com",
    "gitlab.com",
    "bitbucket.org",
    "gitee.com",
    "crates.io",
    "rustup.rs",
    "rust-lang.org",
    "golang.org",
    "go.dev",
    "docker.io",
    "docker.com",
    "quay.io",
    "ghcr.io",
    "gcr.io",
    "maven.org",
    "gradle.org",
    "rubygems.org",
    "nuget.org",
    "ubuntu.com",
    "debian.org",
    "alpinelinux.org",
    "huggingface.co",
    "openai.com",
    "anthropic.com",
    "openrouter.ai",
    "deepseek.com",
)
_OPEN_ENTRY = "*"


def _endpoint(host: str, ports: list[int]) -> dict:
    # tls: skip relays the connection without terminating TLS; these rules
    # carry no request inspection, and inspected egress measured an order of
    # magnitude slower.
    return {"host": host, "ports": ports, "tls": "NETWORK_TLS_MODE_SKIP"}


def _open_rule(extra_hosts: list[str]) -> dict:
    hosts = list(dict.fromkeys([*_APEX_DOMAINS, *extra_hosts]))
    endpoints = [_endpoint(_OPEN_WILDCARD, _ALL_PORTS), *(_endpoint(host, _COMMON_PORTS) for host in hosts)]
    return {"name": "allow_open", "endpoints": endpoints, "binaries": [{"path": "/**"}]}


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


def sandbox_policy(
    *, allow_net: bool | list[str], mounts: list[tuple[str, str, bool]], uid: int, gid: int | None = None
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
    if uid > 0:
        # OpenShell refuses a root workload, so a root host keeps the image's
        # user; a non-root host runs as itself so bind-mounted files stay writable.
        policy["process"] = {"run_as_user": str(uid), "run_as_group": str(gid if gid else uid)}
    if allow_net is True or (isinstance(allow_net, list) and _OPEN_ENTRY in allow_net):
        extra = [] if allow_net is True else [host for host in allow_net if host != _OPEN_ENTRY]
        policy["network_policies"] = {"allow_open": _open_rule(extra)}
    elif allow_net:
        policy["network_policies"] = {
            f"allow_{index}": {
                "name": f"allow_{index}",
                "endpoints": [_endpoint(host, [80, 443])],
                "binaries": [{"path": "/**"}],
            }
            for index, host in enumerate(allow_net)
        }
    return policy


def _owner_digest() -> str:
    return hashlib.sha256(f"{socket.gethostname()}:{os.getuid()}".encode()).hexdigest()[:14]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    ) -> None:
        self._image = image
        self._mounts = MountTable(Path(workspace), extra_volumes or [])
        self._cpus = cpus
        self._memory_mib = memory_mib
        self._allow_net = allow_net
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
            await asyncio.to_thread(
                plane.create,
                name=name,
                image=self._image,
                policy=sandbox_policy(
                    allow_net=self._allow_net, mounts=self._mounts.mounts(), uid=os.getuid(), gid=os.getgid()
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
            raise SandboxInitError(
                f"The sandbox user (uid {uid}) cannot write the workspace {self._mounts.workspace}.\n"
                "  - Run raven as a non-root user that owns the workspace, or\n"
                f"  - make the workspace writable by uid {uid}"
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
        _reaped = True
        try:
            found = await asyncio.to_thread(plane.list_labeled, f"{_OWNER_LABEL}={_owner_digest()}")
        except Exception as exc:
            logger.warning("Could not list OpenShell sandboxes for cleanup: %s", exc)
            return
        for name, labels in found:
            pid = labels.get(_PID_LABEL, "")
            if pid.isdigit() and int(pid) != os.getpid() and not _pid_alive(int(pid)):
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
        effective = self._default_timeout if timeout is None else timeout
        stream = await asyncio.to_thread(
            plane.exec,
            name,
            ["sh", "-c", command],
            workdir=self._mounts.translate_cwd(cwd),
            env=dict(env or {}),
            timeout_seconds=effective or None,
        )
        drain = asyncio.ensure_future(asyncio.to_thread(_drain, stream))
        started = asyncio.get_running_loop().time()
        try:
            # timeout=0 times out at once, as it does for the direct executor.
            deadline = effective + _TIMEOUT_GRACE_SECONDS if effective > 0 else 0
            result = await asyncio.wait_for(asyncio.shield(drain), deadline)
        except asyncio.TimeoutError:
            stream.cancel()
            return ExecResult(stdout="", stderr=f"Command timed out after {effective}s", exit_code=-1)
        except asyncio.CancelledError:
            stream.cancel()
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
        return _GrpcStream(self._client._stub.ExecSandbox(request, timeout=deadline))

    def interactive(self, name, argv, *, workdir, env) -> InteractiveStream:
        from openshell._proto import openshell_pb2

        inputs: queue.Queue = queue.Queue()
        start = openshell_pb2.ExecSandboxInput(start=self._request(name, argv, workdir, env, None))

        def requests() -> Iterator[Any]:
            yield start
            while (data := inputs.get()) is not None:
                yield openshell_pb2.ExecSandboxInput(stdin=data)

        return _GrpcStream(self._client._stub.ExecSandboxInteractive(requests()), inputs)

    def delete(self, name: str) -> None:
        self._client.delete(name, workspace=self._workspace, allow_missing=True)

    def list_labeled(self, selector: str) -> list[tuple[str, dict[str, str]]]:
        refs = self._client.list_all(workspace=self._workspace, label_selector=selector)
        return [(ref.name, dict(ref.labels)) for ref in refs]
