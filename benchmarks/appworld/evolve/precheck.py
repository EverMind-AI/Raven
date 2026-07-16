"""Gate0 pre-scoring environment health check for the AppWorld line (SOP §0).

Wired as ``EvalBackend.precheck``, the loop runs it at the start of every round
BEFORE any scoring. A dirty environment makes every score of the round invalid
(the TB2 debian-apt / proxy-hijack lesson), so this raises with everything it
found wrong — fix the box, then resume — instead of letting a batch run against
a broken setup and record garbage.

Checks, cheapest first:

- the appworld install ``batch.py`` will spawn env servers from (venv binary +
  data dir) is present;
- the subject config exists and names a provider endpoint;
- no orphan env servers hold the ports this run's batches will bind (the
  classic killed-run leftover that 500s every task);
- the subject endpoint sustains a REAL generation (~300 tokens) at healthy
  throughput. A tiny ping is not enough: a congested shared backend answers a
  16-token probe in seconds while real tasks blow the runner timeout (observed:
  3.5s ping alongside 68% of trials timing out at 900s), so the probe must
  measure decode throughput, not reachability.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Optional

from benchmarks.appworld import batch as batch_mod
from benchmarks.appworld.evolve.adapter import AppWorldConfig
from raven.evolver.orchestrator.scoring import PrecheckFn


def _port_bound(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _subject_endpoint(config_path: Path) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``(api_base, model, problem)`` from the subject config; problem set on failure."""
    try:
        cfg = json.loads(config_path.read_text())
        defaults = cfg.get("agents", {}).get("defaults", {})
        model = defaults.get("model")
        provider = defaults.get("provider")
        api_base = (cfg.get("providers", {}).get(provider) or {}).get("api_base")
    except (OSError, ValueError, AttributeError) as e:
        return None, None, f"subject config unreadable ({config_path}): {e}"
    if not (api_base and model):
        return None, None, f"subject config missing provider api_base/model: {config_path}"
    return api_base, model, None


def _endpoint_problem(
    api_base: str, model: str, timeout: float, min_tok_per_s: float
) -> Optional[str]:
    import time

    import httpx  # lazy, same as the driver transport

    url = api_base.rstrip("/") + "/chat/completions"
    # Real-generation probe: ask for a ~300-token completion and judge decode
    # throughput. On reasoning models the budget fills with thinking tokens,
    # which is fine — usage.completion_tokens still measures decode speed.
    body = {
        "model": model,
        "messages": [{"role": "user", "content":
                      "Explain how TCP congestion control works, in about 300 words."}],
        "max_tokens": 300,
        "temperature": 0,
    }
    t0 = time.monotonic()
    try:
        resp = httpx.post(url, json=body, timeout=timeout)
    except httpx.TimeoutException:
        return (f"subject endpoint degraded ({url}): no 300-token completion "
                f"within {timeout:.0f}s")
    except httpx.HTTPError as e:
        return f"subject endpoint unreachable ({url}): {type(e).__name__}: {e}"
    elapsed = max(time.monotonic() - t0, 1e-6)
    if resp.status_code != 200:
        return f"subject endpoint unhealthy ({url}): HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        ntok = int(resp.json().get("usage", {}).get("completion_tokens") or 0)
    except ValueError:
        return f"subject endpoint unhealthy ({url}): non-JSON response: {resp.text[:200]}"
    if ntok == 0:
        return f"subject endpoint unhealthy ({url}): empty generation (0 completion tokens)"
    tps = ntok / elapsed
    if tps < min_tok_per_s:
        return (f"subject endpoint degraded ({url}): {ntok} tokens in {elapsed:.1f}s "
                f"= {tps:.1f} tok/s (< {min_tok_per_s:.1f} tok/s floor)")
    return None


def make_appworld_precheck(
    aw: AppWorldConfig,
    *,
    check_endpoint: bool = True,
    # 300 tokens in <25s (the SOP health bar) is 12 tok/s; the healthy band
    # observed on the shared subject backend is 12-33 tok/s.
    endpoint_timeout: float = 60.0,
    min_tok_per_s: float = 12.0,
) -> PrecheckFn:
    """Build the per-round Gate0 precheck for one AppWorld configuration."""

    def precheck() -> None:
        problems: list[str] = []
        if not Path(batch_mod.APPWORLD_BIN).exists():
            problems.append(f"appworld venv binary missing: {batch_mod.APPWORLD_BIN}")
        data_dir = Path(batch_mod.APPWORLD_ROOT) / "data"
        if not data_dir.exists():
            problems.append(f"appworld data dir missing: {data_dir}")

        base = aw.base_port if aw.base_port is not None else 8100
        busy = [p for p in range(base, base + aw.conc) if _port_bound(p)]
        if busy:
            problems.append(
                f"env-server ports already bound (orphan servers from a killed run?): {busy}"
                " — pkill -9 -f 'serve environment' and re-run"
            )

        if not aw.config_path.exists():
            problems.append(f"subject config missing: {aw.config_path}")
        elif check_endpoint:
            api_base, model, cfg_problem = _subject_endpoint(aw.config_path)
            if cfg_problem:
                problems.append(cfg_problem)
            else:
                ep_problem = _endpoint_problem(
                    api_base, model, endpoint_timeout, min_tok_per_s)
                if ep_problem:
                    problems.append(ep_problem)

        if problems:
            raise RuntimeError("Gate0 precheck failed: " + " | ".join(problems))

    return precheck


__all__ = ["make_appworld_precheck"]
