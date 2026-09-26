"""Curate and install a worker's task Harness using current code, state and execution evidence."""

import asyncio
import json
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from raven.config.live import hold_for_this_turn
from raven.permissions.turn import start_permission_turn

from .generation.context.collect import collect
from .generation.run import GenerationInterruptedError, Limits, generate
from .generation.state import GenerationState
from .raven_adapter.exploration import Exploration
from .raven_adapter.inspection import fingerprint
from .raven_adapter.materialize import _write
from .raven_adapter.observe import plain

PENDING = "pending.json"
CANDIDATE = "candidate.json"


async def improve(worker, provider, *, feedback=None, model=None, limits=Limits(), probe=None, resume=True):
    """Generate and activate one local Harness; composition uses propose before its single activation."""
    if getattr(worker, "children", None):
        from .composition.run import improve as improve_composition

        return await improve_composition(
            worker, provider, feedback=feedback, model=model, limits=limits, probe=probe, resume=resume
        )
    return await _generate_local(
        worker, provider, feedback=feedback, model=model, limits=limits, probe=probe, resume=resume
    )


async def propose(
    worker, provider, *, feedback=None, model=None, limits=Limits(), probe=None, repair=None, observations=None
):
    """Keep a checked local candidate and its investigation for coordinated activation or repair."""
    return await _generate_local(
        worker,
        provider,
        feedback=feedback,
        model=model,
        limits=limits,
        probe=probe,
        install=False,
        repair=repair,
        observations=observations,
    )


