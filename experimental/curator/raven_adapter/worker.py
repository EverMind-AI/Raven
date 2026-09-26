"""A persistent experimental worker process owning Raven's native runtime and configuration."""

import asyncio
import json
import multiprocessing
import os
import signal
import tempfile
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from raven.spine.message import ChatType, Source
from raven.spine.scheduler import conversation_id
from raven.spine.turn import TurnRequest

from ..harness import Artifact, Candidate, Validation
from .deployment import Child, bind_children, child_directory
from .exploration import Withheld
from .inspection import Baseline, Inspection, declaration_for, fingerprint, unavailable_targets
from .inspection.runtime import describe_bound, playbook_nodes
from .materialize import (
    _write,
    copy_local_state,
    extend_artifact,
    restore_content,
    save_content,
)
from .observe import Recorder, observed_errors, plain
from .strategy import SESSION
from .targets import catalogue


def _send(connection, value):
    connection.send_bytes(json.dumps(plain(value), ensure_ascii=False).encode())


def _receive(connection):
    return json.loads(connection.recv_bytes())


async def follow_up(busy, announcements, run_turn, *, poll=0.5):
    """Run what finished background work announces, as follow-up turns, until nothing is running or waiting.

    Raven delivers a background sub-agent's or playbook graph's result to its conversation as a new turn;
    the worker does the same before it returns, so a delegated deliverable lands in the exchange that asked
    for it. `busy` says whether any background work is still in flight.
    """
    while True:
        while busy() and announcements.empty():
            await asyncio.sleep(poll)
        if announcements.empty():
            return
        request = announcements.get_nowait()
        await run_turn(request if request.turn_id else replace(request, turn_id=uuid4().hex))


