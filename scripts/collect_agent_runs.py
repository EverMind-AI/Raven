#!/usr/bin/env python3
"""Collect real, tool-using agent runs into the corpus's run-record directory.

The corpus built by ``scripts/evidence_corpus.py`` ingests **run records** -
frame logs written by Raven's ACP client while it drives an agent. The seven
records this machine had were **transport checks**: the connection worked, the
agent answered, and it called no tool. So the corpus attested the transport
and nothing else, which its own README says.

This script produces the other kind. It drives Raven's own ACP agent
(``raven acp``) with a task that requires a tool call, lets the client
journal the frames, and leaves the journals in a directory the corpus
builder can read.

    uv run python scripts/collect_agent_runs.py --runs <dir> --count 10

It is deliberately boring: no retries that hide a failure, no synthetic
frames, and a run that produced no tool call is a run that is not kept. The
corpus is meant to be evidence, and evidence that has been massaged is not
evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# The tracing state dir decides where the frame journals land, and the journal
# is written by THIS process (the ACP client), not by the agent it spawns.
# So the override has to be in place before anything imports raven.tracing.
TRACING_ENV = "RAVEN_TRACING_DIR"


#: The tasks the agent is driven with. Each one cannot be answered without a
#: tool call, and each asks a different question of a different page, so the
#: corpus holds a spread rather than one run repeated a hundred times.
#:
#: The pages are deliberately small. The journal carries the whole tool
#: result, and AGENTS.md section 7 keeps large files out of the repository.
FETCH_TASKS = [
    ("https://example.com", "the exact title of the page"),
    ("https://example.net", "the exact title of the page"),
    ("https://example.org", "the exact title of the page"),
    ("https://www.iana.org/help/example-domains", "what the page says example domains are reserved for"),
    ("https://www.iana.org/domains/root", "the first top-level domain the page lists"),
    ("https://www.rfc-editor.org/rfc/rfc2606.txt", "the RFC number the document carries"),
]

#: A local file the agent has to read. Written into the run's workspace, so
#: the answer is in the frames and the workspace is not part of the corpus.
FIXTURE_LINES = [
    ("ledger_entry_id", "the entry id on the first line"),
    ("settlement_total", "the settlement total on the second line"),
    ("counterparty", "the counterparty on the third line"),
]

FIXTURE = """ledger_entry_id: corpus-entry-{index}
settlement_total: {total}
counterparty: example-counterparty-{index}
"""


def _task(index: int) -> str:
    """One task that cannot be answered without a tool call."""
    if index % 4 == 3:
        line_no = (index // 4) % len(FIXTURE_LINES)
        key, what = FIXTURE_LINES[line_no]
        return (
            f"Use the read_file tool to read the file fixture.txt in your workspace and then "
            f"reply with {what}, followed by the word DONE. You must read the file; do not "
            f"answer from memory. If the read fails, reply with READ_FAILED and the reason."
        )
    url, what = FETCH_TASKS[index % len(FETCH_TASKS)]
    return (
        f"Use the web_fetch tool to read {url} and then reply with {what}, followed by the "
        f"word DONE. You must fetch the page; do not answer from memory. If the fetch fails, "
        f"reply with FETCH_FAILED and the reason."
    )


def _workspace(runs_dir: Path, index: int) -> Path:
    """The run's workspace, with the fixture the read tasks need."""
    workspace = runs_dir / f"workspace-{index}"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "fixture.txt").write_text(
        FIXTURE.format(index=index, total=1000 + index), encoding="utf-8"
    )
    return workspace


async def _collect(runs_dir: Path, count: int, home: Path, model: str | None) -> int:
    from raven.acp_client.acp_agent import AcpAgentBackend
    from raven.acp_client.pool import close_pool

    binary = Path(sys.executable).with_name("raven")
    if not binary.exists():
        print(f"no raven binary beside {sys.executable}", file=sys.stderr)
        return 2

    kept = 0
    for index in range(count):
        backend = AcpAgentBackend(
            name=f"raven-self-{index}",
            command=f"{binary} acp",
            env={"RAVEN_HOME": str(home), TRACING_ENV: str(runs_dir / "traces")},
            ready_timeout_ms=120_000,
            timeout=300,
        )
        workspace = _workspace(runs_dir, index)
        try:
            reply = await backend.run(
                _task(index),
                task_id=f"corpus-run-{index}",
                workspace=workspace,
                executor=None,
                session_key=f"corpus:run-{index}",
                session_model=model,
            )
        except Exception as exc:  # noqa: BLE001 - one failed run is not a failed batch
            print(f"run {index}: failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            await close_pool()
            continue
        finally:
            await close_pool()

        journals = sorted((runs_dir / "traces" / "logs" / "acp-frames").rglob("*.jsonl"))
        newest = journals[-1] if journals else None
        tool_calls = 0
        if newest is not None:
            tool_calls = newest.read_text(encoding="utf-8", errors="ignore").count('"tool_call"')
        print(
            f"run {index}: reply={reply.strip()[:60]!r} journal={newest.name if newest else None} "
            f"tool_call_markers={tool_calls}"
        )
        if tool_calls:
            kept += 1
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", required=True, type=Path, help="where the run records land")
    parser.add_argument("--count", type=int, default=1, help="how many runs to drive")
    parser.add_argument("--model", default=None, help="the model the spawned agent answers with")
    parser.add_argument("--home", default=str(Path.home() / ".raven"), help="the agent's RAVEN_HOME")
    args = parser.parse_args()

    args.runs.mkdir(parents=True, exist_ok=True)
    os.environ[TRACING_ENV] = str(args.runs / "traces")

    kept = asyncio.run(_collect(args.runs, args.count, Path(args.home), args.model))
    print(json.dumps({"runs": args.count, "with_tool_calls": kept}, indent=2))
    return 0 if kept else 1


if __name__ == "__main__":
    raise SystemExit(main())
