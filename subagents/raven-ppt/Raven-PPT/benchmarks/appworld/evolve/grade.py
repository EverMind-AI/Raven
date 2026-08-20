"""Grading and result recording for AppWorld trials — the fixed scorer.

This module is the measurement half of ``agent_cli.py``: the ``/evaluate``
oracle call, the success/infra classification, and the result-file write.
It lives under ``evolve/`` deliberately — that prefix is in the evolver's
IMMUTABLE_PATTERNS, so a candidate may rewrite the agent surface
(prompt, loop wiring, tools) but never the code that grades it. Keep any
logic that decides *what counts as a pass or an infra failure* here, not
in the editable files.
"""

from __future__ import annotations

import json
import time
import traceback

import requests


def post(env_url: str, path: str, body: dict, timeout: float = 120.0) -> dict:
    r = requests.post(f"{env_url.rstrip('/')}{path}", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json().get("output", {})


# When the LLM endpoint drops/times-out/rate-limits, the agent loop catches the
# provider error and returns it as the final content (finish_reason=="error"),
# so it never raises up to the HTTP layer and would otherwise be graded as a
# normal wrong answer. That is infra, not the agent's fault -- detect it and
# raise so the run is recorded as infra_error and excluded from scoring. The
# signatures are short one-liners; the length guard avoids flagging a long
# genuine answer that merely mentions one of these words.
_LLM_TRANSPORT_SIGNS = (
    "error: connection error",
    "sorry, i encountered an error calling the ai model",
    "apiconnectionerror",
    "apitimeouterror",
    "api timeout error",
    "ratelimiterror",
    "rate limit",
    "internal server error",
    "service unavailable",
    "502 bad gateway",
    "503 service",
    "bad gateway",
    # litellm renders provider failures without the spaced/colon forms above
    # (observed leaking through as INCOMPLETE: "Error calling LLM:
    # litellm.InternalServerError: OpenAIException - Connection error.")
    "error calling llm",
    "connection error",
    "internalservererror",
)


def is_llm_transport_error(resp: str | None) -> bool:
    if not resp:
        return False
    s = resp.strip().lower()
    if len(s) > 300:
        return False
    return any(sig in s for sig in _LLM_TRANSPORT_SIGNS)


def endpoint_dead(config_path: str) -> bool:
    """True only when the subject endpoint is transport-unreachable.

    Disambiguates an EMPTY final response: a dead endpoint (DNS gone,
    connection refused) yields empty completions on every task, which would
    otherwise be graded INCOMPLETE — real-looking fails that poison whole
    evals (observed: a mid-run endpoint death scored 270/270 INCOMPLETE).
    But an empty response with a HEALTHY endpoint is the agent's own W1
    stall and must stay a legit fail, so only a transport-level probe
    failure counts as dead; any HTTP status means alive.
    """
    try:
        cfg = json.load(open(config_path))
        defaults = cfg.get("agents", {}).get("defaults", {})
        base = (cfg.get("providers", {}).get(defaults.get("provider")) or {}).get("api_base")
        if not base:
            return False
        requests.get(base.rstrip("/") + "/models", timeout=10)
        return False
    except requests.RequestException:
        return True
    except Exception:
        return False


def grade_and_record(result: dict, *, env_url: str, task_id: str, response: str, config_path: str, t0: float) -> None:
    """Score one finished attempt via the env oracle and fill ``result``.

    Raises ``RuntimeError`` on a transport-shaped final response so the
    caller records the attempt as infra (Gate-f), not as a legit fail.
    """
    if is_llm_transport_error(response):
        raise RuntimeError(f"llm_transport_error: {response.strip()[:160]}")
    if not response.strip() and endpoint_dead(config_path):
        raise RuntimeError("llm_transport_error: empty response and subject endpoint unreachable")
    ev = post(env_url, "/evaluate", {"task_id": task_id, "suppress_errors": True})
    done = post(env_url, "/task_completed", {"task_id": task_id})
    # Capture the full evaluation oracle (passes/failures) for the evolver's
    # diagnose step; derive pass_count when the env omits it.
    _passes = ev.get("passes") or []
    _failures = ev.get("failures") or []
    result.update(
        success=bool(ev.get("success")),
        num_tests=ev.get("num_tests"),
        pass_count=ev.get("pass_count") if ev.get("pass_count") is not None else len(_passes),
        evaluation={"passes": _passes, "failures": _failures},
        task_completed=bool(done) if not isinstance(done, dict) else done,
        response=(response or "")[:2000],
        elapsed_s=round(time.time() - t0, 1),
    )


def record_infra(result: dict, exc: BaseException, *, t0: float) -> None:
    result.update(
        success=False,
        infra_error=f"{type(exc).__name__}: {exc}",
        traceback=traceback.format_exc()[-2000:],
        elapsed_s=round(time.time() - t0, 1),
    )


def write_result(out_path: str, result: dict) -> None:
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
