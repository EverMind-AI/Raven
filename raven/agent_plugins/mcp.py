"""The MCP configuration of an Agent Plugin package.

A package may carry an ``mcp.json`` at its root: a closed object with a
``$schema`` and an ``mcpServers`` map (spec §7.2). The servers it names
carry the two component types the format defines for MCP -- a local process
over stdio, or a remote endpoint over Streamable HTTP or the legacy
HTTP+SSE transport.

This module reads that file, decides which entries are usable, and
composes the ones that survive into the MCP server configurations Raven
runs. Three rules from the specification shape the whole thing:

- the configuration belongs to the package, so its ``$schema`` must name the
  same Agent Plugins version the package manifest does; a mismatch disables
  MCP for that package and leaves its skills alone (§10.1, §7.2.2);
- one unusable server entry never takes the others with it -- the entry is
  skipped and reported, and the rest of the package still loads (§7.2.2);
- a server's paths stay inside the package, and the two placeholders the
  format defines are expanded in exactly the fields it names (§4.1, §9.2).

Starting a package's server is the operator's call, not this module's: the
servers are composed here and handed over, and the caller decides which
packages are allowed to run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from raven.agent_plugins.package import AgentPluginPackage, PackageProblem, is_contained
from raven.config.schema import MCPServerConfig

MCP_CONFIG_FILENAME = "mcp.json"
"""The MCP configuration's fixed location at the plugin root (spec §6.1)."""

MCP_SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
"""The canonical MCP configuration schema identifier of Agent Plugins 1.0.0."""

PACKAGE_SERVER_SEPARATOR = "__"
"""What separates a package name from a server name in a composed server name.

The package name cannot contain an underscore -- the manifest's name rules
allow only lowercase letters, digits, hyphens and periods -- so the first
occurrence of this separator is where the package name ends.
"""

_PLACEHOLDER_RE = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")

_SCHEMA_URL_RE = re.compile(r"^https://agent-plugins\.org/schemas/([^/]+)/([^/]+)\.schema\.json$")

_ROOT_PLACEHOLDER = "${PLUGIN_ROOT}"
_DATA_PLACEHOLDER = "${PLUGIN_DATA}"

# The two variables the client supplies itself. A server configuration may
# not name them: the client sets them after the configured environment is
# applied, and a plugin may not depend on what that ordering would do
# (spec §9.1, §9.2).
_ROOT_VAR = "PLUGIN_ROOT"
_DATA_VAR = "PLUGIN_DATA"
_RESERVED_ENV_NAMES = frozenset({_ROOT_VAR, _DATA_VAR})


class AgentPluginMcpError(ValueError):
    """The package's MCP configuration is unusable as a whole.

    MCP is then disabled for that package; its skills still load (§7.2.2).
    """


@dataclass(frozen=True)
class McpServerProblem:
    """One server entry that cannot be used, and why."""

    package: str
    server: str
    reason: str


@dataclass(frozen=True)
class PackageMcpReport:
    """What a package's MCP configuration yielded."""

    package: str
    servers: dict[str, MCPServerConfig] = field(default_factory=dict)
    problems: tuple[McpServerProblem, ...] = ()
    disabled: str = ""
    """Set when the package's MCP configuration was refused as a whole."""


def expand_placeholders(text: str, *, plugin_root: str, plugin_data: str) -> str:
    """Expand the two placeholders the format defines.

    One pass, so the text a replacement introduces is never rescanned
    (spec §9.2). Anything that is not one of the two placeholders stays
    literal.
    """
    values = {"PLUGIN_ROOT": plugin_root, "PLUGIN_DATA": plugin_data}
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], text)


def schema_version(url: str) -> str | None:
    """The Agent Plugins version a canonical schema identifier names.

    The manifest schema and the MCP configuration schema are two files of
    one version, so the version is what two identifiers are compared on
    (spec §10.1).
    """
    match = _SCHEMA_URL_RE.match(url)
    return match.group(1) if match else None


class _ServerBase(BaseModel):
    """The shape every server variant shares."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str


class StdioServer(_ServerBase):
    """A local process the client launches (spec §7.2.1)."""

    type: str = "stdio"
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""


class HttpServer(_ServerBase):
    """A remote endpoint (spec §7.2.1)."""

    type: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class AgentPluginMcpConfig(BaseModel):
    """The package's ``mcp.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_uri: str = Field(alias="$schema")
    mcp_servers: dict[str, Any] = Field(alias="mcpServers")


