"""
Nanobot Agent Executor — replaces OpenClaw CLI subprocess calls.

Drives Raven's AgentLoop.run_turn() programmatically to execute
benchmark tasks, capturing the full session transcript for grading.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

from task_loader import Task

logger = logging.getLogger(__name__)

# Default OpenAI-compatible benchmark config.
DEFAULT_API_KEY = (
    os.environ.get("OPENROUTER_API_KEY")
    or os.environ.get("DEEPSEEK_API_KEY")
    or os.environ.get("OPENAI_API_KEY")
    or ""
)
DEFAULT_API_BASE = (
    os.environ.get("OPENROUTER_API_BASE")
    or os.environ.get("DEEPSEEK_API_BASE")
    or os.environ.get("OPENAI_BASE_URL")
    or ""
)
DEFAULT_PROVIDER = os.environ.get("RAVEN_BENCH_PROVIDER", "custom")
DEFAULT_MODEL = os.environ.get("RAVEN_BENCH_MODEL", "deepseek-v4-flash")

async def _fetch_openrouter_model_ids(api_key: str) -> set[str]:
    """Fetch the set of valid model IDs from OpenRouter's /models endpoint."""
    import httpx
    url = "https://openrouter.ai/api/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
            ids = {m["id"] for m in data.get("data", []) if "id" in m}
            logger.info("OpenRouter: %d available models fetched", len(ids))
            return ids
    except Exception as e:
        logger.warning("Could not fetch OpenRouter model list: %s", e)
        return set()


# ---------------------------------------------------------------------------
# Standard-only ModelRouter (benchmark-only; filters non-standard model IDs)
# ---------------------------------------------------------------------------

# Legitimate OpenRouter providers use lowercase names (e.g. "anthropic", "z-ai",
# "minimax").  User-namespace submissions have mixed-case or underscore providers
# (e.g. "Jobeous_II", "JoePro").  Reject anything that doesn't match.
_STANDARD_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9\-\.]*$")


def _is_standard_model_id(model_id: str) -> bool:
    """Return True only for 'provider/model' IDs with a lowercase provider name."""
    parts = model_id.split("/")
    if len(parts) != 2:
        return False
    return bool(_STANDARD_PROVIDER_RE.match(parts[0]))


class _StandardModelRouter:
    """ModelRouter wrapper that restricts selection to models available on OpenRouter.

    PinchBench includes user-tagged submissions (e.g. "Jobeous_II/…") that are
    not publicly accessible. This wrapper fetches the live OpenRouter model
    list and removes any benchmark entries that don't appear in it.

    Even when the live fetch succeeds, provider-name validation is applied as
    a second gate to catch user-namespace models that happen to be listed on
    OpenRouter but are effectively private/inaccessible.
    """

    def __init__(self, api_key: str, profile: str, fallback_model: str):
        from raven.routing.router import ModelRouter
        self._inner = ModelRouter(api_key=api_key, profile=profile, fallback_model=fallback_model)
        self._api_key = api_key
        self._fallback_model = fallback_model
        self._valid_ids: set[str] = set()

    async def initialize(self) -> None:
        # Fetch valid OpenRouter model IDs and initialize benchmark data in parallel
        self._valid_ids, _ = await asyncio.gather(
            _fetch_openrouter_model_ids(self._api_key),
            self._inner.initialize(),
        )
        # Filter benchmark data to models actually available on OpenRouter.
        # Always filter when we have benchmark data; if _valid_ids is empty
        # (fetch failed), log a warning but still remove non-standard models
        # by requiring the "provider/model" format without user-prefixes.
        if self._inner._data:
            before = len(self._inner._data)
            if self._valid_ids:
                # Keep models that are both in OpenRouter's live list AND have a
                # standard lowercase provider name (double-gate against user namespaces).
                self._inner._data = {
                    k: v for k, v in self._inner._data.items()
                    if k in self._valid_ids and _is_standard_model_id(k)
                }
            else:
                # Fallback when live fetch failed: require lowercase provider name.
                # This rejects "Jobeous_II/…", "JoePro/…" etc. even though they
                # have exactly one "/" segment.
                logger.warning(
                    "OpenRouter model list unavailable; filtering benchmark data "
                    "to standard lowercase-provider 'provider/model' entries only"
                )
                self._inner._data = {
                    k: v for k, v in self._inner._data.items()
                    if _is_standard_model_id(k)
                }
            removed = before - len(self._inner._data)
            logger.info(
                "Filtered %d unavailable model(s) from benchmark data "
                "(%d OpenRouter-accessible models remain)",
                removed, len(self._inner._data),
            )

    async def select_model_id(self, prompt: str) -> str | None:
        return await self._inner.select_model_id(prompt)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Usage-tracking provider wrapper (benchmark-only; does not touch loop.py)
# ---------------------------------------------------------------------------

