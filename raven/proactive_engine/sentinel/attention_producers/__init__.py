"""attention.md section producers — one class per H2.

Each :class:`AttentionProducer` subclass owns one canonical H2 section
in ``user_memory/attention.md``. :class:`AttentionUpdater` orchestrates
the lot: per tick it calls ``should_run`` → ``compute_body`` for each
producer (compute is outside the lock so slow producers like LLM calls
don't block hot-path writers), then grabs the attention.md lock once
and splices all bodies in a single write.

Splitting one section = one class means:

- P4-B (stance log, currently-focused-on) and P4-C (LLM forecast +
  behavior patterns) plug in by adding new producers — no edits to
  AttentionUpdater or the existing producers.
- Cooldown / feature-flag logic lives next to the section's content
  rendering, not bolted on the orchestrator.
- Tests target one producer at a time without dragging the whole
  orchestrator into the test setup.
"""

from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer,
)
from raven.proactive_engine.sentinel.attention_producers.active_threads import (
    ActiveThreadsProducer,
)
from raven.proactive_engine.sentinel.attention_producers.archived_patterns import (
    ArchivedPatternsProducer,
)
from raven.proactive_engine.sentinel.attention_producers.behavior_patterns import (
    BehaviorPatternsProducer,
)
from raven.proactive_engine.sentinel.attention_producers.currently_focused import (
    CurrentlyFocusedProducer,
)
from raven.proactive_engine.sentinel.attention_producers.pending_proposals import (
    PendingProposalsProducer,
)
from raven.proactive_engine.sentinel.attention_producers.predicted_3d import (
    Predicted3DProducer,
)
from raven.proactive_engine.sentinel.attention_producers.project_rhythm import (
    ProjectRhythmProducer,
)
from raven.proactive_engine.sentinel.attention_producers.recent_decisions import (
    RecentProactiveDecisionsProducer,
)
from raven.proactive_engine.sentinel.attention_producers.recently_abandoned import (
    RecentlyAbandonedProducer,
)
from raven.proactive_engine.sentinel.attention_producers.rejected_cooldown import (
    RejectedCooldownProducer,
)
from raven.proactive_engine.sentinel.attention_producers.sentinel_observations import (
    SentinelObservationsProducer,
)
from raven.proactive_engine.sentinel.attention_producers.stance_log import (
    StanceLogProducer,
)
from raven.proactive_engine.sentinel.attention_producers.daily_plan import (
    DailyPlanProducer,
)

__all__ = [
    "AttentionProducer",
    "ActiveThreadsProducer",
    "ArchivedPatternsProducer",
    "BehaviorPatternsProducer",
    "CurrentlyFocusedProducer",
    "DailyPlanProducer",
    "PendingProposalsProducer",
    "Predicted3DProducer",
    "ProjectRhythmProducer",
    "RecentProactiveDecisionsProducer",
    "RecentlyAbandonedProducer",
    "RejectedCooldownProducer",
    "SentinelObservationsProducer",
    "StanceLogProducer",
]