def _serve(
    connection,
    baseline_data,
    artifact_data,
    limits,
    root,
    provider_factory,
    start_resources,
    probe,
    copy_inputs,
    planning_state,
    restored_content,
    children_data,
    restored_children,
):
    from .bind import assemble

    if os.name == "posix":
        os.setsid()

    async def main():
        baseline = Baseline.restore(baseline_data)
        children = {name: Child.restore(value) for name, value in children_data.items()}
        artifact = Artifact.model_validate(artifact_data)
        root_path = Path(root)
        recorder = Recorder(root_path / "observations.jsonl")
        bound = None
        try:
            if copy_inputs is not None:
                baseline = copy_local_state(baseline, root_path / "state", copy_inputs)
                for name, child in children.items():
                    child.baseline = copy_local_state(
                        child.baseline, root_path / "state" / "children" / fingerprint(name)[:16], copy_inputs
                    )
            if restored_content:
                restore_content(
                    baseline.config.workspace_path,
                    {baseline.config.workspace_path / name: value for name, value in restored_content.items()},
                )
            for name, content in restored_children.items():
                home = children[name].baseline.config.workspace_path
                restore_content(home, {home / path: value for path, value in content.items()})
            baseline = bind_children(baseline, children, Path(planning_state).parent)
            from .hosting.lifecycle import configure_role

            configure_role(baseline)
            os.chdir(baseline.workdir)
            declaration = declaration_for("assembly", unavailable_targets(baseline), **limits)
            # Assembly sources outlive a process so paused curation keeps the same material locations.
            assembly_root = root_path.parent / "assembly"
            bound = assemble(
                baseline, artifact, declaration, assembly_root, recorder, provider_factory, Path(planning_state)
            )
            if children:
                from .capability.protected import protect_delegation

                protect_delegation(
                    bound.runtime.loop,
                    [row for row in baseline.config.subagents.agents if row.name in children],
                )
            await bound.prepare()
            announcements = asyncio.Queue()
            loop = bound.runtime.loop
            loop.subagents.set_submit(announcements.put_nowait)

            async def dag_progress(conversation, name, payload):
                recorder.add("dag.progress", conversation=conversation, name=name, payload=payload)

            loop.set_dag_progress_sink(dag_progress)

            def busy():
                from raven.agent.subagent.dag_live import live_run_ids

                return bool(loop.subagents.get_running_count() or live_run_ids(loop))

            if start_resources:
                await bound.start()
            _send(connection, {"ok": True, "data": {"records": recorder.rows}})
            while True:
                request = await asyncio.to_thread(_receive, connection)
                offset = len(recorder.rows)
                try:
                    operation = request["operation"]
                    if operation == "close":
                        from raven.acp_client.pool import close_pool

                        try:
                            await bound.close()
                        finally:
                            bound = None
                            await close_pool()
                        _send(connection, {"ok": True})
                        break
                    if operation == "idle":
                        data = not busy() and announcements.empty()
                    elif operation == "inspect":
                        data = describe_bound(bound, recorder.rows[:offset])
                        if children:
                            from .inspection import redact

                            data["facts"]["scope"] = {"kind": "root"}
                            data["facts"]["composition"] = {
                                "nodes": playbook_nodes(bound.runtime, bound.baseline),
                                "children": {name: redact(child.export()) for name, child in children.items()},
                            }
                            data["identity"] = fingerprint(
                                {"root": data["identity"], "composition": data["facts"]["composition"]}
                            )
                    elif operation == "inspect_agent":
                        from .hosting.connection import connection as agent_connection
                        from .hosting.evidence import inspect as inspect_child

                        backend = loop.subagents._resolve_backend(request["agent"])
                        if getattr(backend, "kind", None) != "acp":
                            raise ValueError("this child has not been prepared for full Harness hosting")
                        session = request.get("session_key")
                        binding = loop.binding_for_session(session) if session is not None else loop.default_binding
                        active = await agent_connection(
                            backend, workspace=baseline.workdir, provider=binding.provider, model=binding.model
                        )
                        data = await inspect_child(active.client, offset=0)
                    elif operation == "probe":
                        from .validate import run_probe

                        await run_probe(bound, probe)
                        data = {"records": recorder.rows[offset:]}
                    elif operation == "run":
                        from raven.agent.spine_runner import AgentTurnRunner

                        turn = TypeAdapter(TurnRequest).validate_python(request["turn"])
                        if turn.origin != baseline.origin:
                            raise ValueError("turn origin differs from the inspected worker origin")
                        if baseline.mode is not None:
                            from raven.config.mode_catalogue import build_mode_catalogue

                            profile = build_mode_catalogue(bound.baseline.config).get(baseline.mode)
                            if profile is None:
                                raise ValueError(f"configured worker mode is unavailable: {baseline.mode}")
                            bound.runtime.loop.set_session_policy(
                                turn.conversation or f"{turn.source.channel}:{turn.source.chat_id}",
                                max_iterations=profile.max_iterations,
                                mode=profile.id,
                                mode_overlay=profile.overlay,
                                reasoning_effort=profile.reasoning_effort,
                            )
                        recorder.turn_id = turn.turn_id
                        events = []

                        async def emit(event):
                            events.append(plain(event))
                            recorder.add("runner.event", event_type=type(event).__name__, event=event)

                        async def run_turn(request_turn):
                            token = SESSION.set(conversation_id(request_turn))
                            try:
                                return await AgentTurnRunner(
                                    bound.runtime.loop, stream=request.get("stream", False)
                                ).run(request_turn, emit, lambda: [])
                            finally:
                                SESSION.reset(token)

                        from .hosting.evidence import record_children, snapshots

                        child_before = await snapshots(children)
                        try:
                            outcome = await run_turn(turn)
                            await follow_up(busy, announcements, run_turn)
                            await record_children(
                                children, child_before, recorder, conversation_id(turn), recorder.rows[offset:]
                            )
                        finally:
                            bound.observer.finish()
                            recorder.turn_id = None
                        data = {"events": events, "records": recorder.rows[offset:], "outcome": plain(outcome)}
                    else:
                        raise ValueError(f"unknown worker operation: {operation}")
                    _send(connection, {"ok": True, "data": data})
                except Exception as exc:
                    recorder.add(
                        "runtime.error", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc()
                    )
                    _send(
                        connection,
                        {"ok": False, "error": f"{type(exc).__name__}: {exc}", "records": recorder.rows[offset:]},
                    )
        except Exception as exc:
            recorder.add("runtime.error", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
            _send(connection, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "records": recorder.rows})
        finally:
            from raven.acp_client.pool import close_pool

            try:
                if bound is not None:
                    await bound.close()
            finally:
                await close_pool()
                connection.close()

    asyncio.run(main())


@dataclass(frozen=True)
class Execution:
    """One turn's evidence; `deliverables` are copies of the files the turn handed over through deliver_files."""

    turn_id: str
    events: list[dict]
    records: list[dict]
    outcome: dict
    artifact_id: str = ""
    deliverables: tuple[str, ...] = ()

    @property
    def errors(self):
        return observed_errors(self.records)

    @property
    def text(self):
        return "\n".join(
            row["event"]["content"]
            for row in self.records
            if row["kind"] == "runner.event" and row["event_type"] == "Text"
        )


class WorkerError(RuntimeError):
    def __init__(self, message, records=()):
        super().__init__(message)
        self.records = list(records)


