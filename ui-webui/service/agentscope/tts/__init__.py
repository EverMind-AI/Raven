# -*- coding: utf-8 -*-
"""The TTS (Text-to-Speech) module in AgentScope."""

from ._dashscope import (
    DashScopeCosyVoiceTTSModel,
    DashScopeRealtimeTTSModel,
    DashScopeTTSModel,
)
from ._openai import OpenAITTSModel
from ._tts_base import TTSModelBase
from ._tts_model_card import TTSModelCard
from ._tts_response import TTSResponse, TTSUsage

__all__ = [
    "TTSModelBase",
    "TTSModelCard",
    "TTSResponse",
    "TTSUsage",
    "DashScopeCosyVoiceTTSModel",
    "DashScopeTTSModel",
    "DashScopeRealtimeTTSModel",
    "OpenAITTSModel",
]
