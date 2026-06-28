"""Raven backends.

Phase 4b reduced this from three in-process backends (Planner / Agent /
Sentinel) to **only ``RavenAgentBackend``**, rewritten to drive the
agent via the Phase 3 subprocess driver. The other two modes raise
``NotImplementedError`` with a pointer to MIGRATION_STATUS.md.

Why agent-only:

- Pbench ``--mode agent`` is the **F1=0.382 datapoint** — the strongest
  evidence for "tool-loop architecture beats single-prompt cron" in the
  baseline FINDINGS-summary.
- ``--mode planner`` (F1≈0.135) and ``--mode sentinel`` (F1≈0.135) would
  require either an in-process Sentinel pipeline (defeats the subprocess
  contract) or a new ``raven planner decide --json`` CLI surface.
  Both are deferred until a concrete need appears.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

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
        self.max_iterations = int(
            overrides.get("max_iterations")
            or agent_cfg.get("max_iterations") or 10
        )
        self.agent_timeout_s = int(
            overrides.get("agent_timeout_s")
            or agent_cfg.get("agent_timeout_s") or 180
        )
        self._raven_repo = _resolve_raven_repo()
        # Model is captured only for the ``meta`` field on the outcome —
        # the subprocess uses whatever model raven is configured for.
        self._model = overrides.get("model") or agent_cfg.get("model") or "subprocess"

    async def run_one(
        self, sample: Sample, driver, *,
        session_id: str, ctx: dict[str, Any] | None = None,
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
                status="timeout", elapsed_s=elapsed,
                error=f"timeout after {self.agent_timeout_s}s",
                meta={"model": self._model},
            )
        if not response.ok:
            return AgentOutcome(
                status="exception", elapsed_s=elapsed,
                text=response.stdout.strip() or None,
                error=f"rc={response.returncode}: {response.stderr[:400].strip()}",
                meta={"model": self._model},
            )
        return AgentOutcome(
            status="ok", elapsed_s=elapsed,
            text=response.stdout.strip() or None,
            error=None,
            meta={"model": self._model},
        )


class _DeferredRavenBackend(AgentBackend):
    """Stub for the Planner / Sentinel modes — not ported in Phase 4b."""

    name = "raven"

    def __init__(self, mode: str, overrides: dict[str, Any] | None = None):
        self._mode = mode

    async def run_one(
        self, sample: Sample, driver, *,
        session_id: str, ctx: dict[str, Any] | None = None,
    ) -> AgentOutcome:
        return AgentOutcome(
            status="exception", elapsed_s=0.0,
            error=(
                f"raven --mode {self._mode} is not available in the "
                "subprocess-driven port. Phase 4b only ported --mode agent "
                "(the F1=0.382 pbench datapoint). Use --mode agent, or run "
                "the original in-process eval against the pre-refactor "
                "raven checkout for Planner/Sentinel-only numbers."
            ),
        )


def make_raven_backend(
    mode: str, overrides: dict[str, Any] | None = None,
) -> AgentBackend:
    mode = (mode or "agent").lower()
    if mode == "agent":
        return RavenAgentBackend(overrides=overrides)
    if mode in ("planner", "sentinel"):
        return _DeferredRavenBackend(mode, overrides=overrides)
    raise ValueError(f"Unknown raven mode '{mode}'. Use planner | agent | sentinel.")


__all__ = ["RavenAgentBackend", "make_raven_backend"]
