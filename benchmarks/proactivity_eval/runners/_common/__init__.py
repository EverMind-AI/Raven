"""Shared helpers for proactivity-eval runners.

Every adapter (per-system × per-benchmark) used to re-implement the same
~400 lines of boilerplate (provider construction, proxy bypass, hermes-home
config inheritance, reward_data sampling, JSON decision parsing, obs
rendering). This package is the single source of truth.

Adapters import from here; nothing here imports from an adapter.
"""

from .agents import get_agent_config, reset_agent_config_cache
from .backend import AgentBackend, AgentOutcome, Sample
from .backends import get_backend
from .benchmarks import get_benchmark_config, reset_benchmark_config_cache
from .categories import CATEGORIES, sample_stratified
from .config import RunnersConfig, get_config, reset_config, resolve_path
from .driver import BenchmarkDriver
from .drivers import get_driver
from .env_loader import load_dotenvs
from .hermes_home import load_config_from_hermes_home, load_env_from_hermes_home
from .obs import build_obs_block, build_synth_block
from .openclaw import (
    build_openclaw_config,
    extract_response_text,
    run_openclaw_one_shot,
    write_openclaw_home,
)
from .parse import parse_decision
from .provider import make_provider
from .proxy import bypass_proxy_for_url, strip_proxy_env_vars

__all__ = [
    "AgentBackend",
    "AgentOutcome",
    "BenchmarkDriver",
    "CATEGORIES",
    "RunnersConfig",
    "Sample",
    "build_obs_block",
    "build_openclaw_config",
    "build_synth_block",
    "bypass_proxy_for_url",
    "extract_response_text",
    "get_agent_config",
    "get_backend",
    "get_benchmark_config",
    "get_config",
    "get_driver",
    "load_config_from_hermes_home",
    "load_dotenvs",
    "load_env_from_hermes_home",
    "make_provider",
    "parse_decision",
    "reset_agent_config_cache",
    "reset_benchmark_config_cache",
    "reset_config",
    "resolve_path",
    "run_openclaw_one_shot",
    "sample_stratified",
    "strip_proxy_env_vars",
    "write_openclaw_home",
]
