"""Ops: orchestration of long-lived external compute jobs.

The capability that submits a job to a compute backend, detaches, and resumes
on completion — idempotently and across a crash. Import public types from here.
"""

from __future__ import annotations

from raven.ops.backend import (
    JobBackend,
    JobBackendError,
    JobHandle,
    JobResult,
    JobSpec,
    JobStatus,
)
from raven.ops.backends import DEFAULT_BACKEND, backend_from_meta, prepare_from_meta, register_backend
from raven.ops.campaign import Campaign, EscalationHandler, Trial
from raven.ops.docker_backend import DockerExecutor, make_ssh_runner
from raven.ops.driver import CampaignDriver
from raven.ops.event_watcher import OpsEventWatcher
from raven.ops.experiments import (
    experiment_a_interrupt,
    experiment_a_sweep,
    experiment_b_paired,
    experiment_wake_tradeoff,
    wake_tradeoff_curve,
)
from raven.ops.instrument import log_event, read_events
from raven.ops.ledger import JobRecord, Ledger
from raven.ops.metrics import campaign_metrics
from raven.ops.mock_backend import JobPlan, MockJobBackend
from raven.ops.policy import RetryPolicy
from raven.ops.proposer import (
    Completion,
    GridProposer,
    LLMProposer,
    Proposer,
    config_key,
    make_openai_completer,
    make_raven_completer,
)
from raven.ops.reliability import ScenarioResult, ScoreCard, run_suite
from raven.ops.runner import (
    drive_polling,
    ensure_image,
    make_ssh_sync,
    prepare_remote,
    run_adaptive,
    run_adaptive_polling,
    run_grid,
)
from raven.ops.scripted_backend import JobScript, ScriptedJobBackend, clear_worlds, install_world
from raven.ops.simclock import SimClock
from raven.ops.tuning import tune

__all__ = [
    "JobBackend",
    "JobBackendError",
    "JobHandle",
    "JobResult",
    "JobSpec",
    "JobStatus",
    "JobPlan",
    "MockJobBackend",
    "Ledger",
    "JobRecord",
    "Campaign",
    "Trial",
    "EscalationHandler",
    "RetryPolicy",
    "CampaignDriver",
    "ScenarioResult",
    "ScoreCard",
    "run_suite",
    "DockerExecutor",
    "make_ssh_runner",
    "Proposer",
    "GridProposer",
    "LLMProposer",
    "Completion",
    "config_key",
    "make_raven_completer",
    "make_openai_completer",
    "tune",
    "ensure_image",
    "prepare_remote",
    "make_ssh_sync",
    "run_grid",
    "run_adaptive",
    "run_adaptive_polling",
    "drive_polling",
    "OpsEventWatcher",
    "log_event",
    "read_events",
    "campaign_metrics",
    "experiment_a_interrupt",
    "experiment_a_sweep",
    "experiment_b_paired",
    "experiment_wake_tradeoff",
    "wake_tradeoff_curve",
    "SimClock",
    "JobScript",
    "ScriptedJobBackend",
    "install_world",
    "clear_worlds",
    "DEFAULT_BACKEND",
    "register_backend",
    "backend_from_meta",
    "prepare_from_meta",
]
