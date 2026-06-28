"""
Bot-mode Executor — runs a full AgentLoop end-to-end for one benchmark task.

This executor:
1. Builds a full AgentLoop (executor / MCP / sandbox)
2. Submits the task prompt as one USER turn through the spine ``run_turn``
3. Accumulates the reply text from the emitted Text events
4. Extracts transcripts from session for grading
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

from task_loader import Task

logger = logging.getLogger(__name__)

# Default OpenRouter config
DEFAULT_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DEFAULT_MODEL = os.environ.get("RAVEN_BENCH_MODEL", "anthropic/claude-sonnet-4")

CHANNEL_NAME = "benchmark"


def prepare_workspace(task: Task, workspace: Path, assets_dir: Path) -> Path:
    """Prepare an isolated workspace for a task, copying fixture files."""
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    for file_spec in task.workspace_files:
        if "content" in file_spec:
            dest = workspace / file_spec["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(file_spec["content"])
            continue

        source_key = file_spec.get("source", "")
        dest_key = file_spec.get("dest", source_key)
        source = assets_dir / source_key
        dest = workspace / dest_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            logger.error("Asset not found: %s", source)
            continue
        dest.write_bytes(source.read_bytes())

    return workspace


def _session_to_openclaw_transcript(
    session_messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Convert Raven session messages (OpenAI format) to PinchBench/OpenClaw
    transcript format so that existing grading functions work unchanged.
    """
    transcript: List[Dict[str, Any]] = []

    for msg in session_messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            text = content if isinstance(content, str) else str(content)
            transcript.append({
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [text],
                },
            })

        elif role == "assistant":
            items: List[Dict[str, Any]] = []
            if content:
                items.append({"type": "text", "text": content})

            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        args = {"raw": args}

                items.append({
                    "type": "toolCall",
                    "name": func.get("name", ""),
                    "arguments": args,
                })

            transcript.append({
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": items,
                },
            })

        elif role == "tool":
            result_text = content if isinstance(content, str) else str(content)
            transcript.append({
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "content": [result_text],
                },
            })

    return transcript


def _make_openrouter_provider(model: str, api_key: str):
    """Create an OpenRouter LLM provider via LiteLLM."""
    from raven.providers.base import GenerationSettings
    from raven.providers.litellm_provider import LiteLLMProvider

    provider = LiteLLMProvider(
        api_key=api_key,
        api_base="https://openrouter.ai/api/v1",
        default_model=model,
        provider_name="openrouter",
    )
    provider.generation = GenerationSettings(
        temperature=0.7,
        max_tokens=8192,
    )
    return provider


async def execute_task(
    task: Task,
    workspace: Path,
    assets_dir: Path,
    model: str = DEFAULT_MODEL,
    api_key: str = DEFAULT_API_KEY,
    timeout_multiplier: float = 1.0,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Execute a single benchmark task by running one USER turn through the
    spine ``run_turn`` and accumulating the reply text.

    Returns a result dict compatible with PinchBench grading:
        task_id, status, transcript, workspace, execution_time, timed_out
    """
    from raven.agent.loop import AgentLoop
    from raven.config.schema import ExecToolConfig
    from raven.session.manager import SessionManager
    from raven.spine import ChatType, Origin, Source, Text, TurnRequest

    # Prepare workspace
    task_workspace = prepare_workspace(task, workspace, assets_dir)

    # Create OpenRouter provider
    provider = _make_openrouter_provider(model, api_key)

    session_mgr = SessionManager(task_workspace)
    session_key = f"{CHANNEL_NAME}:{task.task_id}"

    # Load skill_forge config so injection_mode / inject_max / mass_library_db
    # etc. are honored under bot benchmark runs. Without this AgentLoop gets
    # skill_forge_config=None → SkillService falls back to dataclass defaults.
    from raven.config.raven import load_raven_config
    _ec_cfg = load_raven_config()
    skill_forge_cfg = getattr(_ec_cfg, "skill_forge", None)

    agent = AgentLoop(
        provider=provider,
        workspace=task_workspace,
        model=model,
        max_iterations=40,
        context_window_tokens=65_536,
        exec_config=ExecToolConfig(),
        restrict_to_workspace=True,
        session_manager=session_mgr,
        skill_forge_config=skill_forge_cfg,
        runtime_config=getattr(_ec_cfg, "runtime", None),
        # Benchmarks are non-interactive batch runs — opt out of Bug2's
        # per-turn shadow-git checkpoint (no recovery channel to inject
        # into, and we don't want ``.raven/shadow.git`` in task workspaces).
        interactive=False,
    )

    timeout_seconds = task.timeout_seconds * timeout_multiplier
    start_time = time.time()
    status = "success"
    timed_out = False
    response_content = ""

    logger.info(
        "Executing task %s (%s) via bot mode — timeout %.0fs",
        task.task_id, task.name, timeout_seconds,
    )

    # --- Run one USER turn through the spine; accumulate the reply text ---
    # run_turn lazily starts the executor / MCP on first call, so no separate
    # runtime task is needed. Non-streaming → the reply arrives as Text events.
    parts: List[str] = []

    async def _collect(ev: object) -> None:
        if isinstance(ev, Text):
            parts.append(ev.content)

    try:
        await asyncio.wait_for(
            agent.run_turn(
                TurnRequest(
                    origin=Origin.USER,
                    source=Source(
                        channel=CHANNEL_NAME, chat_id=task.task_id,
                        sender_id="benchmark_user", chat_type=ChatType.DM,
                    ),
                    text=task.prompt,
                    conversation=session_key,
                ),
                _collect,
                lambda: [],
                stream=False,
            ),
            timeout=timeout_seconds,
        )
        response_content = "".join(parts)

    except asyncio.TimeoutError:
        timed_out = True
        status = "timeout"
        logger.warning("Task %s timed out after %.0fs", task.task_id, timeout_seconds)
    except Exception as exc:
        status = "error"
        logger.error("Task %s failed: %s", task.task_id, exc, exc_info=True)
    finally:
        # Stop the agent loop and clean up
        agent.stop()
        try:
            await agent.close_mcp()
        except Exception:
            pass

    execution_time = time.time() - start_time

    # Extract transcript from session
    session = session_mgr.get_or_create(session_key)
    raw_messages = list(session.messages)
    transcript = _session_to_openclaw_transcript(raw_messages)

    if verbose:
        logger.info("  Response: %s", (response_content[:500] + "...") if len(response_content) > 500 else response_content)
        logger.info("  Transcript entries: %d", len(transcript))
        logger.info("  Execution time: %.2fs", execution_time)
        if task_workspace.exists():
            logger.info("  Workspace files:")
            for f in sorted(task_workspace.rglob("*")):
                if f.is_file():
                    logger.info("    %s (%d bytes)", f.relative_to(task_workspace), f.stat().st_size)

    return {
        "task_id": task.task_id,
        "status": status,
        "transcript": transcript,
        "workspace": str(task_workspace),
        "execution_time": execution_time,
        "timed_out": timed_out,
        "response": response_content,
        "raw_messages": raw_messages,
    }