def read_mcp_config(path: Path) -> AgentPluginMcpConfig:
    """Read a package's ``mcp.json``.

    Raises :class:`AgentPluginMcpError` when the file is not a usable MCP
    configuration; the caller disables MCP for that package and carries on
    with its skills.
    """
    import json

    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentPluginMcpError(f"the MCP configuration is unreadable: {exc}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise AgentPluginMcpError(f"the MCP configuration is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AgentPluginMcpError("the MCP configuration must contain a JSON object")
    try:
        return AgentPluginMcpConfig.model_validate(raw)
    except ValidationError as exc:
        raise AgentPluginMcpError(
            f"the MCP configuration does not satisfy the Agent Plugins MCP schema: {exc}"
        ) from exc


def _single_token(command: str) -> bool:
    """Whether *command* is one executable token rather than a shell string."""
    return bool(command) and not any(char.isspace() for char in command)


def _resolve_command(
    command: str,
    *,
    plugin_root: Path,
) -> str:
    """The command to launch, after checking the plugin-relative form.

    A bare executable name is left for the platform's own search rules. A
    ``./`` path is resolved against the package root, has to stay inside it
    (spec §4.1, §7.2.1), and comes back absolute: the format anchors it to
    the root, not to whatever working directory the server was given.
    """
    if not _single_token(command):
        raise ValueError("the command must be a single executable token, not a shell command")
    if not command.startswith("./"):
        if "/" in command or "\\" in command:
            raise ValueError("the command must be a bare executable name or a plugin-relative path beginning with './'")
        return command
    candidate = plugin_root / command[2:]
    if not is_contained(plugin_root, candidate):
        raise ValueError("the plugin-relative command resolves outside the package root")
    return str(candidate)


def _resolve_cwd(
    raw: str,
    *,
    plugin_root: Path,
    plugin_data: Path,
) -> Path:
    """The working directory a stdio server runs in.

    Only the three forms the format names are accepted: a plugin-relative
    path, a ``${PLUGIN_ROOT}`` path, a ``${PLUGIN_DATA}`` path. Each has to
    stay inside the root it is anchored to (spec §4.1, §7.2.1).
    """
    if raw.startswith(_ROOT_PLACEHOLDER):
        anchor = plugin_root
        remainder = raw[len(_ROOT_PLACEHOLDER) :]
    elif raw.startswith(_DATA_PLACEHOLDER):
        anchor = plugin_data
        remainder = raw[len(_DATA_PLACEHOLDER) :]
    elif raw.startswith("./"):
        anchor = plugin_root
        remainder = raw[2:]
    else:
        raise ValueError(
            "the working directory must be a plugin-relative path, a ${PLUGIN_ROOT} path or a ${PLUGIN_DATA} path"
        )
    remainder = remainder.lstrip("/\\")
    candidate = anchor / remainder if remainder else anchor
    if not is_contained(anchor, candidate):
        raise ValueError("the working directory resolves outside the directory it is anchored to")
    return candidate


def _check_headers(headers: dict[str, str]) -> None:
    """Header names are case-insensitive and unique; values carry no CR/LF."""
    seen: set[str] = set()
    for name, value in headers.items():
        folded = name.casefold()
        if folded in seen:
            raise ValueError(f"the header {name!r} is spelled twice")
        seen.add(folded)
        if not name.strip():
            raise ValueError("a header name is empty")
        if "\r" in value or "\n" in value:
            raise ValueError(f"the header {name!r} carries a line break")


def _check_url(url: str) -> None:
    """The URL rules the format states for a remote endpoint."""
    from urllib.parse import urlsplit

    from raven.security.urls import require_https

    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise ValueError("the url must be absolute")
    if parts.scheme not in ("http", "https"):
        raise ValueError("the url must be http or https")
    if "@" in parts.netloc:
        raise ValueError("the url must not carry user information")
    if parts.fragment:
        raise ValueError("the url must not carry a fragment")
    require_https(url, what="the package MCP server url")


def _build_stdio(
    name: str,
    entry: StdioServer,
    *,
    package: AgentPluginPackage,
    plugin_data: Path,
) -> MCPServerConfig:
    """The Raven configuration for one stdio server."""
    plugin_root = package.root.resolve()
    command = _resolve_command(entry.command, plugin_root=plugin_root)

    reserved = sorted(_RESERVED_ENV_NAMES.intersection(entry.env))
    if reserved:
        raise ValueError(f"the environment must not define {', '.join(reserved)}")

    if entry.cwd:
        cwd = _resolve_cwd(entry.cwd, plugin_root=plugin_root, plugin_data=plugin_data)
    else:
        cwd = plugin_root

    root_text = str(plugin_root)
    data_text = str(plugin_data)
    args = [expand_placeholders(arg, plugin_root=root_text, plugin_data=data_text) for arg in entry.args]
    env = {
        key: expand_placeholders(value, plugin_root=root_text, plugin_data=data_text)
        for key, value in entry.env.items()
    }
    # The client provides the two reserved variables, and it provides them
    # last (§9.1, §9.2).
    env[_ROOT_VAR] = root_text
    env[_DATA_VAR] = data_text

    return MCPServerConfig(
        type="stdio",
        command=command,
        args=args,
        env=env,
        cwd=str(cwd),
    )


def _build_http(name: str, entry: HttpServer) -> MCPServerConfig:
    """The Raven configuration for one remote server."""
    _check_url(entry.url)
    _check_headers(entry.headers)
    transport = "streamableHttp" if entry.type == "streamable-http" else "sse"
    return MCPServerConfig(type=transport, url=entry.url, headers=dict(entry.headers))


def _server_entry(raw: Any) -> Any:
    """Validate one server entry against the closed variants."""
    if not isinstance(raw, dict):
        raise ValueError("a server entry must be a JSON object")
    kind = raw.get("type")
    if kind == "stdio":
        return StdioServer.model_validate(raw)
    if kind in ("streamable-http", "sse"):
        return HttpServer.model_validate(raw)
    raise ValueError(f"the server type {kind!r} is not one the format defines")


def package_mcp_report(
    package: AgentPluginPackage,
    *,
    plugin_data: Path,
) -> PackageMcpReport:
    """What the package's MCP configuration yields.

    An unusable configuration disables MCP for the package; an unusable
    server entry skips that entry alone.
    """
    config_path = package.root / MCP_CONFIG_FILENAME
    if not config_path.is_file():
        return PackageMcpReport(package=package.name)

    try:
        config = read_mcp_config(config_path)
    except AgentPluginMcpError as exc:
        return PackageMcpReport(package=package.name, disabled=str(exc))

    mcp_version = schema_version(config.schema_uri)
    manifest_version = schema_version(package.manifest.schema_uri)
    if mcp_version is None or not config.schema_uri.endswith("/mcp.schema.json"):
        return PackageMcpReport(
            package=package.name,
            disabled=(
                f"the MCP configuration names {config.schema_uri!r}, which is not the "
                "canonical Agent Plugins MCP configuration schema"
            ),
        )
    if mcp_version != manifest_version:
        return PackageMcpReport(
            package=package.name,
            disabled=(
                f"the MCP configuration declares Agent Plugins version {mcp_version!r}, "
                f"but the package manifest declares {manifest_version!r}"
            ),
        )

    plugin_data = Path(plugin_data)
    servers: dict[str, MCPServerConfig] = {}
    problems: list[McpServerProblem] = []

    for name, raw in config.mcp_servers.items():
        try:
            entry = _server_entry(raw)
        except (ValidationError, ValueError) as exc:
            problems.append(McpServerProblem(package.name, str(name), str(exc)))
            continue
        try:
            if isinstance(entry, StdioServer):
                composed = _build_stdio(name, entry, package=package, plugin_data=plugin_data)
            else:
                composed = _build_http(name, entry)
        except ValueError as exc:
            problems.append(McpServerProblem(package.name, str(name), str(exc)))
            continue
        servers[f"{package.name}{PACKAGE_SERVER_SEPARATOR}{name}"] = composed

    return PackageMcpReport(package=package.name, servers=servers, problems=tuple(problems))


def package_mcp_servers(
    packages: Iterable[AgentPluginPackage],
    *,
    allowed: Iterable[str],
    plugin_data_root: Path,
) -> tuple[dict[str, MCPServerConfig], list[PackageProblem], list[McpServerProblem]]:
    """The MCP servers of the packages the operator allowed to run.

    A package outside *allowed* is parsed and reported, not run: starting a
    package's server is a process on the operator's machine, and that is
    their call to make.
    """
    permitted = {str(name) for name in allowed}
    servers: dict[str, MCPServerConfig] = {}
    problems: list[PackageProblem] = []
    entries: list[McpServerProblem] = []

    for package in packages:
        data_dir = Path(plugin_data_root) / package.name
        if package.name in permitted:
            # The client's persistent directory for this package, created
            # before any process of it starts (spec §9.1).
            data_dir.mkdir(parents=True, exist_ok=True)
        report = package_mcp_report(package, plugin_data=data_dir)
        if report.disabled:
            problems.append(PackageProblem(str(package.root), report.disabled))
            continue
        entries.extend(report.problems)
        if package.name not in permitted:
            continue
        servers.update(report.servers)

    return servers, problems, entries


def package_mcp_layer(
    paths: Iterable[str | Path],
    *,
    allowed: Iterable[str],
    disabled: Iterable[str] = (),
    plugin_data_root: Path,
) -> tuple[dict[str, MCPServerConfig], list[str]]:
    """The MCP server layer the portable packages contribute.

    Returns the composed servers and the problems worth reporting. The
    layer is empty when no package is allowed to run.
    """
    from raven.agent_plugins.package import discover_packages

    packages, problems = discover_packages(paths, disabled=disabled)
    servers, mcp_problems, entries = package_mcp_servers(
        packages,
        allowed=allowed,
        plugin_data_root=plugin_data_root,
    )
    notes = [f"{problem.path}: {problem.reason}" for problem in problems]
    notes += [f"{problem.path}: {problem.reason}" for problem in mcp_problems]
    notes += [f"{problem.package}/{problem.server}: {problem.reason}" for problem in entries]
    return servers, notes


__all__ = [
    "MCP_CONFIG_FILENAME",
    "MCP_SCHEMA_URL",
    "PACKAGE_SERVER_SEPARATOR",
    "AgentPluginMcpConfig",
    "AgentPluginMcpError",
    "HttpServer",
    "McpServerProblem",
    "PackageMcpReport",
    "StdioServer",
    "expand_placeholders",
    "package_mcp_report",
    "package_mcp_servers",
    "read_mcp_config",
]
