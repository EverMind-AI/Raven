"""Role call_fn factory: yaml ``models:`` section -> {driver, design, verdict}.

Provider specs:

- ``{provider: openai_compat, base_url, model, ...}`` -> one OpenAI-compatible
  chat endpoint (:func:`raven.evolver.orchestrator.providers.openai_compat.make_call_fn`).
- ``{provider: claude_cli, model, ...}`` -> ``claude -p`` subprocess per call.
- ``{provider: raven, model?, api_base?, api_key_env?}`` -> raven's own
  ``LitellmProvider`` bridged to a sync call_fn; ``model`` omitted falls back
  to the raven config's ``agents.defaults.model`` — so a config file with no
  ``models:`` section evolves with whatever model Raven itself is running.

Role fallbacks: ``design`` omitted -> reuse driver; ``verdict`` omitted ->
None (the orchestrator then drafts verdicts with the driver). Note the driver
model and the *subject's* model are different knobs: the subject agent's model
lives in the bench config and is pinned for the whole run (same-regime rule).
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

CallFn = Callable[[list], str]

_DEFAULT_SPEC = {"provider": "raven"}


def _raven_default_model() -> str:
    from raven.config.loader import load_config

    return load_config().agents.defaults.model


def make_raven_call_fn(
    model: Optional[str] = None,
    *,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 8192,
    temperature: float = 0.0,
) -> CallFn:
    from raven.providers.litellm_provider import LiteLLMProvider

    provider = LiteLLMProvider(
        api_key=api_key, api_base=api_base,
        default_model=model or _raven_default_model(),
    )

    def call(messages: list) -> str:
        # Sync bridge: each call owns a private event loop, so this is safe
        # from the loop's worker threads (parallel taxonomy induction).
        resp = asyncio.run(provider.chat_with_retry(
            messages, max_tokens=max_tokens, temperature=temperature,
        ))
        content = getattr(resp, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("raven provider returned empty content")
        return content

    return call


def build_call_fn(spec: dict, *, role: str = "?") -> CallFn:
    try:
        return _build_call_fn(spec)
    except TypeError as exc:
        # An unknown/missing kwarg in the provider factory is a config typo,
        # not a programming error — surface it as the readable kind.
        raise ValueError(f"models.{role}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"models.{role}: {exc}") from exc


def _build_call_fn(spec: dict) -> CallFn:
    if not isinstance(spec, dict):
        raise ValueError(f"model spec must be a mapping, got {type(spec).__name__}")
    kind = spec.get("provider", "raven")
    kwargs = {k: v for k, v in spec.items() if k != "provider"}
    if kind == "openai_compat":
        from raven.evolver.orchestrator.providers.openai_compat import make_call_fn

        if "retry_delays" in kwargs:
            delays = kwargs["retry_delays"]
            if not isinstance(delays, (list, tuple)):
                raise ValueError("retry_delays must be a list of seconds")
            kwargs["retry_delays"] = tuple(delays)
        return make_call_fn(**kwargs)
    if kind == "claude_cli":
        import shutil

        from raven.evolver.orchestrator.providers.claude_cli import make_claude_call_fn

        model = kwargs.pop("model", None)
        if not model:
            raise ValueError("claude_cli spec requires 'model'")
        claude_bin = kwargs.get("claude_bin", "claude")
        if shutil.which(claude_bin) is None:
            raise ValueError(
                f"claude_cli: {claude_bin!r} not found on PATH — install the "
                "Claude Code CLI and log in, or switch this role to "
                "openai_compat/raven"
            )
        return make_claude_call_fn(model, **kwargs)
    if kind == "raven":
        model = kwargs.pop("model", None)
        return make_raven_call_fn(model, **kwargs)
    raise ValueError(
        f"unknown model provider {kind!r} (expected openai_compat / claude_cli / raven)"
    )


def build_role_call_fns(models_cfg: dict) -> dict[str, Optional[CallFn]]:
    driver = build_call_fn(models_cfg.get("driver", _DEFAULT_SPEC), role="driver")
    design = (
        build_call_fn(models_cfg["design"], role="design")
        if models_cfg.get("design") else driver
    )
    verdict = (
        build_call_fn(models_cfg["verdict"], role="verdict")
        if models_cfg.get("verdict") else None
    )
    return {"driver": driver, "design": design, "verdict": verdict}


def describe_models(models_cfg: dict) -> dict:
    """Resolved model description for the run_meta snapshot (no secrets)."""
    out = {}
    for role in ("driver", "design", "verdict"):
        spec = models_cfg.get(role)
        if spec is None:
            if role == "driver":
                spec = _DEFAULT_SPEC
            elif role == "design":
                spec = {"inherit": "driver"}
            else:
                spec = {"omitted": "driver drafts verdicts"}
        out[role] = {k: v for k, v in spec.items() if "key" not in k.lower()}
        if out[role].get("provider", "raven") == "raven" and "model" not in out[role] \
                and "inherit" not in out[role] and "omitted" not in out[role]:
            try:
                out[role]["model"] = _raven_default_model()
            except Exception:  # noqa: BLE001 — description is best-effort
                out[role]["model"] = "<raven default>"
    return out


__all__ = ["CallFn", "build_call_fn", "build_role_call_fns", "describe_models",
           "make_raven_call_fn"]
