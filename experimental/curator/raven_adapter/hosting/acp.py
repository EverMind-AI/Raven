"""Serve a checked strategy artifact through Raven's native ACP runtime and session pipeline."""

import argparse
import asyncio
import json
import os
from collections import Counter
from dataclasses import replace
from pathlib import Path

from raven.acp import protocol
from raven.acp.methods import AcpMethodError
from raven.acp.server import install_crash_handlers
from raven.acp.stdio import claim_stdout
from raven.cli.acp_commands import _open_stdin
from raven.rpc.bootstrap import build_rpc_stack

from ...harness import Artifact
from ..baselines import Baseline
from ..bind import assemble
from ..inspection import declaration_for, fingerprint, redact, unavailable_targets
from ..inspection.runtime import describe_bound
from ..observe import Recorder, plain
from .lifecycle import configure_role, start_cron
from .transport import serve

INSPECT = "_curator/inspect"
PAGE_BYTES = 2 * 1024 * 1024
ROW_BYTES = 16 * 1024
_TEXT = 2000
_HEAD, _TAIL = 4, 16


def bounded(value):
    """Shorten long strings and lists; the full value stays in the child's own observation log."""
    if isinstance(value, str) and len(value) > _TEXT:
        return f"{value[:_TEXT]}[{len(value) - _TEXT} more characters in the child's log]"
    if isinstance(value, list) and len(value) > _HEAD + _TAIL:
        hidden = len(value) - _HEAD - _TAIL
        return [
            *map(bounded, value[:_HEAD]),
            f"[{hidden} more items in the child's log]",
            *map(bounded, value[-_TAIL:]),
        ]
    if isinstance(value, list):
        return [bounded(item) for item in value]
    if isinstance(value, dict):
        return {key: bounded(item) for key, item in value.items()}
    return value


def page(rows, offset, budget=PAGE_BYTES):
    """Records from `offset` for one inspect response, below `budget` bytes once encoded.

    A child's full records reach hundreds of megabytes (every provider request with its whole context, every
    streamed delta), while one ACP frame must stay under the client's line limit. Streamed deltas therefore stay
    only in the child's log, since the response row carries their result, and an oversized row is shortened.
    """
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= len(rows):
        raise AcpMethodError(protocol.INVALID_PARAMS, "inspect offset must be a record index")
    records, omitted, size, index = [], Counter(), 0, offset
    while index < len(rows):
        row = rows[index]
        if row["kind"] == "provider.delta":
            omitted[row["kind"]] += 1
            index += 1
            continue
        encoded = len(json.dumps(row))
        if encoded > ROW_BYTES:
            row = bounded(row)
            encoded = len(json.dumps(row))
        if records and size + encoded > budget:
            break
        records.append(row)
        size += encoded
        index += 1
    return {"records": records, "next": index, "omitted": dict(omitted)}


async def serve_harness(reader, out, baseline, artifact, root, *, grants=None, provider_factory=None):
    """Own one real child runtime; inspection observes that same runtime, not a reconstructed proxy."""
    if baseline.hosting != "acp" or baseline.allow_delegation:
        raise ValueError("a child ACP Harness needs ACP hosting and delegation disabled")
    configure_role(baseline)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(root / "observations.jsonl")
    bound = None
    stack = None

    async def build(translator, *, channel, approval_responder):
        nonlocal bound, stack
        declaration = declaration_for("assembly", unavailable_targets(baseline), **(grants or {}))
        bound = assemble(
            baseline, artifact, declaration, root / "assembly", recorder, provider_factory, root / "planning.json"
        )
        await bound.prepare()
        stack = await build_rpc_stack(
            translator.send_frame, agent_loop=bound.runtime.loop, channel=channel, approval_responder=approval_responder
        )
        await bound.start()
        await start_cron(bound.runtime.loop, stack)
        recorder.add("hosting.ready", revision=fingerprint(artifact.model_dump(mode="json")), pid=os.getpid())
        return replace(stack, teardown=close)

    async def inspect(params):
        if set(params or {}) - {"offset"}:
            raise AcpMethodError(protocol.INVALID_PARAMS, "inspect takes only an optional record offset")
        if bound is None:
            raise AcpMethodError(protocol.INTERNAL_ERROR, "Harness is not assembled")
        inspected = describe_bound(bound)
        inspected["grants"] = grants or {}
        report = {
            "inspection": inspected,
            "revision": fingerprint(artifact.model_dump(mode="json")),
            "pid": os.getpid(),
            "log": str(recorder.path),
            "total": len(recorder.rows),
        }
        if params and "offset" in params:
            report.update(page(recorder.rows, params["offset"]))
        return plain(redact(report))

    async def close():
        nonlocal bound, stack
        active, bound = bound, None
        rpc, stack = stack, None
        if active is None:
            return
        if active.runtime.loop.cron_service is not None:
            active.runtime.loop.cron_service.stop()
        try:
            if rpc is not None:
                await rpc.teardown()
        finally:
            await active.close()

    try:
        await serve(reader, out, stack_factory=build, extensions={INSPECT: inspect})
    finally:
        await close()


async def main(path: Path, *, provider_factory=None):
    deployment = json.loads(path.read_text())
    if set(deployment) - {"baseline", "artifact", "state_dir", "grants"}:
        raise ValueError("unknown Harness deployment fields")
    baseline = Baseline.restore(deployment["baseline"])
    if baseline.inherit_model:
        from copy import deepcopy

        from raven.config.product_render import inherit_llm
        from raven.config.schema import Config

        config = baseline.config.model_dump(mode="json", by_alias=True)
        inherit_llm(config, deepcopy(config))
        baseline.config = Config.model_validate(config)
        baseline.extensions.base = baseline.config
    artifact = Artifact.model_validate(deployment["artifact"])
    os.chdir(baseline.workdir)
    install_crash_handlers()
    with claim_stdout() as out:
        async with _open_stdin() as reader:
            await serve_harness(
                reader,
                out,
                baseline,
                artifact,
                Path(deployment["state_dir"]),
                grants=deployment.get("grants"),
                provider_factory=provider_factory,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Host a generated child Harness over native ACP")
    parser.add_argument("--deployment", required=True, type=Path)
    asyncio.run(main(parser.parse_args().deployment))
