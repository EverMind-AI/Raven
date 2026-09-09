"""Token accounting shared by the flow's budget-aware gates.

``usage_tokens`` reads one response's reported usage - prompt plus completion -
tolerating ``LLMResponse``-shaped objects and plain dicts, and answering 0 for
anything it cannot read. The budget note and the spin breaker both size their
decisions from it.
"""

from __future__ import annotations


def usage_tokens(response: object) -> int:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if not isinstance(usage, dict):
        return 0
    try:
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return prompt + completion


__all__ = ["usage_tokens"]
