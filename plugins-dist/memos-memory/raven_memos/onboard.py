"""The ``raven onboard`` screen for MemOS Cloud: one key, one probe."""

from __future__ import annotations

from raven.memory_engine import ApiKeyOnboardStep
from raven.plugins import PluginContext
from raven_memos.backend import MemosBackend


def make_onboard_step(ctx: PluginContext) -> ApiKeyOnboardStep:
    return ApiKeyOnboardStep(ctx, MemosBackend)


__all__ = ["make_onboard_step"]
