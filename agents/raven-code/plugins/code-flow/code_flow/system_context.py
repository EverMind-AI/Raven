"""Add product instructions to system through the existing iteration hook.

The host has already selected history. Size the addition against the remaining
prompt allowance, including tools and the active model's full reply ceiling;
never remove transcript messages to make room for product instructions.
"""

from __future__ import annotations

from raven.contracts.loop_hooks import AgentHookContext, HookDecision
from raven.providers.base import send_max_tokens
from raven.providers.binding import active_binding
from raven.utils.tokens import estimate_prompt_tokens

_ADDITION = "code_flow.system_addition"
_TRUNCATED = (
    "\n\n[Repository instructions truncated to fit the context window. "
    "Read the relevant instruction files before changing code.]"
)


def inject_system_context(
    ctx: AgentHookContext, repository: str, concurrency: str, *, pending_note: str | None = None
) -> HookDecision:
    """Replace this hook's own addition, preserving the host's system prefix."""
    if ctx.messages is None or (not repository and not concurrency and _ADDITION not in ctx.metadata):
        return HookDecision()
    system = next((message for message in ctx.messages if message.get("role") == "system"), None)
    if system is None:
        return HookDecision(
            short_circuit_result="Raven-Code could not find a system message for repository instructions."
        )
    content = system.get("content") or ""
    previous, offset = ctx.metadata.pop(_ADDITION, (None, 0))
    if previous is not None:
        if isinstance(content, str) and isinstance(previous, str):
            if content[offset : offset + len(previous)] == previous:
                content = content[:offset] + content[offset + len(previous) :]
        elif isinstance(content, list):
            content = list(content)
            if offset < len(content) and content[offset] == previous:
                content.pop(offset)
    system["content"] = content
    if not repository and not concurrency:
        return HookDecision()

    def addition(text: str):
        text = ("\n\n" if content else "") + text
        return {"type": "text", "text": text} if isinstance(content, list) else text

    def render(text: str):
        part = addition(text)
        return [*content, part] if isinstance(content, list) else content + part

    binding = active_binding()
    window = binding.context_window if binding is not None else ctx.context_window_tokens
    allowance = None
    if window:
        # Production turns carry a binding. A standalone hook caller without
        # one has no model ceiling to consult, so retain half of its window.
        reserved = window // 2
        if binding is not None:
            provider = binding.provider
            reserved = send_max_tokens(
                getattr(provider, "generation", None),
                getattr(provider, "wire_model_id", lambda model: model)(binding.model),
                allow_fetch=False,
            )
        allowance = max(0, window - reserved - 32)

    def fits(text: str) -> bool:
        if allowance is None:
            return True
        candidate = [
            ({**message, "content": render(text)} if message is system else message) for message in ctx.messages
        ]
        if pending_note:
            candidate.append({"role": "user", "content": pending_note})
        return estimate_prompt_tokens(candidate, ctx.tools) <= allowance

    tail = ("\n\n" if repository and concurrency else "") + concurrency
    text = repository + tail
    if not fits(text):
        # Keep the complete concurrency warning even when repository text has
        # to shrink. If even the notice cannot fit, do not send an oversized call.
        suffix = (_TRUNCATED if repository else "") + tail
        if not fits(suffix):
            return HookDecision(
                short_circuit_result=(
                    "Raven-Code could not fit repository instructions and workspace notices alongside this conversation "
                    "and the model's reply allowance. Shorten the conversation or use a model with a larger context window."
                )
            )
        lo, hi = 0, len(repository)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fits(repository[:mid] + suffix):
                lo = mid
            else:
                hi = mid - 1
        text = repository[:lo] + suffix
    system["content"] = render(text)
    ctx.metadata[_ADDITION] = (addition(text), len(content))
    return HookDecision()