class Worker:
    """Keep native state across turns; rebuild only when the Harness changes.

    `withheld` names the repository files its Curator may not read while it curates this worker and its children.
    """

    def __init__(
        self,
        baseline: Baseline,
        root: Path,
        *,
        provider_factory=None,
        names=None,
        fields=None,
        phases=None,
        timeout: float = 120,
        children=None,
        withheld: Withheld = Withheld(),
    ):
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("worker timeout must be finite and positive")
        self.baseline = Baseline.restore(baseline.export())
        self.root = Path(root).resolve()
        self.provider_factory = provider_factory
        self._prepare_children = children if callable(children) else None
        supplied_children = {} if self._prepare_children else children or {}
        self.children = {
            name: Child.restore(child.export()) if isinstance(child, Child) else Child(child)
            for name, child in supplied_children.items()
        }
        for child in self.children.values():
            if child.baseline.task is None:
                child.baseline.task = self.baseline.task
        self.limits = {
            "names": list(names) if names is not None else None,
            "fields": {name: list(value) for name, value in fields.items()} if fields is not None else None,
            "phases": {name: list(value) for name, value in phases.items()} if phases is not None else None,
        }
        self.timeout = timeout
        self.withheld = withheld
        self.artifact = Artifact(values={})
        self.last_plan = None
        self.last_execution = None
        self._process = None
        self._connection = None
        self._generation_root = None
        self._copy_inputs = None
        self._content_baseline = {}
        self._restored_content = {}
        self._restored_children = {}
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def start(self, *, start_resources=True, probe=None):
        async with self._lock:
            await self._start(start_resources=start_resources, probe=probe)

    async def _start(self, *, start_resources=True, probe=None):
        if self._process is not None:
            raise RuntimeError("worker is already started")
        self.root.mkdir(parents=True, exist_ok=True)
        if self._prepare_children is not None:
            prepared = self._prepare_children(self.baseline)
            self.children = {
                name: child if isinstance(child, Child) else Child(child) for name, child in prepared.items()
            }
            for child in self.children.values():
                if child.baseline.task is None:
                    child.baseline.task = self.baseline.task
            self._prepare_children = None
        from .deployment import check_content_owners

        check_content_owners(self.baseline, self.artifact, self.children)
        if self._copy_inputs is None:
            owners = [
                (self.baseline, self.artifact, self.limits),
                *((child.baseline, child.artifact, child.grants) for child in self.children.values()),
            ]
            for baseline, artifact, grants in owners:
                declared = declaration_for("content", unavailable_targets(baseline), **grants)
                for path, value in save_content(baseline.config.workspace_path, artifact, declared).items():
                    self._content_baseline.setdefault(path, value)
        self._generation_root = self.root / uuid4().hex
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self._connection = parent
        self._process = context.Process(
            target=_serve,
            args=(
                child,
                self.baseline.export(),
                self.artifact.model_dump(mode="json"),
                self.limits,
                str(self._generation_root),
                self.provider_factory,
                start_resources,
                probe,
                self._copy_inputs,
                str(self.root / "planning.json"),
                self._restored_content,
                {name: child.export() for name, child in self.children.items()},
                self._restored_children,
            ),
        )
        self._process.start()
        child.close()
        try:
            reply = await asyncio.wait_for(asyncio.to_thread(_receive, parent), self.timeout)
            self._assembly_records = self._check_reply(reply)["records"]
        except WorkerError:
            await self._terminate(grace=5)
            raise
        except (asyncio.TimeoutError, EOFError) as exc:
            await self._terminate()
            raise WorkerError("worker timed out or exited during startup", self.records()) from exc
        except BaseException:
            await self._terminate()
            raise

    @staticmethod
    def _check_reply(reply):
        if not reply["ok"]:
            raise WorkerError(reply["error"], reply.get("records", ()))
        return reply.get("data")

    async def _exchange(self, request):
        if self._process is None or not self._process.is_alive():
            raise WorkerError("worker is not running")
        try:
            _send(self._connection, request)
            reply = await asyncio.wait_for(asyncio.to_thread(_receive, self._connection), self.timeout)
            return self._check_reply(reply)
        except asyncio.CancelledError:
            await self._terminate()
            raise
        except (asyncio.TimeoutError, EOFError) as exc:
            await self._terminate()
            raise WorkerError("worker timed out or exited during the operation", self.records()) from exc

    def records(self) -> list[dict]:
        if self._generation_root is None:
            return []
        path = self._generation_root / "observations.jsonl"
        if not path.is_file():
            return []
        result = []
        for line in path.read_text(errors="replace").splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                result.append({"kind": "runtime.error", "error": "An observation record was incomplete at shutdown."})
        return result

    async def inspect(self) -> Inspection:
        async with self._lock:
            return await self._inspect()

    async def agent_state(self, name: str, *, session_key: str | None = None):
        """Read facts and execution evidence from the child selected by the native launch binding."""
        async with self._lock:
            return await self._exchange({"operation": "inspect_agent", "agent": name, "session_key": session_key})

    async def inspect_agent(self, name: str, *, session_key: str | None = None) -> Inspection:
        return Inspection.restore((await self.agent_state(name, session_key=session_key))["inspection"])

    @property
    def revision_id(self):
        if not self.children:
            return fingerprint(self.artifact.model_dump(mode="json"))
        return fingerprint(
            {
                "root": self.artifact.model_dump(mode="json"),
                "children": {
                    name: {
                        "baseline": child.baseline.export(),
                        "artifact": child.artifact.model_dump(mode="json"),
                        "grants": child.grants,
                    }
                    for name, child in self.children.items()
                },
            }
        )

    async def _inspect(self) -> Inspection:
        data = await self._exchange({"operation": "inspect"})
        return Inspection.restore(data, **self.limits)

    async def run(self, request: str | TurnRequest, *, session_key="curator:task", stream=False) -> Execution:
        if isinstance(request, str):
            channel, _, chat_id = session_key.partition(":")
            request = TurnRequest(
                origin=self.baseline.origin,
                source=Source(channel=channel, chat_id=chat_id, sender_id="user", chat_type=ChatType.DM),
                text=request,
                conversation=session_key,
                turn_id=uuid4().hex,
            )
        if request.turn_id is None:
            request = replace(request, turn_id=uuid4().hex)
        async with self._lock:
            artifact_id = self.revision_id
            data = await self._exchange(
                {
                    "operation": "run",
                    "turn": TypeAdapter(TurnRequest).dump_python(request, mode="json"),
                    "stream": stream,
                }
            )
            self.last_execution = Execution(
                request.turn_id,
                data["events"],
                data["records"],
                data["outcome"],
                artifact_id,
                self._keep_deliverables(request.turn_id, data["records"]),
            )
            return self.last_execution

    def _keep_deliverables(self, turn_id, records) -> tuple[str, ...]:
        """Copy what the turn delivered, so a later turn rewriting the same file does not erase this one."""
        kept = []
        for row in records:
            event = row.get("event") or {}
            if row["kind"] != "runner.event" or event.get("name") != "deliver_files" or event.get("phase") != "start":
                continue
            for item in (event.get("arguments") or {}).get("files", []):
                source = Path(str(item.get("path", "")))
                source = source if source.is_absolute() else self.baseline.workdir / source
                if source.is_file():
                    target = self.root / "deliverables" / turn_id / source.name
                    _write(target, source.read_bytes())
                    kept.append(str(target))
        return tuple(kept)

    def _accept(self, candidate, inspection):
        candidate = inspection.declaration.validate(candidate)
        for name in candidate.artifact.remove:
            if name not in self.artifact.values:
                raise ValueError(f"cannot retire a target that is not currently authored: {name}")
            inspection.declaration.target(name).parse(self.artifact.values[name])
        return candidate

    def _release_edited(self, submitted, proposed, declaration):
        from .materialize import release_edited

        return release_edited(self.baseline.config.workspace_path, self.artifact, submitted, proposed, declaration)

    def _retired_content(self, proposed, declaration):
        from .materialize import retired_content

        return retired_content(
            self.baseline.config.workspace_path, self.artifact, proposed, declaration, self._content_baseline
        )

    async def install(self, candidate: Candidate, *, children=None):
        """Activate one set; an explicit child value of None restores its supplied baseline artifact."""
        from .deployment import activate

        return await activate(self, candidate, children)

    async def _terminate(self, *, grace=0):
        process, connection = self._process, self._connection
        self._process = self._connection = None
        if process is not None:
            if grace:
                await asyncio.to_thread(process.join, grace)
            try:
                if process.is_alive():
                    if os.name == "posix" and os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
            except ProcessLookupError:
                pass
            await asyncio.to_thread(process.join, 5)
            try:
                if process.is_alive():
                    if os.name == "posix" and os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    await asyncio.to_thread(process.join, 5)
            except ProcessLookupError:
                pass
            process.close()
        if connection is not None:
            connection.close()

    async def _close(self):
        if self._process is None:
            return
        try:
            if self._process.is_alive():
                await asyncio.wait_for(self._exchange({"operation": "close"}), self.timeout)
        finally:
            await self._terminate(grace=1)

    async def close(self):
        if self._lock.locked():
            await self._terminate()
        else:
            async with self._lock:
                await self._close()

    async def check(self, candidate: Candidate, *, probe=None):
        """Construct a candidate on copied local state; optional host probes exercise behavior."""
        async with self._lock:
            return await self._check(candidate, probe=probe)

    async def _check(self, candidate: Candidate, *, probe=None, inspection=None):
        try:
            async with self._validation_copy(candidate, probe=probe, inspection=inspection) as trial:
                data = await trial._exchange({"operation": "probe"})
                await trial.inspect()
                observations = [*trial._assembly_records, *data["records"]]
                return Validation(
                    [row.get("error", row["kind"]) for row in observed_errors(observations)], observations
                )
        except (WorkerError, asyncio.TimeoutError, ValueError) as exc:
            return Validation([str(exc) or "validation timed out"], getattr(exc, "records", []))

    async def check_composition(self, candidate, children, *, probe=None):
        """Start the complete proposed set on copied state before touching the active deployment."""
        async with self._lock:
            try:
                async with self._validation_copy(candidate, children=children, probe=probe) as trial:
                    observations = list(trial._assembly_records)
                    for name in trial.children:
                        report = await trial.agent_state(name)
                        observations.append(
                            {
                                "kind": "composition.child_ready",
                                "harness": name,
                                "revision": report["revision"],
                                "pid": report["pid"],
                            }
                        )
                        observations.append(
                            {
                                "kind": "child.execution",
                                "harness": name,
                                "revision": report["revision"],
                                "records": report["records"],
                            }
                        )
                    tested = await trial._exchange({"operation": "probe"})
                    observations.extend(tested["records"])
                    return Validation(
                        [row.get("error", row["kind"]) for row in observed_errors(observations)], observations
                    )
            except (WorkerError, asyncio.TimeoutError, ValueError) as exc:
                return Validation([str(exc) or "composition validation timed out"], getattr(exc, "records", []))

    async def preview_nodes(self, candidate):
        """Read the proposed native graph on an isolated copy; no temporary source paths escape."""
        async with self._lock:
            async with self._validation_copy(candidate) as trial:
                viewed = await trial.inspect()
                return viewed.facts.get("composition", {}).get("nodes", {})

    @asynccontextmanager
    async def _validation_copy(self, candidate, *, probe=None, inspection=None, children=None):
        from .validate import static_checks

        inspection = await self._inspect() if inspection is None else inspection
        candidate = self._accept(candidate, inspection)
        errors = static_checks(candidate)
        if errors:
            raise ValueError("; ".join(errors))
        with tempfile.TemporaryDirectory(prefix="raven-curator-check-") as temporary:
            temp = Path(temporary)
            trial = Worker(
                self.baseline,
                temp / "runtime",
                provider_factory=self.provider_factory,
                timeout=self.timeout,
                children=self.children,
                **self.limits,
            )
            trial._copy_inputs = (str(self.root), str(temp))
            from .deployment import child_artifact
            from .materialize import retired_content

            for name, submitted in (children or {}).items():
                child = trial.children[name]
                report = await self._exchange({"operation": "inspect_agent", "agent": name})
                viewed = Inspection.restore(report["inspection"])
                home = child.baseline.config.workspace_path
                effective = child_artifact(child, submitted, viewed, restoring=submitted is None)
                trial._restored_children[name] = {
                    str(path.relative_to(home)): value
                    for path, value in retired_content(
                        home,
                        child.artifact,
                        effective,
                        viewed.declaration,
                        self._content_baseline,
                    ).items()
                }
                child.artifact = effective
            for name in self.children:
                old_root, new_root = child_directory(self.root, name), child_directory(trial.root, name)
                for target in catalogue():
                    if target.binding.endswith(".strategy"):
                        saved = old_root / f"{target.name.split('.')[0]}.json"
                        if saved.exists():
                            _write(new_root / saved.name, saved.read_bytes())
            for target in catalogue():
                if target.binding.endswith(".strategy"):
                    checkpoint = self.root / f"{target.name.split('.')[0]}.json"
                    if checkpoint.exists():
                        _write(trial.root / checkpoint.name, checkpoint.read_bytes())
            try:
                trial.artifact = self._release_edited(
                    candidate.artifact, extend_artifact(self.artifact, candidate.artifact), inspection.declaration
                )
                trial._restored_content = {
                    str(path.relative_to(self.baseline.config.workspace_path)): value
                    for path, value in self._retired_content(trial.artifact, inspection.declaration).items()
                }
                await trial.start(start_resources=False, probe=probe)
                yield trial
            finally:
                await trial.close()
