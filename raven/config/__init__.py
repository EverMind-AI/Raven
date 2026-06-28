"""Configuration module for Raven.

This package exposes two layers:

    Base layer (agent runtime):
        ``Config`` + ``load_config`` + path helpers — the fields inherited
        from the base agent framework (agents, channels, providers, tools).

    Raven feature layer:
        ``RavenConfig`` + ``load_raven_config`` + per-feature blocks
        (``ContextConfig``, ``SentinelConfig``, ``TokenWiseConfig``,
        ``SkillForgeConfig``). Defined in :mod:`raven.config.raven`.
"""

from raven.config.raven import (
    BudgetPolicyConfig,
    ContextConfig,
    RavenConfig,
    NudgePolicyConfig,
    SentinelConfig,
    SkillForgeConfig,
    SmartRoutingConfig,
    TokenWiseConfig,
    ToolResultLifecycleConfig,
    load_raven_config,
)
from raven.config.loader import get_config_path, load_config
from raven.config.paths import (
    get_bridge_install_dir,
    get_cli_history_path,
    get_cron_dir,
    get_data_dir,
    get_legacy_sessions_dir,
    get_logs_dir,
    get_media_dir,
    get_runtime_subdir,
    get_workspace_path,
)
from raven.config.schema import Config

__all__ = [
    # Base layer
    "Config",
    "load_config",
    "get_config_path",
    "get_data_dir",
    "get_runtime_subdir",
    "get_media_dir",
    "get_cron_dir",
    "get_logs_dir",
    "get_workspace_path",
    "get_cli_history_path",
    "get_bridge_install_dir",
    "get_legacy_sessions_dir",
    # Raven feature layer
    "RavenConfig",
    "load_raven_config",
    "ContextConfig",
    "SentinelConfig",
    "TokenWiseConfig",
    "SkillForgeConfig",
    "NudgePolicyConfig",
    "BudgetPolicyConfig",
    "SmartRoutingConfig",
    "ToolResultLifecycleConfig",
]
