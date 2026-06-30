"""
Raven — Agent framework with intelligent context management, proactivity,
token efficiency, and skill self-evolution.

Four feature pillars:
    1. Context Management   — context_engine/          (Curator engine)
    2. Proactivity          — proactive_engine/        (Sentinel + Scheduler)
    3. Token Efficiency     — token_wise/
    4. Skill Self-Evolution — memory_engine/skill_forge/

The base agent runtime (agent/, channels/, cli/, config/, providers/,
routing/, session/, templates/, utils/) originated from the MIT-licensed
nanobot project by HKUDS. Feature pillars listed above are new to Raven.
See LICENSE for attribution.
"""

import logging as _logging


class _LiteLLMBotocorePreloadFilter(_logging.Filter):
    """Drop LiteLLM's bedrock/sagemaker `botocore`-missing pre-load warnings.

    LiteLLM emits these at import-time when ``botocore`` is not installed
    (``boto3`` is an optional extra in Raven; not in the default deps).
    The warnings are noise for users who don't target AWS Bedrock / SageMaker.
    If those providers are actually used, the downstream call will surface
    a clearer error than this pre-load chatter.
    """

    _patterns = (
        "could not pre-load bedrock-runtime response stream shape",
        "could not pre-load sagemaker-runtime response stream shape",
    )

    def filter(self, record: _logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(p in msg for p in self._patterns)


_logging.getLogger("LiteLLM").addFilter(_LiteLLMBotocorePreloadFilter())

__version__ = "0.1.0"
__logo__ = "🐦‍⬛"
