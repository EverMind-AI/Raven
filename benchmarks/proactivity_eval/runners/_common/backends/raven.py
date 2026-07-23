"""Raven backends.

Two live modes:

- ``--mode agent`` (``RavenAgentBackend``): Phase 4b subprocess port —
  one ``raven agent --message ...`` subprocess per sample. The
  **F1=0.382 datapoint** in the baseline FINDINGS-summary.
- ``--mode sentinel`` (``RavenSentinelBackend``): in-process
  ``ProactivePlanner.decide()`` over ``driver.to_planner_context(sample)``
  — the native L3 decision channel (historical F1≈0.135). Restored for
  prompt-vs-architecture ablations; no prompt template is consumed.

``--mode planner`` remains a stub (would need a ``raven planner decide
--json`` CLI surface).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

from ..agents import get_agent_config
from ..backend import AgentBackend, AgentOutcome, Sample


def _resolve_raven_repo() -> Path:
    """Locate the raven checkout (in-repo eval lives inside it).

    Layout: backends → _common → runners → proactivity_eval → benchmarks
    → <repo root>. RAVEN_REPO env var overrides if set.
    """
    env = os.environ.get("RAVEN_REPO")
    if env:
        return Path(env).expanduser().resolve()
    candidate = Path(__file__).resolve().parents[5]
    if (candidate / "raven" / "__main__.py").exists():
        return candidate
    raise FileNotFoundError(
        f"Could not locate the raven checkout at {candidate}. "
        "Set RAVEN_REPO=<path> to the dir containing raven/__main__.py."
    )


class RavenAgentBackend(AgentBackend):
    """Prompt-based backend: drives one ``raven agent --message ...``
    subprocess per pbench sample.

    Each sample gets its own tempdir workspace. If the benchmark driver
    exposes ``workspace_files(sample)`` or the sample carries
    ``memory_md`` / ``history_md_recent``, those files seed the
    workspace before the agent runs so tool-using reasoning can
    grep / read them.
    """

    name = "raven"

    def __init__(self, overrides: dict[str, Any] | None = None):
        overrides = overrides or {}
        agent_cfg = get_agent_config("raven")
        self.max_iterations = int(overrides.get("max_iterations") or agent_cfg.get("max_iterations") or 10)
        self.agent_timeout_s = int(overrides.get("agent_timeout_s") or agent_cfg.get("agent_timeout_s") or 180)
        self._raven_repo = _resolve_raven_repo()
        raven_config = overrides.get("raven_config") or agent_cfg.get("raven_config")
        self._raven_config = Path(raven_config).expanduser().resolve() if raven_config else None
        # Model is captured only for the ``meta`` field on the outcome —
        # the subprocess uses whatever model raven is configured for.
        self._model = overrides.get("model") or agent_cfg.get("model") or "subprocess"

    async def run_one(
        self,
        sample: Sample,
        driver,
        *,
        session_id: str,
        ctx: dict[str, Any] | None = None,
    ) -> AgentOutcome:
        from ..raven_driver import RavenDriver

        prompt = driver.build_prompt(sample, ctx)
        workspace = Path(tempfile.mkdtemp(prefix=f"ec-agent-{session_id[:16]}-"))
        memory_dir = workspace / "memory"
        memory_dir.mkdir(exist_ok=True)

        # Seed workspace files. The raven refactor's MemoryStore reads
        # from ``<workspace>/memory/`` so seed there (the legacy layout
        # used the workspace root — both supported via fallthrough).
        plant = getattr(driver, "workspace_files", None)
        if plant is not None:
            try:
                for fname, content in (plant(sample) or {}).items():
                    target = memory_dir / fname if fname.endswith(".md") else workspace / fname
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
            except Exception:
                pass
        else:
            raw = sample.raw
            if isinstance(raw.get("memory_md"), str) and raw["memory_md"]:
                (memory_dir / "MEMORY.md").write_text(raw["memory_md"], encoding="utf-8")
            if isinstance(raw.get("history_md_recent"), str) and raw["history_md_recent"]:
                (memory_dir / "HISTORY.md").write_text(raw["history_md_recent"], encoding="utf-8")

        raven_driver = RavenDriver(
            raven_repo=self._raven_repo,
            workspace=workspace,
            config=self._raven_config,
            timeout_seconds=float(self.agent_timeout_s),
        )

        started = time.monotonic()
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: raven_driver.send_message(prompt, session_id=session_id),
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

        elapsed = round(time.monotonic() - started, 2)
        if response.returncode == -1 and "timed out" in response.stderr:
            return AgentOutcome(
                status="timeout",
                elapsed_s=elapsed,
                error=f"timeout after {self.agent_timeout_s}s",
                meta={"model": self._model},
            )
        if not response.ok:
            return AgentOutcome(
                status="exception",
                elapsed_s=elapsed,
                text=response.stdout.strip() or None,
                error=f"rc={response.returncode}: {response.stderr[:400].strip()}",
                meta={"model": self._model},
            )
        return AgentOutcome(
            status="ok",
            elapsed_s=elapsed,
            text=response.stdout.strip() or None,
            error=None,
            meta={"model": self._model},
        )


class RavenSentinelBackend(AgentBackend):
    """Structured backend: drives ``ProactivePlanner.decide()`` in-process.

    The driver supplies a ``PlannerContext`` via ``to_planner_context(sample)``;
    the Planner makes one LLM call and returns a skip/nudge/spawn decision.
    No prompt template is consumed — the Planner uses its own internal
    system prompt, so ``--prompts-dir`` has no effect in this mode.

    In-process ``raven`` import mirrors the driver's own structured hook
    (``drivers/pbench.py::to_planner_context`` already imports raven types);
    the subprocess contract applies to ``raven_driver``, not this backend.
    """

    name = "raven"

    def __init__(self, overrides: dict[str, Any] | None = None):
        overrides = overrides or {}
        agent_cfg = get_agent_config("raven")
        raven_config = overrides.get("raven_config") or agent_cfg.get("raven_config")
        config_path = (
            Path(raven_config).expanduser().resolve() if raven_config else Path.home() / ".raven" / "config.json"
        )
        if not raven_config:
            logger.warning(
                "RavenSentinelBackend: no --raven-config given — falling back to "
                "the LIVE {} (results depend on this machine's config; pass "
                "--raven-config for reproducible runs)",
                config_path,
            )
        import json

        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        defaults = cfg.get("agents", {}).get("defaults", {})
        providers_cfg = cfg.get("providers", {})
        model = overrides.get("model") or defaults.get("model") or ""

        if model.startswith("openrouter/"):
            api_base = "https://openrouter.ai/api/v1"
            api_key = providers_cfg.get("openrouter", {}).get("apiKey") or "no-key"
            self._model = model[len("openrouter/") :]
        elif defaults.get("provider") == "custom" or model.startswith("custom/"):
            custom = providers_cfg.get("custom", {})
            api_base = custom.get("apiBase") or "http://localhost:8000/v1"
            api_key = custom.get("apiKey") or "no-key"
            self._model = model.removeprefix("custom/")
        else:
            raise ValueError(
                f"RavenSentinelBackend cannot resolve an OpenAI-compatible "
                f"endpoint for model {model!r} in {config_path}. Supported: "
                "openrouter/<model>, or provider 'custom' with apiBase set."
            )

        from raven.proactive_engine.sentinel.planner import ProactivePlanner
        from raven.providers.custom_provider import CustomProvider

        provider = CustomProvider(api_key=api_key, api_base=api_base, default_model=self._model)
        self._planner = ProactivePlanner(provider, self._model)

    async def run_one(
        self,
        sample: Sample,
        driver,
        *,
        session_id: str,
        ctx: dict[str, Any] | None = None,
    ) -> AgentOutcome:
        to_ctx = getattr(driver, "to_planner_context", None)
        if to_ctx is None:
            return AgentOutcome(
                status="exception",
                elapsed_s=0.0,
                error=f"driver '{driver.name}' does not support Sentinel (missing to_planner_context)",
            )
        planner_ctx = to_ctx(sample)
        started = time.monotonic()
        decision = await self._planner.decide(planner_ctx)
        elapsed = round(time.monotonic() - started, 2)

        reason = decision.reason or ""
        if reason.startswith("llm_error"):
            return AgentOutcome(
                status="exception",
                elapsed_s=elapsed,
                error=reason,
                meta={"model": self._model},
            )
        # "model did not call planner_decision tool" is the structured-mode
        # analogue of a parse failure: keep the row, flag parse_ok=False.
        parse_ok = reason != "model did not call planner_decision tool"
        should_help = decision.action in ("nudge", "nudge_inject", "nudge_defer", "spawn_agent")
        return AgentOutcome(
            status="ok",
            elapsed_s=elapsed,
            text=None,
            decision={
                "parse_ok": parse_ok,
                "should_help": should_help,
                "proposed_task": decision.spawn_task or decision.nudge_message,
                "reason": reason,
                "sentinel_action": decision.action,
                "sentinel_route": "planner_direct",
            },
            meta={"model": self._model, "proactivity_score": decision.proactivity_score},
        )


class _DeferredRavenBackend(AgentBackend):
    """Stub for the Planner mode — not ported in Phase 4b."""

    name = "raven"

    def __init__(self, mode: str, overrides: dict[str, Any] | None = None):
        self._mode = mode

    async def run_one(
        self,
        sample: Sample,
        driver,
        *,
        session_id: str,
        ctx: dict[str, Any] | None = None,
    ) -> AgentOutcome:
        return AgentOutcome(
            status="exception",
            elapsed_s=0.0,
            error=(
                f"raven --mode {self._mode} is not available in the "
                "subprocess-driven port. Use --mode agent (Phase 4b port) "
                "or --mode sentinel (in-process ProactivePlanner)."
            ),
        )


def make_raven_backend(
    mode: str,
    overrides: dict[str, Any] | None = None,
) -> AgentBackend:
    mode = (mode or "agent").lower()
    if mode == "agent":
        return RavenAgentBackend(overrides=overrides)
    if mode == "sentinel":
        return RavenSentinelBackend(overrides=overrides)
    if mode == "planner":
        return _DeferredRavenBackend(mode, overrides=overrides)
    raise ValueError(f"Unknown raven mode '{mode}'. Use planner | agent | sentinel.")


__all__ = ["RavenAgentBackend", "make_raven_backend"]
