"""OpenClaw subprocess helpers — shared by cases + pbench adapters.

Previously lived in ``openclaw_runner.py``; moving here breaks the
adapter-imports-adapter anti-pattern so both entry points import cleanly
from ``_common``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import get_config
from .proxy import strip_proxy_env_vars

DEFAULT_PROVIDER_KEY = "local"


def build_openclaw_config(
    model_id: str | None = None,
    base_url: str | None = None,
    provider_key: str = DEFAULT_PROVIDER_KEY,
    workspace: str | None = None,
    bootstrap_max_chars: int = 1,
    mcp_servers: dict[str, Any] | None = None,
    api_key_override: str | None = None,
    context_window: int | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Produce a minimal openclaw.json pointing at an OpenAI-compatible endpoint.

    Defaults pull from the global ``runners.config.yaml`` LLM block.
    bootstrap_max_chars=1 (default) suppresses the ~13K chars of SOUL.md /
    IDENTITY.md bootstrap. Raise this (e.g. 20000) when enabling memory —
    OpenClaw then injects ``workspace/memory/MEMORY.md`` into system prompt.

    mcp_servers (longrun only): dict of stdio MCP server configs to inject
    under ``mcp.servers``. Requires OC ≥ 2026.3.31. Shape:
    ``{"name": {"command": "node", "args": [...], "env": {...}}}``.
    """
    cfg = get_config()
    model_id = model_id or cfg.vllm_model_id
    base_url = base_url or cfg.vllm_base_url
    api_key = api_key_override or cfg.vllm_api_key
    ctx_window = context_window or cfg.vllm_context_window
    max_out = max_tokens or cfg.vllm_max_tokens
    ws = workspace or tempfile.mkdtemp(prefix="openclaw-empty-ws-")
    out: dict[str, Any] = {
        "agents": {
            "defaults": {
                "model": {"primary": f"{provider_key}/{model_id}"},
                "workspace": ws,
                "bootstrapMaxChars": bootstrap_max_chars,
            }
        },
        "models": {
            "providers": {
                provider_key: {
                    "baseUrl": base_url,
                    "apiKey": api_key,
                    "api": "openai-completions",
                    "models": [
                        {
                            "id": model_id,
                            "name": model_id,
                            "reasoning": True,
                            "input": ["text"],
                            "contextWindow": ctx_window,
                            "maxTokens": max_out,
                            # vLLM-served qwen3.5 rejects OpenAI-reasoning-
                            # specific request shape: the `developer` role
                            # (o1/o3-only) and the `store` field both trip
                            # its chat-template / schema validation, and its
                            # request body uses `max_tokens` not
                            # `max_completion_tokens`.
                            "compat": {
                                "supportsDeveloperRole": False,
                                "supportsStore": False,
                                "maxTokensField": "max_tokens",
                            },
                        }
                    ],
                }
            }
        },
        # Tools profile intentionally NOT set → OpenClaw uses its default
        # tool registry (read_file / write_file / bash / web_search / etc).
        # Earlier we forced "minimal" here to suppress tool calls during
        # pbench cold-mode runs; that biased the comparison since Raven
        # and Hermes both run with full tools in their backends. Match
        # real-world deployment by deferring to OC's default.
    }
    if mcp_servers:
        out["mcp"] = {"servers": mcp_servers}
    return out


def write_openclaw_home(dest: Path, config: dict[str, Any]) -> None:
    (dest / ".openclaw").mkdir(parents=True, exist_ok=True)
    (dest / ".openclaw" / "openclaw.json").write_text(json.dumps(config, indent=2, ensure_ascii=False))


def build_subprocess_env(openclaw_home: Path) -> dict[str, str]:
    """Strip proxy vars + set OPENCLAW_HOME for a per-task subprocess."""
    env = strip_proxy_env_vars()
    env["OPENCLAW_HOME"] = str(openclaw_home)
    return env


def extract_response_text(stdout: str, stderr: str = "") -> str | None:
    """Pull the last assistant reply out of ``openclaw --json`` output.

    Scans both streams (openclaw may split depending on mode); finds the
    outermost top-level JSON object (not a nested ``{``); reads the
    assistant reply, trying:

    1. ``result.finalAssistantRawText`` / ``result.finalAssistantVisibleText``
       — OC ≥ 2026.4 wraps the reply here for normal LLM turns.
    2. ``finalAssistantRawText`` / ``finalAssistantVisibleText`` — same
       fields at the top level (older shapes).
    3. ``payloads[0].text`` / ``result.payloads[0].text`` — legacy fallback;
       OC sometimes uses this for tool-finalized responses.

    Earlier versions of this function used ``rfind("\\n{")`` which on
    pretty-printed JSON locks onto the LAST inner ``{`` (e.g.
    ``completion: {``) and fails to parse the whole document; that
    silently dropped ~46% of OC longrun replies as ``[openclaw no-text]``.
    Now we anchor at the first ``{`` of stdout (the outer document) and
    parse forward.
    """
    for source in (stdout, stderr):
        if not source:
            continue
        # First {-anchor in the stream is the document boundary; OC --json
        # writes one top-level object per invocation.
        idx = source.find("{")
        if idx < 0:
            continue
        try:
            data = json.loads(source[idx:])
        except (json.JSONDecodeError, TypeError):
            continue
        result = data.get("result") if isinstance(data.get("result"), dict) else None
        for container in (result, data):
            if not isinstance(container, dict):
                continue
            for field in ("finalAssistantRawText", "finalAssistantVisibleText"):
                v = container.get(field)
                if isinstance(v, str) and v.strip():
                    return v
            payloads = container.get("payloads")
            if isinstance(payloads, list) and payloads:
                t = (payloads[0] or {}).get("text", "")
                if isinstance(t, str) and t.strip():
                    return t
    return None


