"""Hermes subprocess backend.

Drives Hermes's ``cron.scheduler.run_job()`` via a per-sample subprocess
with a clean HERMES_HOME and ``hermes_time.now`` patched. The cron spec
is supplied by the driver:

- CasesDriver.to_hermes_cron(sample) → prescribed cron (or None → skip)
- PbenchDriver.to_hermes_cron(sample) → synthetic cron wrapping the obs
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..agents import get_agent_config
from ..backend import AgentBackend, AgentOutcome, Sample
from ..config import get_config
from ..hermes_home import load_config_from_hermes_home
from ..proxy import bypass_proxy_for_url, strip_proxy_env_vars

_INNER_MODULE = Path(__file__).resolve().parent / "hermes_inner.py"


def _find_hermes_python(hermes_src: Path) -> str:
    for cand in (
        hermes_src / "venv" / "bin" / "python3",
        hermes_src / "venv" / "bin" / "python",
        hermes_src / ".venv" / "bin" / "python3",
        hermes_src / ".venv" / "bin" / "python",
    ):
        if cand.exists():
            return str(cand)
    return sys.executable


class HermesBackend(AgentBackend):
    name = "hermes"

    def __init__(self, overrides: dict[str, Any] | None = None):
        overrides = overrides or {}
        agent_cfg = get_agent_config("hermes")
        global_cfg = get_config()

        hermes_src_raw = (
            overrides.get("hermes_src")
            or agent_cfg.get("hermes_src")
            or (str(global_cfg.hermes_src) if global_cfg.hermes_src else None)
        )
        if not hermes_src_raw:
            raise RuntimeError(
                "Hermes source tree not configured. Set systems.hermes_src in "
                "runners.config.yaml, agents/hermes/hermes.yaml::hermes_src, "
                "$HERMES_AGENT_SRC, or pass --hermes-src."
            )
        self.hermes_src = Path(hermes_src_raw).resolve()
        if not (self.hermes_src / "cron" / "scheduler.py").exists():
            raise RuntimeError(f"{self.hermes_src} is not a hermes-agent checkout (missing cron/scheduler.py)")

        self.inherit_home = bool(overrides.get("inherit_home", agent_cfg.get("inherit_home", True)))
        self.subprocess_timeout_s = int(
            overrides.get("subprocess_timeout_s") or agent_cfg.get("subprocess_timeout_s") or 300
        )
        self.with_memory = bool(overrides.get("with_memory", False))
        self.python_exe = _find_hermes_python(self.hermes_src)

        # One-shot: bypass proxy for the Hermes-configured LAN vLLM host.
        hermes_model_cfg = load_config_from_hermes_home().get("model") or {}
        if isinstance(hermes_model_cfg, dict) and hermes_model_cfg.get("base_url"):
            bypass_proxy_for_url(hermes_model_cfg["base_url"])

    async def run_one(
        self, sample: Sample, driver, *, session_id: str, ctx: dict[str, Any] | None = None
    ) -> AgentOutcome:
        to_cron = getattr(driver, "to_hermes_cron", None)
        if to_cron is None:
            return AgentOutcome(
                status="exception",
                elapsed_s=0.0,
                error=f"driver '{driver.name}' does not support Hermes (missing to_hermes_cron)",
            )

        # Propagate with_memory into ctx so driver.to_hermes_cron can inject.
        effective_ctx = dict(ctx or {})
        if self.with_memory:
            effective_ctx["with_memory"] = True
        cron_spec = to_cron(sample, effective_ctx)
        if cron_spec is None:
            # Driver said "no prescription" (cases with plausible=false).
            return AgentOutcome(
                status="skip",
                elapsed_s=0.0,
                text=None,
                error=None,
                meta={"reason": "no_prescription"},
            )

        fake_now = cron_spec.get("fake_now") or (ctx.get("fake_now") if ctx else None)
        if not fake_now:
            return AgentOutcome(
                status="exception",
                elapsed_s=0.0,
                error="cron spec missing fake_now ISO",
            )

        hermes_home = Path(tempfile.mkdtemp(prefix=f"hermes-{session_id[:16]}-"))
        if self.inherit_home:
            # Honor HERMES_HOME_OVERRIDE for model-tier ablation runs
            # (a parallel ~/.hermes-<tag>/ with alternate config.yaml).
            override = os.environ.get("HERMES_HOME_OVERRIDE")
            real = Path(override).expanduser() if override else Path.home() / ".hermes"
            for fn in ("config.yaml", ".env", "auth.json"):
                src = real / fn
                if src.exists():
                    shutil.copy2(src, hermes_home / fn)

        env = strip_proxy_env_vars()
        env.update(
            {
                "HERMES_HOME": str(hermes_home),
                "HERMES_AGENT_SRC": str(self.hermes_src),
                "HERMES_EVAL_FAKE_NOW": fake_now,
                "HERMES_EVAL_CRON_JOB": json.dumps(cron_spec, ensure_ascii=False),
                "PYTHONPATH": f"{self.hermes_src}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            }
        )
        cmd = [self.python_exe, str(_INNER_MODULE)]
        started = time.monotonic()
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.subprocess_timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = round(time.monotonic() - started, 2)
            if not os.environ.get("HERMES_KEEP_HOME"):
                shutil.rmtree(hermes_home, ignore_errors=True)
            return AgentOutcome(
                status="timeout",
                elapsed_s=elapsed,
                error=f"subprocess timeout after {self.subprocess_timeout_s}s",
                meta={
                    "stdout_tail": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
                    "stderr_tail": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
                },
            )
        finally:
            if not os.environ.get("HERMES_KEEP_HOME"):
                shutil.rmtree(hermes_home, ignore_errors=True)
            else:
                import sys as _sys

                print(f"HERMES_HOME_KEPT: {hermes_home}", file=_sys.stderr, flush=True)

        elapsed = round(time.monotonic() - started, 2)

        # Diagnostic: when HERMES_DEBUG_TOOLS is set, dump full
        # subprocess stdout/stderr to parent stderr so any tool-use
        # traces become visible in the parent log.
        if os.environ.get("HERMES_DEBUG_TOOLS") and "proc" in dir():
            import sys as _sys

            print(
                f"\n========== HERMES SUBPROCESS STDOUT ({len(proc.stdout)} chars) ==========",
                file=_sys.stderr,
                flush=True,
            )
            print(proc.stdout, file=_sys.stderr, flush=True)
            print(
                f"========== HERMES SUBPROCESS STDERR ({len(proc.stderr)} chars) ==========",
                file=_sys.stderr,
                flush=True,
            )
            print(proc.stderr, file=_sys.stderr, flush=True)
            print("========== END HERMES SUBPROCESS DUMP ==========\n", file=_sys.stderr, flush=True)
        if proc.returncode != 0:
            return AgentOutcome(
                status="subprocess_error",
                elapsed_s=elapsed,
                error=f"returncode={proc.returncode}",
                meta={
                    "stdout_tail": proc.stdout[-2000:],
                    "stderr_tail": proc.stderr[-2000:],
                },
            )

        tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
        if not tail:
            return AgentOutcome(
                status="empty",
                elapsed_s=elapsed,
                error="inner produced no JSON",
                meta={"stderr_tail": proc.stderr[-2000:]},
            )
        try:
            payload = json.loads(tail[-1])
        except json.JSONDecodeError as exc:
            return AgentOutcome(
                status="empty",
                elapsed_s=elapsed,
                error=f"malformed inner JSON: {exc}",
                meta={"raw": proc.stdout[-2000:]},
            )

        final_response = payload.get("final_response") or ""
        success = bool(payload.get("success"))
        return AgentOutcome(
            status="ok" if success else "exception",
            elapsed_s=elapsed,
            text=final_response,
            error=payload.get("error") if not success else None,
            decision=None,  # driver parses free-text for pbench; cases doesn't need it
            meta={
                "full_doc": payload.get("full_doc"),
                "fake_now": fake_now,
                "job_id": payload.get("job_id"),
                "cron_prompt": cron_spec.get("prompt"),
                **(cron_spec.get("_cron_meta") or {}),
            },
        )


__all__ = ["HermesBackend"]