class _UsageTrackingProvider:
    """Wraps any LLMProvider to accumulate token usage and track model calls.

    Intercepts chat_with_retry() so every LLM call in the agent loop is
    recorded per-model, enabling accurate cost estimation across mixed-model
    routing sessions.  Delegates everything else transparently to the real provider.
    """

    def __init__(self, inner):
        self._inner = inner
        self.accumulated: Dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self.model_calls: List[str] = []   # ordered list of models called
        # per-model token breakdown: model -> {prompt_tokens, completion_tokens}
        self.per_model_usage: Dict[str, Dict[str, int]] = {}

    def reset(self) -> None:
        self.accumulated = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.model_calls = []
        self.per_model_usage = {}

    # --- intercept the one method that matters ---

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs):
        response = await self._inner.chat_with_retry(messages, tools=tools, model=model, **kwargs)
        # accumulate total usage
        for k in self.accumulated:
            self.accumulated[k] += response.usage.get(k, 0)
        # record which model was actually called
        effective = model or self._inner.get_default_model()
        self.model_calls.append(effective)
        # accumulate per-model usage
        if effective not in self.per_model_usage:
            self.per_model_usage[effective] = {"prompt_tokens": 0, "completion_tokens": 0}
        self.per_model_usage[effective]["prompt_tokens"] += response.usage.get("prompt_tokens", 0)
        self.per_model_usage[effective]["completion_tokens"] += response.usage.get("completion_tokens", 0)
        return response

    # --- transparent delegation ---

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------

def prepare_workspace(
    task: Task, workspace: Path, assets_dir: Path
) -> Path:
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

    Nanobot format:
        {"role": "user", "content": "..."}
        {"role": "assistant", "content": "...", "tool_calls": [...]}
        {"role": "tool", "tool_call_id": "...", "name": "...", "content": "..."}

    OpenClaw/PinchBench format:
        {"type": "message", "message": {"role": "user", "content": [...]}}
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "..."},
            {"type": "toolCall", "name": "...", "arguments": {...}}
        ]}}
        {"type": "message", "message": {"role": "toolResult", "content": [...]}}
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
                    import json
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


def _make_benchmark_provider(model: str, api_key: str, api_base: str, provider_name: str):
    """Create the benchmark LLM provider."""
    from raven.providers.base import GenerationSettings
    from raven.providers.custom_provider import CustomProvider
    from raven.providers.litellm_provider import LiteLLMProvider

    if provider_name == "custom":
        provider = CustomProvider(
            api_key=api_key,
            api_base=api_base,
            default_model=model,
        )
    else:
        provider = LiteLLMProvider(
            api_key=api_key,
            api_base=api_base or ("https://openrouter.ai/api/v1" if provider_name == "openrouter" else None),
            default_model=model,
            provider_name=provider_name,
        )
    provider.generation = GenerationSettings(
        temperature=0.7,
        max_tokens=8192,
    )
    return provider


