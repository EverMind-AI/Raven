"""The ``raven onboard`` screen for Zep Cloud: one key, one probe."""

from __future__ import annotations

from raven.memory_engine.api_key_onboard import ApiKeyOnboardStep
from raven.plugins import PluginContext
from raven_zep.backend import ZepBackend


def make_onboard_step(ctx: PluginContext) -> ApiKeyOnboardStep:
    return ApiKeyOnboardStep(ctx, ZepBackend)


__all__ = ["make_onboard_step"]
