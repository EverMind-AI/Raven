"""The ``raven onboard`` screen for Mem0 Platform: one key, one probe."""

from __future__ import annotations

from raven.memory_engine.api_key_onboard import ApiKeyOnboardStep
from raven.plugins import PluginContext
from raven_mem0.backend import Mem0Backend


def make_onboard_step(ctx: PluginContext) -> ApiKeyOnboardStep:
    return ApiKeyOnboardStep(ctx, Mem0Backend)


__all__ = ["make_onboard_step"]
