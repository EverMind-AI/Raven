"""Unified launch layer: one config file + one command for any registered bench.

``python -m raven.evolver run --config <yaml>`` drives the whole SOP flow as a
resumable state machine: cold-start thick ledger -> evolution rounds ->
terminate -> unseal. Interrupt anywhere; re-running the same command resumes
from the last durable artifact (trial files / round journal / meta stamps).
"""

from raven.evolver.launch.contract import BenchBundle, LaunchContext, validate_whitelist
from raven.evolver.launch.registry import load_bench

__all__ = ["BenchBundle", "LaunchContext", "load_bench", "validate_whitelist"]