def _estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Estimate USD cost using LiteLLM's pricing database with manual fallbacks.

    Falls back to _fallback_pricing for models not yet in LiteLLM's DB.
    Returns None if the model is unknown to both.
    """
    # Manual fallback pricing ($/token) for models absent from LiteLLM's DB.
    # Source: OpenRouter model pages (as of 2026-03).
    _fallback_pricing: Dict[str, tuple[float, float]] = {
        "z-ai/glm-4.5-air": (0.13e-6, 0.85e-6),   # $0.13/$0.85 per 1M tokens
    }

    try:
        import litellm
        # LiteLLM expects "openrouter/<provider>/<model>" format for OpenRouter models.
        or_model = f"openrouter/{model}" if not model.startswith("openrouter/") else model
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=or_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return prompt_cost + completion_cost
    except Exception:
        pass

    # Fallback for models not in LiteLLM DB
    base_model = model.removeprefix("openrouter/")
    if base_model in _fallback_pricing:
        p_per_tok, c_per_tok = _fallback_pricing[base_model]
        return p_per_tok * prompt_tokens + c_per_tok * completion_tokens

    return None


async def _run_turn_text(agent, message: str, *, session_key: str, chat_id: str) -> str:
    """Run one USER turn through the spine ``run_turn`` and return the reply text
    (non-streaming → the reply arrives as Text events, which we accumulate)."""
    from raven.spine import ChatType, Origin, Source, Text, TurnRequest

    parts: list[str] = []

    async def _collect(ev: object) -> None:
        if isinstance(ev, Text):
            parts.append(ev.content)

    await agent.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(
                channel="benchmark", chat_id=chat_id, sender_id="user", chat_type=ChatType.DM
            ),
            text=message,
            conversation=session_key,
        ),
        _collect,
        lambda: [],
        stream=False,
    )
    return "".join(parts)


async def execute_task(
    task: Task,
    workspace: Path,
    assets_dir: Path,
    model: str = DEFAULT_MODEL,
    api_key: str = DEFAULT_API_KEY,
    api_base: str = DEFAULT_API_BASE,
    provider_name: str = DEFAULT_PROVIDER,
    timeout_multiplier: float = 1.0,
    verbose: bool = False,
    routing_profile: str | None = None,
) -> Dict[str, Any]:
    """
    Execute a single benchmark task using Raven's AgentLoop.

    When ``routing_profile`` is given (e.g. "eco"), a ModelRouter is
    attached to the AgentLoop so each turn selects the best model for
    that specific prompt.  Token usage and estimated cost are recorded
    via a lightweight provider wrapper — no changes to loop.py required.

    Returns a result dict compatible with PinchBench grading:
        task_id, status, transcript, workspace, execution_time, timed_out,
        usage, cost_usd, models_used
    """
    from raven.agent.loop import AgentLoop
    from raven.config.schema import ExecToolConfig
    from raven.session.manager import SessionManager

    # Prepare workspace
    task_workspace = prepare_workspace(task, workspace, assets_dir)

    # Create provider wrapped for usage tracking
    raw_provider = _make_benchmark_provider(model, api_key, api_base, provider_name)
    tracked_provider = _UsageTrackingProvider(raw_provider)

    # Optionally attach EcoClaw-style router (standard OpenRouter models only)
    router = None
    if routing_profile:
        if provider_name != "openrouter":
            raise ValueError("--routing-profile requires --provider openrouter and an OpenRouter API key")
        router = _StandardModelRouter(api_key=api_key, profile=routing_profile, fallback_model=model)
        await router.initialize()
        logger.info("ModelRouter enabled with profile=%s", routing_profile)

    session_mgr = SessionManager(task_workspace)
    session_key = f"bench:{task.task_id}"

    # Load skill_forge config so injection_mode / inject_max / mass_library_db
    # etc. are honored under benchmark runs. Without this AgentLoop receives
    # ``skill_forge_config=None`` → SkillService falls back to dataclass
    # defaults (e.g. injection_mode="summary"), regardless of user config.
    from raven.config.raven import load_raven_config
    _ec_cfg = load_raven_config()
    skill_forge_cfg = getattr(_ec_cfg, "skill_forge", None)

    agent = AgentLoop(
        provider=tracked_provider,   # wrapped provider — transparent to AgentLoop
        workspace=task_workspace,
        model=model,
        max_iterations=40,
        context_window_tokens=65_536,
        exec_config=ExecToolConfig(),
        restrict_to_workspace=True,  # sandbox for benchmark safety
        session_manager=session_mgr,
        router=router,
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
    response = ""

    logger.info(
        "Executing task %s (%s) — timeout %.0fs%s",
        task.task_id, task.name, timeout_seconds,
        f" [routing={routing_profile}]" if routing_profile else "",
    )

    try:
        response = await asyncio.wait_for(
            _run_turn_text(
                agent,
                task.prompt,
                session_key=session_key,
                chat_id=task.task_id,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        timed_out = True
        status = "timeout"
        logger.warning("Task %s timed out after %.0fs", task.task_id, timeout_seconds)
    except Exception as exc:
        status = "error"
        logger.error("Task %s failed: %s", task.task_id, exc, exc_info=True)
    finally:
        try:
            await agent.close_mcp()
        except Exception:
            pass

    execution_time = time.time() - start_time

    # Collect usage & cost from the tracking wrapper
    usage = dict(tracked_provider.accumulated)
    models_used = list(tracked_provider.model_calls)
    # Sum cost across all models using their actual per-model token counts
    total_cost: float | None = None
    for m, m_usage in tracked_provider.per_model_usage.items():
        partial = _estimate_cost_usd(
            m,
            m_usage.get("prompt_tokens", 0),
            m_usage.get("completion_tokens", 0),
        )
        if partial is not None:
            total_cost = (total_cost or 0.0) + partial
    cost_usd = total_cost

    # Extract transcript from session
    session = session_mgr.get_or_create(session_key)
    raw_messages = list(session.messages)
    transcript = _session_to_openclaw_transcript(raw_messages)

    if verbose:
        logger.info("  Response: %s", (response[:500] + "...") if len(response) > 500 else response)
        logger.info("  Transcript entries: %d", len(transcript))
        logger.info("  Execution time: %.2fs", execution_time)
        logger.info("  Tokens: prompt=%d  completion=%d  total=%d",
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                    usage.get("total_tokens", 0))
        if cost_usd is not None:
            logger.info("  Estimated cost: $%.6f", cost_usd)
        logger.info("  Models called: %s", models_used)
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
        "response": response,
        "raw_messages": raw_messages,
        # cost fields
        "usage": usage,
        "cost_usd": cost_usd,
        "models_used": models_used,
    }
