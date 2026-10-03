"""Verify immutable native DAG snapshots and coordinate their durable authority."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from raven.contracts.mailbox import MailboxError
from raven.mailbox.codec import canonical_bytes

_OUTPUT_LIMIT = 16 * 1024 * 1024
_EXECUTOR_IDENTITY = None


def _start_ticks(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


def executor_identity():
    """Identify the actual Linux host process once, including its incarnation."""
    from raven.mailbox.dag import ExecutorIdentity

    global _EXECUTOR_IDENTITY
    if _EXECUTOR_IDENTITY is None or _EXECUTOR_IDENTITY.pid != os.getpid():
        _EXECUTOR_IDENTITY = ExecutorIdentity(
            str(uuid4()),
            os.getpid(),
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            _start_ticks(os.getpid()),
        )
    return _EXECUTOR_IDENTITY


def observe_process(identity):
    from raven.mailbox.dag import OwnerObservation

    try:
        current_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        fields = Path(f"/proc/{identity.pid}/stat").read_text().rsplit(")", 1)[1].split()
        state = (
            "live"
            if fields[0] not in ("Z", "X")
            and current_boot == identity.linux_boot_id
            and int(fields[19]) == identity.process_start_ticks
            else "dead"
        )
    except FileNotFoundError:
        state = "dead"
    except (OSError, ValueError, IndexError):
        state = "unknown"
    return OwnerObservation(
        identity.executor_id, identity.pid, identity.linux_boot_id, identity.process_start_ticks, state
    )


def publish_file(store, source: Path, name: str) -> dict:
    """Publish bounded regular evidence through the existing mailbox blob lock."""
    from raven.mailbox import blobs

    with blobs._open_regular(source, "unsafe_artifact_path") as content:
        size = os.fstat(content.fileno()).st_size
        if size > _OUTPUT_LIMIT:
            raise MailboxError("artifact_limit")
        digest = hashlib.file_digest(content, "sha256").hexdigest()
    descriptor = {"sha256": digest, "size": size, "media_type": "application/octet-stream", "name": name}
    with blobs.locked(store.db):
        blobs.import_artifacts(store.db, SimpleNamespace(artifacts=[SimpleNamespace(**descriptor)]), {digest: source})
    return descriptor


def _blob_bytes(store, digest: str, size: int | None = None) -> bytes:
    from raven.mailbox import blobs

    try:
        with blobs._open_regular(store.root / "blobs" / "sha256" / digest, "storage_conflict") as source:
            content = source.read(_OUTPUT_LIMIT + 1)
        if (
            len(content) > _OUTPUT_LIMIT
            or size is not None
            and len(content) != size
            or hashlib.sha256(content).hexdigest() != digest
        ):
            raise MailboxError("storage_conflict")
        return content
    except OSError:
        raise MailboxError("storage_conflict") from None


def materialize_snapshot(store, manifest, destination: Path):
    """Verify a pinned baseline archive and overlay its declared content blobs."""
    destination.mkdir(parents=True, exist_ok=False)
    if destination.is_symlink():
        raise MailboxError("unsafe_snapshot")
    archive = _blob_bytes(store, manifest.baseline_sha256)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
            seen = set()
            for member in source:
                relative = _relative_path(member.name)
                if not member.isfile() or member.name in seen or member.size > _OUTPUT_LIMIT:
                    raise MailboxError("unsafe_snapshot")
                seen.add(member.name)
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.extractfile(member).read())
    except (tarfile.TarError, OSError):
        raise MailboxError("unsafe_snapshot") from None
    expected = {entry.path: entry for entry in manifest.entries}
    for archived in seen - set(expected):
        (destination / _relative_path(archived)).unlink()
    if len(expected) != len(manifest.entries):
        raise MailboxError("unsafe_snapshot")
    for relative, entry in expected.items():
        target = destination / _relative_path(relative)
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == entry.sha256:
            if target.stat().st_size != entry.size:
                raise MailboxError("storage_conflict")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_blob_bytes(store, entry.sha256, entry.size))
    verifier = SnapshotVerifier(
        destination, revision_commit=manifest.revision_commit, revision_tree=manifest.revision_tree
    )
    if {entry[0] for entry in verifier.entries()} != set(expected):
        raise MailboxError("storage_conflict")
    if manifest.fingerprint and verifier.fingerprint() != manifest.fingerprint:
        raise MailboxError("storage_conflict")
    return verifier


def _relative_path(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise MailboxError("unsafe_snapshot")
    return path


def _git(repository: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            [shutil.which("git") or "/usr/bin/git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise MailboxError("invalid_revision") from None


def export_baseline(repository: Path, revision: str, paths: tuple[str, ...]):
    """Export regular-file content from one trusted pinned Git commit."""
    from raven.mailbox.dag import ArtifactEntry, SnapshotManifest

    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision) and not revision.startswith("refs/"):
        raise MailboxError("invalid_revision")
    selected = [_relative_path(value).as_posix().rstrip("/") for value in paths]
    commit = _git(repository, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()
    tree = _git(repository, "rev-parse", "--verify", "--end-of-options", commit + "^{tree}").decode().strip()
    entries = []
    archive = io.BytesIO()
    matched = set()
    with tarfile.open(fileobj=archive, mode="w") as target:
        for record in _git(repository, "ls-tree", "-r", "-z", commit).split(b"\0"):
            if not record:
                continue
            info, name = record.split(b"\t", 1)
            path = name.decode("utf-8")
            matching = {
                prefix for prefix in selected if prefix == "." or path == prefix or path.startswith(prefix + "/")
            }
            if not matching:
                continue
            matched.update(matching)
            mode, kind, object_id = info.decode().split()
            if mode not in {"100644", "100755"} or kind != "blob":
                raise MailboxError("unsafe_snapshot")
            _relative_path(path)
            size = int(_git(repository, "cat-file", "-s", object_id))
            if size > _OUTPUT_LIMIT:
                raise MailboxError("artifact_limit")
            content = _git(repository, "cat-file", "blob", object_id)
            entry = ArtifactEntry(path, hashlib.sha256(content).hexdigest(), len(content))
            entries.append(entry)
            header = tarfile.TarInfo(path)
            header.size = len(content)
            header.mode = 0o700 if mode == "100755" else 0o600
            target.addfile(header, io.BytesIO(content))
            if archive.tell() > _OUTPUT_LIMIT:
                raise MailboxError("artifact_limit")
    if set(selected) != matched:
        raise MailboxError("missing_check_input")
    data = archive.getvalue()
    if len(data) > _OUTPUT_LIMIT:
        raise MailboxError("artifact_limit")
    fingerprint = _manifest_fingerprint(commit, tree, entries)
    return SnapshotManifest(commit, tree, hashlib.sha256(data).hexdigest(), tuple(entries), fingerprint), data


def _manifest_fingerprint(commit, tree, entries):
    return hashlib.sha256(
        canonical_bytes(
            {
                "revision_commit": commit,
                "revision_tree": tree,
                "entries": [
                    [entry.path, entry.size, entry.sha256] for entry in sorted(entries, key=lambda item: item.path)
                ],
            }
        )
    ).hexdigest()


class SnapshotVerifier:
    """Execute saved argv against a complete, fingerprinted regular-file tree."""

    def __init__(self, root: Path, *, revision_commit: str, revision_tree: str):
        self.root = Path(root)
        self.revision_commit = revision_commit
        self.revision_tree = revision_tree
        self._scratch = None
        self.stdout_path = None
        self.stderr_path = None

    def entries(self):
        if self.root.is_symlink() or not self.root.is_dir():
            raise MailboxError("unsafe_snapshot")
        entries = []
        for folder, directories, files in os.walk(self.root, followlinks=False):
            for name in directories:
                if (Path(folder) / name).is_symlink():
                    raise MailboxError("unsafe_snapshot")
            for name in files:
                path = Path(folder) / name
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as source:
                        info = os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode):
                            raise MailboxError("unsafe_snapshot")
                        digest = hashlib.file_digest(source, "sha256").hexdigest()
                except OSError:
                    raise MailboxError("unsafe_snapshot") from None
                entries.append((path.relative_to(self.root).as_posix(), info.st_size, digest))
        return sorted(entries)

    def fingerprint(self) -> str:
        return hashlib.sha256(
            canonical_bytes(
                {
                    "revision_commit": self.revision_commit,
                    "revision_tree": self.revision_tree,
                    "entries": [list(entry) for entry in self.entries()],
                }
            )
        ).hexdigest()

    async def check(
        self,
        argv: tuple[str, ...],
        *,
        timeout: int,
        cwd: str = ".",
        executable_sha256: str | None = None,
        deadline_ms: int | None = None,
        on_started=None,
    ):
        from raven.mailbox.dag import CheckObservation

        location = self.root / cwd
        if Path(cwd).is_absolute() or not location.resolve().is_relative_to(self.root.resolve()):
            raise MailboxError("unsafe_snapshot")
        if any((self.root / Path(*Path(cwd).parts[:n])).is_symlink() for n in range(1, len(Path(cwd).parts) + 1)):
            raise MailboxError("unsafe_snapshot")
        if not argv or not Path(argv[0]).is_absolute() or type(timeout) is not int or timeout < 1:
            raise MailboxError("invalid_check")
        before = self.fingerprint()
        started = int(time.time() * 1000)
        deadline = deadline_ms if deadline_ms is not None else started + timeout * 1000
        self._scratch = tempfile.TemporaryDirectory(prefix="raven-dag-check-")
        self.stdout_path = Path(self._scratch.name) / "stdout"
        self.stderr_path = Path(self._scratch.name) / "stderr"
        process = None
        exit_code = None
        error = None
        streams = []

        async def drain(stream, path):
            size = 0
            with path.open("wb") as output:
                while chunk := await stream.read(65536):
                    size += len(chunk)
                    if size > _OUTPUT_LIMIT:
                        raise MailboxError("verification_output_limit")
                    output.write(chunk)

        async def stop():
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for task in streams:
                    task.cancel()
                await asyncio.gather(*streams, return_exceptions=True)
                await process.communicate()
                await process.wait()

        try:
            if (
                executable_sha256 is not None
                and hashlib.sha256(Path(argv[0]).read_bytes()).hexdigest() != executable_sha256
            ):
                raise MailboxError("verification_executable_changed")
            remaining = (deadline - int(time.time() * 1000)) / 1000
            if remaining <= 0:
                raise TimeoutError
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=location,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": self._scratch.name},
            )
            if on_started is not None:
                on_started(process.pid)
            remaining = (deadline - int(time.time() * 1000)) / 1000
            if remaining <= 0:
                raise TimeoutError
            streams = [
                asyncio.create_task(drain(process.stdout, self.stdout_path)),
                asyncio.create_task(drain(process.stderr, self.stderr_path)),
            ]
            async with asyncio.timeout(remaining):
                await asyncio.gather(*streams)
                exit_code = await process.wait()
            await stop()
            if (
                executable_sha256 is not None
                and hashlib.sha256(Path(argv[0]).read_bytes()).hexdigest() != executable_sha256
            ):
                raise MailboxError("verification_executable_changed")
        except TimeoutError:
            error = "timeout"
            await stop()
        except asyncio.CancelledError:
            await stop()
            raise
        except (MailboxError, OSError):
            error = "exception"
            exit_code = None
            await stop()
        after = self.fingerprint()
        for path in (self.stdout_path, self.stderr_path):
            if not path.exists():
                path.touch()
        return CheckObservation(
            exit_code=exit_code,
            error=error,
            before_fingerprint=before,
            after_fingerprint=after,
            stdout_sha256=hashlib.sha256(self.stdout_path.read_bytes()).hexdigest(),
            stderr_sha256=hashlib.sha256(self.stderr_path.read_bytes()).hexdigest(),
            started_at_ms=started,
            ended_at_ms=int(time.time() * 1000),
        )


class StrictDagRuntime:
    """Bind host-authorized ledger transitions to the existing native DAG runner."""

    def __init__(self, ledger, dag_tool, *, executor_identity, repository_for, executable_for):
        self.ledger = ledger
        self.tool = dag_tool
        self.executor = executor_identity
        self.repository_for = repository_for
        self.executable_for = executable_for
        self.tool._strict_runtime = self

    def _root(self, binding, root_id, *, recover=False):
        root = self.ledger.status(root_id, scope=binding.scope).root
        same_owner = root.task_owner_ref == binding.ref or (
            recover and root.task_owner_ref.agent_id == binding.ref.agent_id
        )
        if root.session_key != binding.session_key or not same_owner:
            raise MailboxError("scope_denied")
        return root

    def _plan(self, repo, selector, policies):
        from dataclasses import replace

        from raven.mailbox.dag import CheckSpec, NodePolicy, VerificationPlan

        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|refs/[A-Za-z0-9_./-]+", selector["ref"]):
            raise MailboxError("invalid_revision")
        if selector["repo_id"] != "workspace":
            raise MailboxError("scope_denied")
        nodes = []
        selected = set()
        commit = _git(repo, "rev-parse", "--verify", f"{selector['ref']}^{{commit}}").decode().strip()
        tree_paths = {
            row.split(b"\t", 1)[1].decode() for row in _git(repo, "ls-tree", "-r", "-z", commit).split(b"\0") if row
        }
        for data in policies:
            for path in (*data.get("output_paths", ()), *data.get("protected_paths", ())):
                if path == ".raven-result.md" or path == ".raven-inputs" or path.startswith(".raven-inputs/"):
                    raise MailboxError("reserved_path")
                _relative_path(path)
            outputs = tuple(data.get("output_paths", ()))
            protected = tuple(data.get("protected_paths", ()))
            if any(Path(o).is_relative_to(p) or Path(p).is_relative_to(o) for o in outputs for p in protected):
                raise MailboxError("protected_output_conflict")
            selected.update(protected)
            selected.update(
                path for path in outputs if any(f == path or f.startswith(path.rstrip("/") + "/") for f in tree_paths)
            )
            checks = []
            for check in data.get("checks", ()):
                argv = check["argv"]
                executable = Path(self.executable_for(argv[0])).resolve()
                if not executable.is_file():
                    raise MailboxError("verification_executable_unavailable")
                cwd = check.get("cwd", ".")
                _relative_path(cwd)
                checks.append(
                    CheckSpec(
                        check["check_id"],
                        (str(executable), *argv[1:]),
                        hashlib.sha256(executable.read_bytes()).hexdigest(),
                        cwd,
                        check["timeout_seconds"],
                        tuple(check.get("repairable_exit_codes", ())),
                        tuple(check.get("nonrepairable_exit_codes", ())),
                    )
                )
            nodes.append(
                NodePolicy(
                    data["logical_node_id"],
                    tuple(data.get("depends_on", ())),
                    outputs,
                    protected,
                    tuple(checks),
                    data.get("subjective_review", False),
                )
            )
        protected_paths = tuple(path for node in nodes for path in node.protected_paths)
        if any(
            Path(output).is_relative_to(protected) or Path(protected).is_relative_to(output)
            for node in nodes
            for output in node.output_paths
            for protected in protected_paths
        ):
            raise MailboxError("protected_output_conflict")
        baseline, archive = export_baseline(repo, commit, tuple(sorted(selected)))
        with tempfile.TemporaryDirectory(prefix="raven-dag-baseline-") as scratch:
            source = Path(scratch) / "baseline.tar"
            source.write_bytes(archive)
            descriptor = publish_file(self.ledger.store, source, "baseline.tar")
        baseline = replace(baseline, baseline_sha256=descriptor["sha256"])
        from raven.mailbox.dag import _validate_plan

        plan = VerificationPlan(baseline.revision_commit, baseline.revision_tree, baseline, tuple(nodes))
        _validate_plan(plan)
        return plan

    def _validate_graph(self, spec, node_policies):
        policies = {p["logical_node_id"]: p for p in node_policies}
        if set(policies) != {n.id for n in spec.nodes} or len(policies) != len(node_policies):
            raise MailboxError("invalid_verification_plan")
        for node in spec.nodes:
            if node.instance is not None:
                raise MailboxError("strict_shared_instance_unsupported")
            if tuple(node.depends_on) != tuple(policies[node.id].get("depends_on", ())):
                raise MailboxError("invalid_verification_plan")
            native = self.tool._resolve_node(node)
            from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

            if not isinstance(native, RavenLoopBackend):
                raise MailboxError("strict_backend_unsupported")

    async def create(
        self,
        binding,
        *,
        request_id,
        history_root,
        session_key,
        graph,
        revision_selector,
        node_policies,
        max_auto_repairs=3,
    ):
        from raven.agent.subagent.dag_graph import SubAgentDagSpec, validate_and_order
        from raven.agent.subagent.history import session_history_root
        from raven.mailbox.db import canonical_id

        request_id = canonical_id(request_id)
        if binding.session_key != session_key:
            raise MailboxError("scope_denied")
        wire_inputs = {
            "scope": binding.scope,
            "history_root": history_root,
            "session_key": session_key,
            "graph": graph,
            "revision_selector": revision_selector,
            "node_policies": node_policies,
            "max_auto_repairs": max_auto_repairs,
        }
        replay = self.ledger.replay_create(
            task_owner_ref=binding.ref, scope=binding.scope, request_id=request_id, wire_inputs=wire_inputs
        )
        if replay is not None:
            return replay
        with self.ledger.db.connection() as conn:
            self.ledger._authority(conn, binding.ref, binding.scope)
        repo = Path(self.repository_for(revision_selector["repo_id"], session_key)).resolve()
        spec = SubAgentDagSpec.model_validate(graph)
        validate_and_order(spec, (str(repo),))
        self._validate_graph(spec, node_policies)
        plan = self._plan(repo, revision_selector, node_policies)
        canonical = Path(session_history_root(self.tool._session_dir_for(session_key))) / "strict" / request_id
        if history_root is not None and Path(history_root).resolve() != canonical.resolve():
            raise MailboxError("scope_denied")
        self.ledger.db.upgrade_strict()
        return self.ledger.create_root(
            task_owner_ref=binding.ref,
            scope=binding.scope,
            history_root=str(canonical),
            session_key=session_key,
            graph=spec.model_dump(),
            verification_plan=plan,
            request_id=request_id,
            max_auto_repairs=max_auto_repairs,
            wire_inputs=wire_inputs,
        )

    async def start(self, binding, *, root_id, request_id, expected_owner_epoch):
        root = self._root(binding, root_id, recover=True)
        wire_inputs = {"operation": "start", "root_id": root_id, "expected_owner_epoch": expected_owner_epoch}
        replay = self.ledger.replay_owner(
            root_id,
            task_owner_ref=binding.ref,
            executor=self.executor,
            request_id=request_id,
            wire_inputs=wire_inputs,
        )
        if replay is not None:
            return self.ledger.recovery_view(replay)
        if root.task_owner_ref != binding.ref and (root.executor is not None or root.owner_epoch != 0):
            raise MailboxError("scope_denied")
        fence = self.ledger.acquire_owner(
            root_id,
            task_owner_ref=binding.ref,
            executor=self.executor,
            expected_owner_epoch=expected_owner_epoch,
            request_id=request_id,
            adopt_current_ref=True,
            wire_inputs=wire_inputs,
        )
        root = self.ledger.recovery_view(fence).root
        await self._dispatch(root, fence)
        return self.ledger.recovery_view(fence)

    async def recover(
        self, binding, *, root_id, request_id, expected_owner_epoch, expected_executor_id, replace_owner=False
    ):
        root = self._root(binding, root_id, recover=True)
        wire_inputs = {
            "root_id": root_id,
            "expected_owner_epoch": expected_owner_epoch,
            "expected_executor_id": expected_executor_id,
            "replace_owner": replace_owner,
        }
        replay = self.ledger.replay_owner(
            root_id,
            task_owner_ref=binding.ref,
            executor=self.executor,
            request_id=request_id,
            wire_inputs=wire_inputs,
        )
        if replay is not None:
            return self.ledger.recovery_view(replay)
        if root.executor is None or root.executor.executor_id != expected_executor_id:
            raise MailboxError("owner_conflict")
        previous = observe_process(root.executor) if replace_owner else None
        fence = self.ledger.acquire_owner(
            root_id,
            task_owner_ref=binding.ref,
            executor=self.executor,
            expected_owner_epoch=expected_owner_epoch,
            request_id=request_id,
            replace=replace_owner,
            previous_owner=previous,
            adopt_current_ref=True,
            wire_inputs=wire_inputs,
        )
        root = self.ledger.recovery_view(fence).root
        view = self.ledger.recovery_view(fence)
        if root.run_id not in self.tool._runs:
            for attempt in view.attempts:
                if (
                    attempt.state in ("submitting", "started")
                    and attempt.staged_envelope_bytes is None
                    and attempt.execution_owner_epoch == fence.owner_epoch
                ):
                    self.ledger.record_unknown(
                        fence, attempt.attempt_id, reason="execution_unknown", request_id=str(uuid4())
                    )
        await self._dispatch(root, fence)
        return self.ledger.recovery_view(fence)

    async def resolve(self, binding, *, root_id, attempt_id, request_id, expected_owner_epoch, resolution):
        root = self._root(binding, root_id)
        from raven.mailbox.dag import RootFence

        if root.executor != self.executor:
            raise MailboxError("owner_conflict")
        fence = RootFence(root_id, binding.ref, self.executor.executor_id, expected_owner_epoch, root.assignment_epoch)
        wire_resolution = resolution
        replay = self.ledger.replay_resolve(
            fence,
            attempt_id,
            expected_owner_epoch=expected_owner_epoch,
            request_id=request_id,
            wire_resolution=wire_resolution,
        )
        if replay is not None:
            await self._dispatch(self.ledger.recovery_view(fence).root, fence)
            return replay
        if resolution.get("action") == "replan":
            from dataclasses import asdict

            from raven.agent.subagent.dag_graph import SubAgentDagSpec, validate_and_order

            if root.run_id in self.tool._runs:
                raise MailboxError("execution_not_settled")
            view = self.ledger.recovery_view(fence)
            for attempt in view.attempts:
                for check in attempt.check_runs:
                    if check.state in ("running", "submitting", "unknown") and (
                        check.process_identity is None or observe_process(check.process_identity).state != "dead"
                    ):
                        raise MailboxError("execution_not_settled")
            graph = SubAgentDagSpec.model_validate(resolution["successor_graph"])
            repo = Path(self.repository_for("workspace", root.session_key)).resolve()
            validate_and_order(graph, (str(repo),))
            candidate = resolution["successor_verification_plan"]
            self._validate_graph(graph, candidate["nodes"])
            plan = self._plan(repo, {"repo_id": "workspace", "ref": candidate["revision_commit"]}, candidate["nodes"])
            resolution = {
                **resolution,
                "successor_graph": graph.model_dump(),
                "successor_verification_plan": json.loads(json.dumps(asdict(plan))),
            }
        result = self.ledger.resolve(
            fence,
            attempt_id,
            expected_owner_epoch=expected_owner_epoch,
            resolution=resolution,
            request_id=request_id,
            wire_resolution=wire_resolution,
        )
        await self._dispatch(self.ledger.recovery_view(fence).root, fence)
        return result

    def root_for_session_run(self, run_id, session_key):

        from raven.mailbox.db import canonical_id

        try:
            canonical_id(run_id)
        except MailboxError:
            return None
        if not self.ledger.db.path.exists():
            return None
        with self.ledger.db.connection() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] != 3:
                return None
            row = conn.execute(
                "SELECT record_json FROM strict_roots WHERE root_id IN (SELECT root_id FROM strict_roots WHERE run_id=? UNION SELECT root_id FROM strict_attempts WHERE run_id=? UNION SELECT root_id FROM strict_roots WHERE EXISTS (SELECT 1 FROM json_each(history_json) WHERE json_extract(value,'$.run_id')=?))",
                (run_id, run_id, run_id),
            ).fetchone()
            if row is None:
                return None
            data = json.loads(row[0])
        if data["session_key"] != session_key:
            raise MailboxError("scope_denied")
        return self.ledger.root_for_run({"task_id": data["task_id"], "workspace_id": data["workspace_id"]}, run_id)

    def session_run_ids(self, session_key):

        if not self.ledger.db.path.exists():
            return set()
        with self.ledger.db.connection() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] != 3:
                return set()
            rows = conn.execute(
                "SELECT run_id,history_json FROM strict_roots WHERE json_extract(record_json,'$.session_key')=?",
                (session_key,),
            ).fetchall()
        return {run_id for row in rows for run_id in (row[0], *json.loads(row[1]))}

    def projection_details(self, run_id, session_key):
        from dataclasses import asdict

        root = self.root_for_session_run(run_id, session_key)
        if root is None:
            return None
        view = self.ledger.status(root.root_id, scope={"task_id": root.task_id, "workspace_id": root.workspace_id})
        attempts = []
        for attempt in view.attempts:
            if attempt.run_id == run_id:
                record = asdict(attempt)
                record.pop("staged_envelope_bytes", None)
                for check in record["check_runs"]:
                    check.pop("submit_now", None)
                attempts.append(record)
        return {
            "root_id": root.root_id,
            "owner_epoch": view.root.owner_epoch,
            "max_auto_repairs": root.max_auto_repairs,
            "attempts": attempts,
        }

    async def project_run(self, run_id, session_key):
        from raven.agent.subagent.dag_runner import _finalize
        from raven.agent.subagent.dag_store import DagRunStore, index_guard
        from raven.mailbox.dag import RootFence

        root = self.root_for_session_run(run_id, session_key)
        if root is None:
            return False
        fence = RootFence(
            root.root_id,
            root.task_owner_ref,
            root.executor.executor_id if root.executor else "",
            root.owner_epoch,
            root.assignment_epoch,
        )
        execution = _StrictExecution(self, root, fence, readonly=True)
        store = DagRunStore(
            self.tool._backend,
            self.tool._run_root(session_key),
            run_id,
            nodes_root=self.tool._nodes_root(session_key),
            registry_root=self.tool._history_root(session_key),
        )
        async with index_guard(store.registry_root):
            await store.init(execution.spec.model_dump_json(), [n.id for n in execution.spec.nodes])
        status = {n.id: "pending" for n in execution.spec.nodes}
        paths = {}
        await execution.hydrate(store, status, paths)
        by_id = {n.id: n for n in execution.spec.nodes}
        dependents = {n.id: [] for n in execution.spec.nodes}
        for node in execution.spec.nodes:
            for dep in node.depends_on:
                dependents[dep].append(node.id)
        await _finalize(
            execution.spec, by_id, status, {}, paths, set(), dependents, {}, {}, {}, store, session_key=session_key
        )
        return True

    async def _dispatch(self, root, fence):
        from raven.agent.subagent.dag_tool import _DagOrigin, _RunDirs
        from raven.agent.subagent.history import dag_root, nodes_root, session_history_root

        if root.run_id in self.tool._runs:
            return
        execution = _StrictExecution(self, root, fence)
        spec = execution.spec
        backends = {}
        from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

        for node in spec.nodes:
            native = self.tool._resolve_node(node)
            if not isinstance(native, RavenLoopBackend):
                raise MailboxError("strict_backend_unsupported")
            backends[node.id] = native
        session_dir = self.tool._session_dir_for(root.session_key)
        dirs = _RunDirs(
            str(self.repository_for("workspace", root.session_key)),
            str(dag_root(session_dir)),
            str(nodes_root(session_dir)),
            str(session_history_root(session_dir)),
        )
        origin = _DagOrigin(channel="strict", chat_id=root.task_id, conversation=root.session_key)
        await self.tool._dispatch(spec, root.run_id, dirs, backends, frozenset(), origin, None, True, strict=execution)


class _StrictExecution:
    """Execute one saved root within the existing runner's ready set and gate."""

    def __init__(self, runtime, root, fence, *, readonly=False):
        self.readonly = readonly
        from uuid import UUID, uuid5

        from raven.agent.subagent.dag_graph import SubAgentDagSpec

        self.runtime, self.ledger, self.root, self.fence = runtime, runtime.ledger, root, fence
        self.ids = {
            p.logical_node_id: "strict_" + uuid5(UUID(root.root_id), root.run_id + ":" + p.logical_node_id).hex
            for p in root.verification_plan.nodes
        }
        self.logical = {v: k for k, v in self.ids.items()}
        graph = copy.deepcopy(root.graph)
        for node in graph["nodes"]:
            original = node["id"]
            node["id"] = self.ids[original]
            node["depends_on"] = [self.ids[d] for d in node.get("depends_on", ())]
            node["instance"] = None
            node["mcps"] = []
            from raven.agent.subagent.prompt_placeholders import iter_placeholders

            template = node.get("prompt_template", "")
            for _, _, placeholder in list(iter_placeholders(template)):
                if placeholder.kind in ("output", "output_path"):
                    suffix = "output" if placeholder.kind == "output" else "output_path"
                    template = template.replace(
                        placeholder.raw, "{{ " + self.ids[placeholder.name] + "." + suffix + " }}"
                    )
            node["prompt_template"] = template
            node["inputs"] = {
                k: {"node": self.ids[v["node"]]} if isinstance(v, dict) and "node" in v else v
                for k, v in node.get("inputs", {}).items()
            }
        self.spec = SubAgentDagSpec.model_validate(graph)

    def view(self):
        if self.readonly:
            return self.ledger.status(
                self.root.root_id, scope={"task_id": self.root.task_id, "workspace_id": self.root.workspace_id}
            )
        return self.ledger.recovery_view(self.fence)

    async def hydrate(self, store, status, output_paths):
        for attempt in self.view().attempts:
            if attempt.run_id != self.root.run_id or attempt.logical_node_id not in self.ids:
                continue
            nid = self.ids[attempt.logical_node_id]
            status[nid] = (
                "completed"
                if attempt.state == "accepted"
                else "failed"
                if attempt.state == "failed"
                else "running"
                if self.readonly and attempt.state in ("submitting", "started")
                else "pending"
                if (
                    attempt.state in ("prepared", "review")
                    or attempt.staged_envelope_bytes is not None
                    and attempt.result is None
                )
                and not attempt.human_reason
                else "exception"
            )
            if attempt.state == "accepted":
                await self._project(attempt, store, nid, output_paths)

    async def _project(self, attempt, store, nid, output_paths):
        entry = next(e for e in attempt.snapshot.entries if e.path == ".raven-result.md")
        text = _blob_bytes(self.ledger.store, entry.sha256, entry.size).decode("utf-8")
        path = store.output_path(nid)
        await store.write_text(path, text)
        for archived, latest in (
            (store.attempt_prompt_path(nid, attempt.ordinal), store.prompt_path(nid)),
            (store.attempt_transcript_path(nid, attempt.ordinal), store.transcript_path(nid)),
        ):
            if await self.runtime.tool._backend.file_exists(archived):
                await store.write_text(latest, await store.read_text(archived))
        output_paths[nid] = path

    async def run_node(self, node, native, *, store, status, output_paths, errors, prompt_written):
        logical = self.logical[node.id]
        view = self.view()
        prior = [a for a in view.attempts if a.logical_node_id == logical and a.run_id == self.root.run_id]
        attempt = prior[-1] if prior else None
        policy = next(p for p in self.root.verification_plan.nodes if p.logical_node_id == logical)
        if attempt is None:
            intent, attempt = self.ledger.claim_node(
                self.fence, logical, run_id=self.root.run_id, request_id=str(uuid4())
            )
        else:
            intent = next((i for i in view.prepared_intents if i.attempt_id == attempt.attempt_id), None)
        try:
            if attempt.staged_envelope_bytes is not None and attempt.result is None:
                with self.ledger.db.connection() as conn:
                    intent = self.ledger._attempt_intent(conn, attempt.attempt_id)
                transition = await self._admit(intent, attempt, attempt.staged_envelope_bytes, policy)
            elif attempt.state == "review":
                transition = await self._verify(attempt, policy)
            elif attempt.state == "prepared" and intent is not None:
                intent = self.ledger.mark_submitting(self.fence, intent.dispatch_id, request_id=str(uuid4()))
                if not intent.submit_now:
                    status[node.id] = "exception"
                    return
                transition = await self._launch(node, native, store, prompt_written, intent, attempt, policy, prior)
            else:
                status[node.id] = "exception"
                return
            if transition.decision == "accepted":
                status[node.id] = "completed"
                await self._project(transition.attempt, store, node.id, output_paths)
            elif transition.decision == "repair_scheduled":
                status[node.id] = "pending"
            else:
                status[node.id] = "failed" if transition.decision == "failed" else "exception"
                errors[node.id] = transition.reason or transition.decision
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.ledger.record_unknown(
                self.fence, attempt.attempt_id, reason=type(exc).__name__, request_id=str(uuid4())
            )
            status[node.id] = "exception"
            errors[node.id] = type(exc).__name__
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _launch(self, node, native, store, prompt_written, intent, attempt, policy, prior):
        from raven.agent.subagent.dag_render import render_prompt
        from raven.mailbox.dag import ArtifactEntry, SnapshotManifest

        parent = Path(self.root.history_root) / "attempts" / attempt.attempt_id
        parent.mkdir(parents=True, exist_ok=False)
        worker = parent / "worker"
        baseline = self.root.verification_plan.baseline
        materialize_snapshot(self.ledger.store, baseline, worker)
        overlays = {}
        accepted = {
            a.logical_node_id: a for a in self.view().attempts if a.state == "accepted" and a.run_id == self.root.run_id
        }
        for dep in policy.depends_on:
            upstream = accepted[dep]
            dep_policy = next(p for p in self.root.verification_plan.nodes if p.logical_node_id == dep)
            for entry in upstream.snapshot.entries:
                if entry.path == ".raven-result.md":
                    continue
                if not any(
                    entry.path == p or entry.path.startswith(p.rstrip("/") + "/") for p in dep_policy.output_paths
                ):
                    continue
                if entry.path in overlays and overlays[entry.path] != entry:
                    raise MailboxError("input_conflict")
                if any(entry.path == p or entry.path.startswith(p.rstrip("/") + "/") for p in policy.protected_paths):
                    raise MailboxError("protected_output_conflict")
                overlays[entry.path] = entry
        for entry in overlays.values():
            path = worker / entry.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_blob_bytes(self.ledger.store, entry.sha256, entry.size))
        refs = worker / ".raven-inputs"
        refs.mkdir()
        derived_inputs = {}
        for dep in policy.depends_on:
            entry = next(e for e in accepted[dep].snapshot.entries if e.path == ".raven-result.md")
            path = f".raven-inputs/{self.ids[dep]}.out.md"
            (worker / path).write_bytes(_blob_bytes(self.ledger.store, entry.sha256, entry.size))
            derived_inputs[path] = (entry.size, entry.sha256)
        prompt = await render_prompt(
            node,
            backend=self.runtime.tool._backend,
            cwd=str(worker),
            nodes_root=str(refs),
            roots=(str(worker),),
            by_id={n.id: n for n in self.spec.nodes},
            include_memory=False,
        )
        if intent.successor_kind == "repair" and len(prior) > 1:
            previous = prior[-2]
            result = next(e for e in previous.snapshot.entries if e.path == ".raven-result.md")
            candidate = _blob_bytes(self.ledger.store, result.sha256, result.size).decode()
            feedback = [
                {"check_id": c.check_id, "exit_code": c.observation.exit_code, "error": c.observation.error}
                for c in previous.check_runs
                if c.observation
            ]
            for entry in previous.snapshot.entries:
                if any(entry.path == p or entry.path.startswith(p.rstrip("/") + "/") for p in policy.output_paths):
                    target = worker / entry.path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(_blob_bytes(self.ledger.store, entry.sha256, entry.size))
            prompt += "\n\nSaved candidate:\n" + candidate + "\n\nFixed check feedback:\n" + json.dumps(feedback)
        await store.write_text(store.prompt_path(node.id), prompt)
        await store.write_text(store.attempt_prompt_path(node.id, attempt.ordinal), prompt)
        prompt_written.add(node.id)
        self.ledger.record_started(self.fence, attempt.attempt_id, attempt.attempt_id)
        transcript = []
        output = await native.run(
            prompt,
            task_id=attempt.attempt_id,
            workspace=worker,
            executor=None,
            allowed_dirs=(worker,),
            tools_allow=("read_file", "write_file", "edit_file", "list_dir"),
            history=None,
            on_messages=transcript.append,
            mcps=[],
        )
        if not isinstance(output, str) or len(output.encode()) > _OUTPUT_LIMIT:
            raise MailboxError("artifact_limit")
        (worker / ".raven-result.md").write_text(output)
        await store.write_text(store.attempt_output_path(node.id, attempt.ordinal), output)
        if transcript:
            text = "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in transcript[-1])
            await store.write_text(store.attempt_transcript_path(node.id, attempt.ordinal), text)
        verifier = SnapshotVerifier(
            worker, revision_commit=baseline.revision_commit, revision_tree=baseline.revision_tree
        )
        all_entries = verifier.entries()
        original = {e.path: e for e in baseline.entries}
        protected = tuple(p for node_policy in self.root.verification_plan.nodes for p in node_policy.protected_paths)
        observed = {path: (size, digest) for path, size, digest in all_entries}
        actual_inputs = {path: value for path, value in observed.items() if path.startswith(".raven-inputs/")}
        if actual_inputs != derived_inputs:
            raise MailboxError("accepted_input_changed")
        expected_protected = {
            path: (entry.size, entry.sha256)
            for path, entry in original.items()
            if any(path == p or path.startswith(p.rstrip("/") + "/") for p in protected)
        }
        actual_protected = {
            path: value
            for path, value in observed.items()
            if any(path == p or path.startswith(p.rstrip("/") + "/") for p in protected)
        }
        if actual_protected != expected_protected:
            raise MailboxError("protected_input_changed")
        for path, size, digest in all_entries:
            if any(path == p or path.startswith(p.rstrip("/") + "/") for p in policy.protected_paths) and (
                path not in original or (size, digest) != (original[path].size, original[path].sha256)
            ):
                raise MailboxError("protected_input_changed")
            if (
                (path not in original or original[path].sha256 != digest)
                and path != ".raven-result.md"
                and path not in derived_inputs
                and not any(path == p or path.startswith(p.rstrip("/") + "/") for p in policy.output_paths)
                and (path not in overlays or overlays[path].sha256 != digest)
            ):
                raise MailboxError("undeclared_output")
        descriptors = []
        entries = []
        for path, size, digest in all_entries:
            entries.append(ArtifactEntry(path, digest, size))
            if path not in original or original[path].sha256 != digest:
                descriptors.append(publish_file(self.ledger.store, worker / path, path))
        snapshot = SnapshotManifest(
            baseline.revision_commit,
            baseline.revision_tree,
            baseline.baseline_sha256,
            tuple(entries),
            verifier.fingerprint(),
        )
        raw = self._envelope(intent, attempt, descriptors, output)
        self.ledger.stage_result(self.fence, attempt.attempt_id, raw, snapshot)
        return await self._admit(intent, attempt, raw, policy)

    def _envelope(self, intent, attempt, artifacts, output):
        from datetime import datetime, timezone

        from raven.mailbox.codec import encode_envelope, seal_envelope

        def identity(ref):
            return {k: v for k, v in ref.model_dump().items() if k != "generation"}

        return encode_envelope(
            seal_envelope(
                {
                    "protocol_version": "1.0",
                    "message_id": intent.result_message_id,
                    "trace_id": self.root.root_id,
                    "in_reply_to": intent.request_message_id,
                    "kind": "task.result",
                    "sender_identity": identity(intent.sender_ref),
                    "target_identity": identity(intent.recipient_ref),
                    "scope": {"task_id": self.root.task_id, "workspace_id": self.root.workspace_id},
                    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "ttl": 86400,
                    "receipt_policy": "none",
                    "payload": {
                        "content_type": "application/json",
                        "schema": "opena2a.result/1",
                        "data": {
                            "request_message_id": intent.request_message_id,
                            "outcome": "succeeded",
                            "summary": output[:2048],
                            "evidence": [],
                            "strict_root_id": self.root.root_id,
                            "logical_node_id": attempt.logical_node_id,
                            "attempt_id": attempt.attempt_id,
                            "task_id": intent.task_id,
                            "backend": "raven-loop",
                            "bridge": "host-native-return",
                        },
                    },
                    "artifacts": artifacts,
                    "extensions": {},
                }
            )
        )

    async def _admit(self, intent, attempt, raw, policy):
        from raven.mailbox.codec import decode_envelope
        from raven.mailbox.dag import AdmittedResultKey

        envelope = decode_envelope(raw)
        self.ledger.store.send(raw, intent.sender_ref)
        attempt = self.ledger.record_result(
            self.fence,
            attempt.attempt_id,
            AdmittedResultKey(intent.recipient_ref.agent_id, envelope.message_id, envelope.digest.value),
        )
        return await self._verify(attempt, policy)

    async def _verify(self, attempt, policy):
        parent = Path(self.root.history_root) / "checks" / attempt.attempt_id / str(uuid4())
        verifier = materialize_snapshot(self.ledger.store, attempt.snapshot, parent)
        for spec in policy.checks:
            check = self.ledger.prepare_check(
                self.fence, attempt.attempt_id, spec.check_id, request_id=str(uuid4()), now_ms=int(time.time() * 1000)
            )
            if check.state == "result":
                continue
            check = self.ledger.mark_check_submitting(self.fence, check.check_run_id, request_id=str(uuid4()))
            if not check.submit_now:
                break

            def started(pid):
                from raven.mailbox.dag import ExecutorIdentity

                try:
                    ticks = _start_ticks(pid)
                except FileNotFoundError:
                    return
                actual = ExecutorIdentity(str(uuid4()), pid, self.runtime.executor.linux_boot_id, ticks)
                self.ledger.record_check_started(self.fence, check.check_run_id, actual)

            observation = await verifier.check(
                spec.argv,
                timeout=spec.timeout_seconds,
                cwd=spec.cwd,
                executable_sha256=spec.executable_sha256,
                deadline_ms=check.deadline_ms,
                on_started=started,
            )
            publish_file(self.ledger.store, verifier.stdout_path, "stdout")
            publish_file(self.ledger.store, verifier.stderr_path, "stderr")
            self.ledger.record_check_result(self.fence, check.check_run_id, observation)
        if verifier.fingerprint() != attempt.snapshot.fingerprint:
            self.ledger.record_unknown(
                self.fence, attempt.attempt_id, reason="snapshot_changed", request_id=str(uuid4())
            )
        return self.ledger.decide(self.fence, attempt.attempt_id, request_id=str(uuid4()))
