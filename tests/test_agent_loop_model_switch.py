"""A live model switch must reach everything holding the old provider.

``config.set key="model"`` builds a fresh provider and hands it to
``AgentLoop.set_provider``. The loop is not the only holder: the subagent
manager, the context engine's LLM-backed segments and the consolidator
each captured the provider they were built with. When the switch stopped
at ``loop.provider``, those three kept calling the abandoned endpoint --
subagent spawns and the skill rewriter/gate failed to authenticate while
the main loop worked fine.
"""

from types import SimpleNamespace

from raven.agent.loop.main import AgentLoop
from raven.context_engine.assembler import ContextAssembler
from raven.memory_engine.skill_forge.gate import LLMGateFilter
from raven.memory_engine.skill_forge.rewriter import QueryRewriter


class _Recorder:
    """Holder that remembers the provider/model it was last pointed at."""

    def __init__(self) -> None:
        self.provider: object = "old-provider"
        self.model = "old-model"

    def set_provider(self, provider: object, model: str) -> None:
        self.provider = provider
        self.model = model


class _TextOnlyBuilder:
    """A segment that never calls an LLM, so it has no set_provider."""

    name = "identity"
    order = 1
    needs_prefix = False

    async def build(self, ctx):  # pragma: no cover - never invoked here
        return None


def test_set_provider_reaches_every_holder() -> None:
    loop = object.__new__(AgentLoop)
    loop.provider = "old-provider"
    loop.model = "old-model"
    loop.subagents = _Recorder()
    loop.context_engine = _Recorder()
    loop.memory_consolidator = _Recorder()

    new_provider = SimpleNamespace(name="new-provider")
    loop.set_provider(new_provider, "anthropic/claude-opus-4-8")

    assert loop.provider is new_provider
    assert loop.model == "anthropic/claude-opus-4-8"
    for holder in (loop.subagents, loop.context_engine, loop.memory_consolidator):
        assert holder.provider is new_provider
        assert holder.model == "anthropic/claude-opus-4-8"


def test_assembler_forwards_to_llm_backed_builders_only() -> None:
    llm_builder = _Recorder()
    llm_builder.name = "skills"
    llm_builder.order = 5
    llm_builder.needs_prefix = False
    text_builder = _TextOnlyBuilder()

    assembler = ContextAssembler([llm_builder, text_builder], lambda: [])
    new_provider = SimpleNamespace(name="new-provider")

    # The text-only builder has no set_provider; walking must skip it rather
    # than blow up, which is why the fan-out is duck-typed.
    assembler.set_provider(new_provider, "anthropic/claude-opus-4-8")

    assert llm_builder.provider is new_provider
    assert llm_builder.model == "anthropic/claude-opus-4-8"


def test_rewriter_and_gate_adopt_the_new_provider() -> None:
    new_provider = SimpleNamespace(name="new-provider")

    rewriter = QueryRewriter("old-provider")
    rewriter.set_provider(new_provider, "anthropic/claude-opus-4-8")
    assert rewriter._provider is new_provider

    gate = LLMGateFilter("old-provider")
    gate.set_provider(new_provider, "anthropic/claude-opus-4-8")
    assert gate._provider is new_provider
    # Unset means the gate follows the provider's default model, so the
    # switch must not pin it to the agent's model behind the user's back.
    assert gate._model is None


def test_pinned_gate_model_survives_the_switch() -> None:
    gate = LLMGateFilter("old-provider", model="openai/gpt-5-mini")
    gate.set_provider(SimpleNamespace(name="new-provider"), "anthropic/claude-opus-4-8")
    assert gate._model == "openai/gpt-5-mini"