def run_openclaw_one_shot(
    prompt: str,
    *,
    session_id: str,
    thinking: str,
    cli_timeout_s: int,
    subprocess_timeout_s: int,
    config: dict[str, Any] | None = None,
    openclaw_cmd: str = "openclaw",
    docker_image: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Spawn one ``openclaw agent --local`` call, return a result dict.

    status ∈ {ok, timeout, subprocess_error, empty_output}. On ``ok`` the
    ``text`` field is the assistant's final reply; otherwise ``text`` is None
    and ``error`` + tail logs are populated.

    When ``docker_image`` is set the CLI runs inside that image via
    ``docker run --rm``. ``home/.openclaw`` is bind-mounted to
    ``/home/node/.openclaw`` (the container's default ``OPENCLAW_HOME``).
    The caller may pre-allocate ``home`` to plant workspace files before
    the call; otherwise a fresh tmpdir is used.
    """
    if home is None:
        home = Path(tempfile.mkdtemp(prefix=f"openclaw-{session_id}-"))
    write_openclaw_home(home, config or build_openclaw_config())

    cli_args = [
        "agent",
        "--local",
        "--session-id",
        session_id,
        "--message",
        prompt,
        "--thinking",
        thinking,
        "--timeout",
        str(cli_timeout_s),
        "--json",
    ]

    container_name: str | None = None
    if docker_image:
        safe_sid = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id[:16])
        container_name = f"oc-{safe_sid}-{time.monotonic_ns()}"
        cmd = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--name",
            container_name,
            "-v",
            f"{home}/.openclaw:/home/node/.openclaw",
            docker_image,
            "node",
            "dist/index.js",
        ] + cli_args
        env = None
    else:
        cmd = [openclaw_cmd] + cli_args
        env = build_subprocess_env(home)

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=subprocess_timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = round(time.monotonic() - started, 2)
        if container_name:
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                timeout=5,
            )
        shutil.rmtree(home, ignore_errors=True)
        return {
            "status": "timeout",
            "elapsed_s": elapsed,
            "error": f"subprocess timeout after {subprocess_timeout_s}s",
            "text": None,
            "stdout_tail": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
        }
    finally:
        if not os.environ.get("OC_KEEP_HOME"):
            shutil.rmtree(home, ignore_errors=True)
        else:
            import sys as _sys

            print(f"OC_HOME_KEPT: {home}", file=_sys.stderr, flush=True)

    elapsed = round(time.monotonic() - started, 2)

    # Diagnostic: when OC_DEBUG_TOOLS is set, dump full stdout/stderr to
    # parent stderr so tool-use evidence (which the JSON output strips)
    # becomes visible in the parent log.
    if os.environ.get("OC_DEBUG_TOOLS"):
        import sys as _sys

        print(f"\n========== OC SUBPROCESS STDOUT ({len(proc.stdout)} chars) ==========", file=_sys.stderr, flush=True)
        print(proc.stdout, file=_sys.stderr, flush=True)
        print(f"========== OC SUBPROCESS STDERR ({len(proc.stderr)} chars) ==========", file=_sys.stderr, flush=True)
        print(proc.stderr, file=_sys.stderr, flush=True)
        print("========== END OC SUBPROCESS DUMP ==========\n", file=_sys.stderr, flush=True)

    if proc.returncode != 0:
        return {
            "status": "subprocess_error",
            "elapsed_s": elapsed,
            "error": f"returncode={proc.returncode}",
            "text": None,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        }

    text = extract_response_text(proc.stdout, proc.stderr)
    if text is None:
        return {
            "status": "empty_output",
            "elapsed_s": elapsed,
            "error": "no payload text found",
            "text": None,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        }

    return {
        "status": "ok",
        "elapsed_s": elapsed,
        "error": None,
        "text": text,
        "stdout_tail": proc.stdout[-500:],
        "stderr_tail": proc.stderr[-500:],
    }


__all__ = [
    "DEFAULT_PROVIDER_KEY",
    "build_openclaw_config",
    "build_subprocess_env",
    "extract_response_text",
    "run_openclaw_one_shot",
    "write_openclaw_home",
]
