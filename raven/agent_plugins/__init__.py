"""Agent Plugin packages: the portable package format of Agent Plugins 1.0.

A package is a directory whose root holds a ``plugin.json`` that validates
against the Agent Plugins 1.0.0 manifest schema, with optional ``skills/``
and ``mcp.json`` components. It contributes skills and MCP servers, not code
loaded into the host process; a package's MCP server is a separate process.
It is not a Raven kernel plugin (which ships Python factories in
``raven-plugin.toml``).

The loader reads the manifest, discovers the package's skills, validates
them against the Agent Skills rules, imports them into the workspace skill
tree, and composes the servers its ``mcp.json`` declares.
"""

from raven.agent_plugins.ledger import LEDGER_VERSION, LedgerEntry, PackageLedger
from raven.agent_plugins.manifest import (
    AGENT_PLUGINS_SCHEMA_URL,
    MANIFEST_FILENAME,
    SUPPORTED_SCHEMA_URLS,
    AgentPluginAuthor,
    AgentPluginManifest,
    AgentPluginManifestError,
    read_manifest,
)
from raven.agent_plugins.package import (
    AgentPluginPackage,
    PackageProblem,
    candidate_roots,
    discover_packages,
    is_contained,
    load_package,
    package_fingerprint,
)
from raven.agent_plugins.skills import (
    SKILL_FILENAME,
    ImportReport,
    PackageSkill,
    SkillProblem,
    discover_skills,
    drop_always_flag,
    import_package,
    validate_skill,
)

__all__ = [
    "AGENT_PLUGINS_SCHEMA_URL",
    "LEDGER_VERSION",
    "MANIFEST_FILENAME",
    "SUPPORTED_SCHEMA_URLS",
    "SKILL_FILENAME",
    "AgentPluginAuthor",
    "AgentPluginManifest",
    "AgentPluginManifestError",
    "AgentPluginPackage",
    "ImportReport",
    "LedgerEntry",
    "PackageLedger",
    "PackageProblem",
    "PackageSkill",
    "SkillProblem",
    "candidate_roots",
    "discover_packages",
    "discover_skills",
    "drop_always_flag",
    "import_package",
    "is_contained",
    "load_package",
    "package_fingerprint",
    "read_manifest",
    "validate_skill",
]
