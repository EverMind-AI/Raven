"""The loop's ContextBuilder must know the model, or estimation drifts.

Regression (2026-08-13): ``AgentLoop`` constructed its ``ContextBuilder``
without ``model``, so ``build_system_prompt`` -- the renderer behind
``_make_token_budget`` and ``MemoryConsolidator`` -- always rendered the
*default* identity. The live prompt path (``build_context_engine`` ->
``IdentitySegmentBuilder``) did receive the model, so on Claude models the
budget was estimated against a prompt ~500 tokens shorter than the one
actually sent, overstating the history budget by the same amount.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7,
                   reasoning_effort=None, tool_choice=None):
        return LLMResponse(content="ok", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


def _loop(workspace: Path, model: str) -> AgentLoop:
    return AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model=model,
        max_iterations=5,
        restrict_to_workspace=True,
        interactive=False,
    )


# First lines of the two identity prompt files -- distinct on purpose, see
# raven/context_engine/segments/prompts/coding/{anthropic,default}.txt.
_ANTHROPIC_MARK = "interactive CLI tool"
_DEFAULT_MARK = "interactive agent"


def test_budget_estimation_renders_the_claude_identity_for_claude_models(workspace):
    prompt = _loop(workspace, "anthropic/claude-opus-4.8").context.build_system_prompt()
    assert _ANTHROPIC_MARK in prompt
    assert _DEFAULT_MARK not in prompt


def test_budget_estimation_keeps_the_default_identity_for_other_models(workspace):
    prompt = _loop(workspace, "deepseek-v4-flash").context.build_system_prompt()
    assert _DEFAULT_MARK in prompt
    assert _ANTHROPIC_MARK not in prompt
