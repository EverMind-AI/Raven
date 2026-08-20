"""OpenClaw CLI backend — thinnest implementation.

Delegates to the existing ``run_openclaw_one_shot`` helper. Every
benchmark's prompt goes through here as a text string; the driver parses
the text into a decision.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from ..agents import get_agent_config
from ..backend import AgentBackend, AgentOutcome, Sample
from ..config import get_config
from ..openclaw import build_openclaw_config, run_openclaw_one_shot


class OpenClawBackend(AgentBackend):
    name = "openclaw"

    def __init__(self, overrides: dict[str, Any] | None = None):
        overrides = overrides or {}
        agent_cfg = get_agent_config("openclaw")
        global_cfg = get_config()
        self.thinking = overrides.get("thinking") or agent_cfg.get("thinking") or "medium"
        self.cli_timeout_s = int(overrides.get("cli_timeout_s") or agent_cfg.get("cli_timeout_s") or 300)
        self.subprocess_timeout_s = int(
            overrides.get("subprocess_timeout_s") or agent_cfg.get("subprocess_timeout_s") or 360
        )
        self.openclaw_cmd = overrides.get("openclaw_cmd") or agent_cfg.get("openclaw_cmd") or global_cfg.openclaw_cmd
        self.vllm_base_url = overrides.get("vllm_base_url") or agent_cfg.get("vllm_base_url")
        self.vllm_model_id = overrides.get("vllm_model_id") or agent_cfg.get("vllm_model_id")
        self.with_memory = bool(overrides.get("with_memory", False))
        # When set, run openclaw inside this docker image instead of invoking
        # openclaw_cmd on PATH. The per-sample tmp home gets bind-mounted to
        # /home/node/.openclaw inside the container.
        self.docker_image = overrides.get("docker_image") or agent_cfg.get("docker_image")

    async def run_one(
        self, sample: Sample, driver, *, session_id: str, ctx: dict[str, Any] | None = None
    ) -> AgentOutcome:
        prompt = driver.build_prompt(sample, ctx)

        # Pre-allocate the per-sample home so workspace files can live under
        # the same dir that gets bind-mounted into the container (docker path)
        # or that run_openclaw_one_shot otherwise uses (native path).
        host_home = Path(tempfile.mkdtemp(prefix=f"openclaw-{session_id[:12]}-"))

        # Co-locate workspace under .openclaw so a single bind-mount covers both.
        # In docker mode this is mandatory (the default tempfile.mkdtemp inside
        # build_openclaw_config would return a host-only /var/folders path that
        # doesn't exist in the container). "with-memory" mode additionally plants
        # MEMORY.md / HISTORY.md and un-caps bootstrap so OpenClaw pulls them
        # into the system prompt.
        ws_host = host_home / ".openclaw" / "workspace"
        ws_host.mkdir(parents=True, exist_ok=True)
        workspace_arg: str | None = "/home/node/.openclaw/workspace" if self.docker_image else str(ws_host)
        bootstrap_max = 1
        planted_paths: list[str] = []
        if self.with_memory and hasattr(driver, "workspace_files"):
            try:
                for rel, content in (driver.workspace_files(sample) or {}).items():
                    p = ws_host / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content, encoding="utf-8")
                    planted_paths.append(rel)
                if planted_paths:
                    bootstrap_max = 20000
            except Exception:
                pass

        config = build_openclaw_config(
            model_id=self.vllm_model_id,
            base_url=self.vllm_base_url,
            workspace=workspace_arg,
            bootstrap_max_chars=bootstrap_max,
        )
        # run_openclaw_one_shot is blocking subprocess.run — offload so we
        # don't block the event loop (matters when run.py gains parallelism).
        outcome = await asyncio.to_thread(
            run_openclaw_one_shot,
            prompt,
            session_id=session_id,
            thinking=self.thinking,
            cli_timeout_s=self.cli_timeout_s,
            subprocess_timeout_s=self.subprocess_timeout_s,
            config=config,
            openclaw_cmd=self.openclaw_cmd,
            docker_image=self.docker_image,
            home=host_home,
        )

        return AgentOutcome(
            status=outcome["status"],
            elapsed_s=outcome["elapsed_s"],
            text=outcome.get("text"),
            error=outcome.get("error"),
            decision=None,  # driver parses the text
            meta={
                "stdout_tail": outcome.get("stdout_tail", "")[-500:],
                "stderr_tail": outcome.get("stderr_tail", "")[-500:],
                "thinking": self.thinking,
                "with_memory": self.with_memory,
                "memory_planted": planted_paths,
                "docker_image": self.docker_image,
            },
        )


__all__ = ["OpenClawBackend"]
