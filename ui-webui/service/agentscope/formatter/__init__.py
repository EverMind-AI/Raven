# -*- coding: utf-8 -*-
"""The formatter module in agentscope."""

from ._anthropic_formatter import (
    AnthropicChatFormatter,
    AnthropicMultiAgentFormatter,
)
from ._dashscope_formatter import (
    DashScopeChatFormatter,
    DashScopeMultiAgentFormatter,
)
from ._deepseek_formatter import (
    DeepSeekChatFormatter,
    DeepSeekMultiAgentFormatter,
)
from ._formatter_base import FormatterBase
from ._gemini_formatter import (
    GeminiChatFormatter,
    GeminiMultiAgentFormatter,
)
from ._moonshot_formatter import (
    MoonshotChatFormatter,
    MoonshotMultiAgentFormatter,
)
from ._ollama_formatter import (
    OllamaChatFormatter,
    OllamaMultiAgentFormatter,
)
from ._openai_formatter import (
    OpenAIChatFormatter,
    OpenAIMultiAgentFormatter,
)
from ._xai_formatter import (
    XAIChatFormatter,
    XAIMultiAgentFormatter,
)

__all__ = [
    "FormatterBase",
    "DashScopeChatFormatter",
    "DashScopeMultiAgentFormatter",
    "OpenAIChatFormatter",
    "OpenAIMultiAgentFormatter",
    "AnthropicChatFormatter",
    "AnthropicMultiAgentFormatter",
    "GeminiChatFormatter",
    "GeminiMultiAgentFormatter",
    "OllamaChatFormatter",
    "OllamaMultiAgentFormatter",
    "DeepSeekChatFormatter",
    "DeepSeekMultiAgentFormatter",
    "MoonshotChatFormatter",
    "MoonshotMultiAgentFormatter",
    "XAIChatFormatter",
    "XAIMultiAgentFormatter",
]