async def _generate_local(
    worker,
    provider,
    *,
    feedback=None,
    model=None,
    limits=Limits(),
    probe=None,
    resume=True,
    install=True,
    repair=None,
    observations=None,
):
    """Generate, validate and install a revision; ordinary turns use Worker.run.

    Calling this function explicitly requests curation. Feedback can be human
    text or the Analyst's structured feedback; it is not inferred from every
    worker message. The latest execution retains the artifact identity that
    actually produced it. A paused generation resumes by default with cumulative
    limits; resume=False explicitly discards it. Input changes refuse resumption
    before any model call.
    """
    pending_path = worker.root / "curation" / PENDING
    pending = json.loads(pending_path.read_text()) if pending_path.exists() else None
    candidate_path = pending_path.with_name(CANDIDATE)
    if repair is not None:
        if pending is not None or not candidate_path.is_file():
            raise ValueError("repair requires a completed local candidate with no pending generation")
        pending = json.loads(candidate_path.read_text())
        pending["state"]["validation"] = plain(repair)
        pending["state"]["stage"] = "repair"
        pending["state"]["messages"] = []
        pending["state"]["repairs"] += 1
    if pending is not None and not resume:
        shutil.rmtree(pending["workspace"], ignore_errors=True)
        pending_path.unlink()
        pending = None
    state = GenerationState.model_validate(pending["state"]) if pending else None
    if pending:
        if feedback is None:
            feedback = pending["feedback"]
        if model is None:
            model = pending["model"]
    inspection = await worker.inspect()
    task = inspection.facts.get("task")
    if task is None:
        raise ValueError("curation workflow requires a worker bound to a current task")
    supplied_observations = observations
    observations = []
    if worker.last_execution is not None:
        execution = worker.last_execution
        observations = [
            {
                "kind": "task.execution",
                "task_id": task["id"],
                "turn_id": execution.turn_id,
                "artifact_id": execution.artifact_id,
                "outcome": execution.outcome,
            },
            *execution.records,
        ]
    if supplied_observations is not None:
        observations = supplied_observations
    context_arguments = dict(
        facts=inspection.facts,
        feedback=feedback,
        observations=observations,
        previous_plan=worker.last_plan,
    )

    async def validate(candidate):
        return await worker.check(candidate, probe=probe)

    progress_path = worker.root / "progress" / "curation.json"
    started, latest = time.time(), {}

    def progress(state, **ending):
        """Write where the generation stands; an ending (finished, error) keeps the last state's events."""
        if state is None:
            state = latest.get("state")
        else:
            latest["state"] = state
        if state is not None:
            _write(worker.root / "progress/generation.json", state.model_dump_json(indent=2).encode())
        _write(
            progress_path,
            json.dumps(
                {
                    "started": started,
                    "updated": time.time(),
                    "stage": state.stage if state else None,
                    "calls": state.calls if state else 0,
                    "queries": state.queries if state else 0,
                    "checks": state.checks if state else 0,
                    "repairs": state.repairs if state else 0,
                    "staged": sorted(state.staged) if state else [],
                    "events": [_brief(event) for event in (state.trace[-40:] if state else ())],
                    **ending,
                },
                ensure_ascii=False,
            ).encode(),
        )

    record = {
        "task_id": task["id"],
        "feedback": feedback,
        "turn_id": worker.last_execution.turn_id if worker.last_execution else None,
    }
    workspace = Path(pending["workspace"]) if pending else Path(tempfile.mkdtemp(prefix="raven-curator-explore-"))
    keep_workspace = pending is not None
    try:
        if pending and not (workspace / "snapshot.json").is_file():
            raise ValueError("paused exploration workspace is missing; cannot resume")
        async with Exploration(
            worker.baseline.config, inspection, withheld=worker.withheld, root=workspace
        ) as exploration:
            # Every observation stays readable in the workspace; the request carries only an index of them, and the
            # Curator reads a row on demand with `read_observation`.
            observed = exploration.root / "observations.json"
            observed.write_text(json.dumps(context_arguments["observations"], ensure_ascii=False, default=str))
            described = {**exploration.describe(), "observations_file": str(observed)}
            context = collect(
                task["text"],
                inspection.declaration,
                **context_arguments,
                sources=exploration.sources,
                read_source=exploration.read_source,
                exploration=described,
            )
            record["exploration"] = described
            if state is not None:
                state.check(context, limits)
            pending_path.unlink(missing_ok=True)
            keep_workspace = False

            async def curate():
                start_permission_turn(None, conversation_id=task["id"], turn_id=uuid4().hex, origin="curator")
                with (
                    exploration.registry.turn_scope(),
                    hold_for_this_turn(**{"tools.exec.timeout": worker.baseline.config.tools.exec.timeout}),
                ):
                    return await generate(
                        context,
                        provider,
                        validate=validate,
                        model=model,
                        limits=limits,
                        tool_registry=exploration.registry,
                        stage_candidate=exploration.stage_candidate,
                        resume=state,
                        progress=progress,
                    )

            try:
                result = await asyncio.create_task(curate())
            except GenerationInterruptedError as exc:
                exploration.verify()
                _write(
                    pending_path,
                    json.dumps(
                        {
                            "workspace": str(workspace),
                            "feedback": feedback,
                            "model": model,
                            "limits": asdict(limits),
                            "state": exc.state.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    ).encode(),
                )
                keep_workspace = True
                record["paused"] = {
                    "stage": exc.state.stage,
                    "calls": exc.state.calls,
                    "checkpoint": str(pending_path),
                    "reason": str(exc),
                }
                raise
            record["generated"] = plain(result)
            exploration.verify()
        if install:
            await worker.install(result.candidate)
        else:
            completed = latest["state"]
            completed.candidate = result.candidate
            completed.plan = result.candidate.plan
            completed.validation = result.validation
            _write(
                candidate_path,
                json.dumps(
                    {
                        "workspace": str(workspace),
                        "feedback": feedback,
                        "model": model,
                        "state": completed.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                ).encode(),
            )
            keep_workspace = True
        progress(None, finished=True, error=None)
        return result
    except Exception as exc:
        record["error"] = str(exc)
        progress(None, finished=True, error=str(exc)[:2000])
        record["trace"] = getattr(exc, "trace", ())
        record["records"] = getattr(exc, "records", ())
        raise
    finally:
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
        record["active_artifact_id"] = getattr(
            worker, "revision_id", fingerprint(worker.artifact.model_dump(mode="json"))
        )
        _write(worker.root / "curation" / f"{uuid4().hex}.json", json.dumps(plain(record), ensure_ascii=False).encode())


def _brief(event: dict) -> dict:
    """One generation trace event, cut down to what a live reader needs."""
    row = {"stage": event.get("stage"), "event": event.get("event")}
    for key in ("tool", "path", "call", "content", "error", "errors"):
        if key in event:
            row[key] = str(event[key])[:20000]
    if "arguments" in event:
        row["arguments"] = json.dumps(event["arguments"], ensure_ascii=False)[:4000]
    output = event.get("output")
    if isinstance(output, dict):
        targets = output.get("targets") or [
            c.get("target") for c in (output.get("plan") or output).get("changes", []) or []
        ]
        row["targets"] = [target for target in targets if target]
        if isinstance(output.get("understanding"), str):
            row["understanding"] = output["understanding"][:20000]
    return row
