"""Prompt-cache breakpoints for gateway models Raven's table does not list, in this simulation's process only.

Through OpenRouter, the `openai/gpt-6` family bills a cache write on every request and reads nothing back unless the
request carries Anthropic-shaped `cache_control` breakpoints over the chat-completions wire (the Responses wire reads
nothing either way); with them it reads the marked prefix at a tenth of the input rate. Raven's
`accepts_cache_control` answers yes only for Anthropic's family, so its providers strip those marks, and LiteLLM's
OpenRouter transformation strips them again for any model outside its own short list. `install` widens both answers,
in the process that calls it, to the families in `READS_BREAKPOINTS` routed through OpenRouter. The Curator and the
owner run in that process; the employee and its subagents run in their own processes and keep both tables as
shipped, so the employee under cultivation is measured on Raven's own behaviour.
"""

from raven.providers import prompt_cache

READS_BREAKPOINTS = ("openai/gpt-6",)


def install() -> None:
    original = getattr(prompt_cache.accepts_cache_control, "__wrapped__", prompt_cache.accepts_cache_control)

    def accepts(model: str, *, addressed_to: str = "") -> bool:
        if original(model, addressed_to=addressed_to):
            return True
        routed = addressed_to == "openrouter" or str(model).startswith("openrouter/")
        return bool(model) and routed and any(family in model for family in READS_BREAKPOINTS)

    accepts.__wrapped__ = original
    prompt_cache.accepts_cache_control = accepts
    # The Curator's generation bound the name when it was imported.
    from experimental.curator.generation import run

    run.accepts_cache_control = accepts

    from litellm.llms.openrouter.chat.transformation import OpenrouterConfig

    keeps = getattr(OpenrouterConfig._supports_cache_control_in_content, "__wrapped__", None)
    keeps = keeps or OpenrouterConfig._supports_cache_control_in_content

    def in_content(self, model: str) -> bool:
        return keeps(self, model) or any(family in model for family in READS_BREAKPOINTS)

    in_content.__wrapped__ = keeps
    OpenrouterConfig._supports_cache_control_in_content = in_content
