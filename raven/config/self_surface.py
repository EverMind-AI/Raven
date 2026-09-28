"""The self-configuration surface: what Raven may read and change about itself.

``raven_config`` (the agent's tool) and the permission gate both read this
catalog. Each :class:`Setting` names one dotted path as ``config.json`` spells
it, what kind of value it takes, which writer owns it, and -- the part that is
easy to get wrong -- when a change to it actually takes effect in the process
that is serving. The effect is part of the entry rather than prose in a skill
or a tool description, because it is a fact about the code: a live reader in
``config/live.py``, one of the runtime's three doors, a generation swap, or a
whole-process restart. ``tests/test_config_self_surface.py`` holds every entry
against the schema and every next-turn claim against a real reader, so the
catalog cannot promise what the runtime does not do.

Writers are named, not called, here. ``raw`` is :func:`write_value`: a
spelling-aware, locked read-modify-write that refuses a candidate the schema
rejects. The others are the RPC methods the settings page already uses
(``settings.set``, ``config.set``, ``channels.configure``, ``subagents.*``),
routed by the tool through whatever dispatcher the entrance lent it, so the
agent and the page change a setting through one path with one set of checks.

Deliberately absent: anything that composes a command line (an MCP server's
``command``/``env``, a sub-agent's ``command``) -- that is arbitrary execution
under another name, and the ``plugin`` tool already installs MCP servers from
the trusted catalog. Secrets are listed so their presence can be reported, but
their values never travel through a tool call.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.alias_generators import to_camel, to_snake

from raven.config.loader import EXTENSION_KEYS, get_config_path, read_raw_or_raise
from raven.utils.atomic_io import atomic_update


class Effect(StrEnum):
    """When a written value starts to change what the running process does."""

    NEXT_TURN = "next_turn"
    IMMEDIATE = "immediate"
    RELOAD = "reload"
    RESTART = "restart"
    MEMORY_SERVER = "memory_server"
    INERT = "inert"


EFFECT_TEXT: dict[Effect, str] = {
    Effect.NEXT_TURN: "takes effect from the next turn; nothing to restart",
    Effect.IMMEDIATE: "applied at once by the running process",
    Effect.RELOAD: (
        "needs a gateway reload (a generation swap: the process stays up, turns in flight finish first); "
        "outside the gateway it needs a restart"
    ),
    Effect.RESTART: "needs the whole Raven process restarted",
    Effect.MEMORY_SERVER: "applied by restarting the memory server, which the writer does itself",
    Effect.INERT: "accepted by the config file but nothing reads it, so changing it does nothing",
}

#: The effects the tool can be asked to apply with its ``restart`` action.
PENDING_EFFECTS = (Effect.RELOAD, Effect.RESTART)


@dataclass(frozen=True)
class Setting:
    """One configurable path.

    ``path`` is camelCase, the way the file is written; ``*`` stands for one
    segment the caller names (a provider, a channel, a sub-agent). ``sensitive``
    is the reason a change deserves a second look -- every write is confirmed
    by the user, and this sentence is put on the confirmation.
    """

    path: str
    summary: str
    kind: str
    effect: Effect
    writer: str = "raw"
    choices: tuple[str, ...] = ()
    low: float | None = None
    high: float | None = None
    nullable: bool = False
    secret: bool = False
    sensitive: str = ""
    note: str = ""
    #: For a setting that names a block (a model pin), the keys of it that are
    #: the setting; a read shows only these, never the rest of the block.
    keys: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "summary": self.summary,
            "type": self.kind,
            "takes_effect": EFFECT_TEXT[self.effect],
        }
        if self.choices:
            out["choices"] = list(self.choices)
        if self.low is not None or self.high is not None:
            out["range"] = [self.low, self.high]
        if self.nullable:
            out["nullable"] = True
        if self.secret:
            out["secret"] = True
        if self.sensitive:
            out["sensitive"] = self.sensitive
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True)
class Section:
    name: str
    summary: str
    settings: tuple[Setting, ...] = field(default_factory=tuple)


_REASONING = ("minimal", "low", "medium", "high")
_WEB_SEARCH = ("serper", "anysearch", "serpapi", "tavily", "exa", "brave", "firecrawl", "serply")
_WEB_FETCH = ("jina", "anysearch", "tavily", "exa", "firecrawl")
_WEB_VENDORS = ("serper", "anysearch", "serpapi", "jina", "tavily", "exa", "brave", "firecrawl", "serply")

_E = Effect

SECTIONS: tuple[Section, ...] = (
    Section(
        "model",
        "Which model answers, and how it is driven",
        (
            Setting(
                "agents.defaults.model",
                "Default model for new conversations, with the provider whose credential serves it",
                "model_ref",
                _E.IMMEDIATE,
                writer="config.model",
                note='value is {"provider": "<provider>", "model": "<model id>"}',
            ),
            Setting(
                "agents.defaults.reasoningEffort",
                "Reasoning effort sent with each model call",
                "enum",
                _E.NEXT_TURN,
                writer="settings",
                choices=_REASONING,
            ),
            Setting(
                "agents.defaults.maxToolIterations",
                "Most tool calls one turn may make",
                "int",
                _E.NEXT_TURN,
                writer="settings",
                low=1,
                high=200,
            ),
            Setting(
                "agents.defaults.contextWindowTokens",
                "Context window override; null uses the model's own",
                "int",
                _E.NEXT_TURN,
                writer="settings",
                low=1024,
                high=100_000_000,
                nullable=True,
            ),
            Setting(
                "agents.defaults.enablePersonalization",
                "Personalization flow (classify, ask, execute, learn)",
                "bool",
                _E.NEXT_TURN,
                writer="settings",
            ),
            Setting("agents.defaults.temperature", "Sampling temperature", "float", _E.RELOAD, low=0, high=2),
            Setting("agents.defaults.llmCallTimeout", "Seconds one model call may take", "int", _E.RELOAD, low=1),
            Setting(
                "agents.defaults.maxConcurrentSubagents",
                "Sub-agents that may run at once",
                "int",
                _E.RELOAD,
                low=1,
            ),
            Setting(
                "agents.defaults.maxSubagentSpawnsPerHour",
                "Sub-agent dispatches allowed per hour",
                "int",
                _E.RELOAD,
                low=1,
            ),
            Setting(
                "agents.defaults.workspace",
                "Raven's home workspace directory",
                "str",
                _E.RESTART,
                sensitive="moves where sessions, memory and files live",
            ),
            Setting(
                "routing.profile",
                "Model-routing profile (only with the ecoclaw router)",
                "enum",
                _E.NEXT_TURN,
                choices=("best", "balanced", "eco"),
            ),
            Setting("routing.enabled", "Automatic model routing", "bool", _E.RELOAD),
        ),
    ),
    Section(
        "providers",
        "Model providers and their endpoints (credentials are reported, never taken)",
        (
            Setting(
                "providers.*.apiKey",
                "The provider's API key",
                "str",
                _E.NEXT_TURN,
                secret=True,
                note="set it in Settings > Models; the gateway picks a new key up on the next call",
            ),
            Setting(
                "providers.*.apiBase",
                "Endpoint URL override",
                "str",
                _E.NEXT_TURN,
                writer="model.fields",
                nullable=True,
                note="a raven serve or TUI process keeps its startup default binding until restarted",
            ),
            Setting(
                "providers.*.models",
                "Model ids offered in the picker for this provider",
                "list",
                _E.NEXT_TURN,
            ),
        ),
    ),
    Section(
        "tools",
        "The agent's own tools",
        (
            Setting(
                "tools.disabledTools",
                "Tools withheld from the model",
                "list",
                _E.NEXT_TURN,
                writer="settings",
                note="the list replaces the stored one; read it first and send the whole list back",
            ),
            Setting(
                "tools.exec.timeout", "Seconds a shell command may run", "int", _E.NEXT_TURN, writer="settings", low=5
            ),
            Setting(
                "tools.exec.extraDenyPatterns",
                "Extra regexes a shell command may not match",
                "list",
                _E.NEXT_TURN,
                sensitive="removing a pattern loosens what shell commands may run",
            ),
            Setting("tools.exec.pathAppend", "Directories appended to PATH for shell commands", "str", _E.RELOAD),
            Setting(
                "tools.web.search.provider",
                "Web search vendor",
                "enum",
                _E.NEXT_TURN,
                writer="settings",
                choices=_WEB_SEARCH,
            ),
            Setting(
                "tools.web.fetch.provider",
                "Web page fetch vendor",
                "enum",
                _E.NEXT_TURN,
                writer="settings",
                choices=_WEB_FETCH,
            ),
            *(
                Setting(
                    f"tools.web.providers.{vendor}.apiKey",
                    f"{vendor} API key",
                    "str",
                    _E.NEXT_TURN,
                    secret=True,
                    note="set it in Settings > Tools",
                )
                for vendor in _WEB_VENDORS
            ),
            Setting("tools.web.proxy", "HTTP/SOCKS proxy for web tools", "str", _E.RELOAD, nullable=True),
            Setting("tools.web.search.images", "Offer the image search tool", "bool", _E.RELOAD),
            *(
                Setting(
                    f"tools.media.{medium}.model",
                    f"Model for {medium} generation",
                    "str",
                    _E.NEXT_TURN,
                    writer="settings" if medium == "image" else "raw",
                )
                for medium in ("image", "speech", "video")
            ),
            Setting(
                "tools.media.image.quality",
                "Image quality",
                "enum",
                _E.NEXT_TURN,
                writer="settings",
                choices=("", "low", "medium", "high"),
            ),
            *(
                Setting(
                    f"tools.media.{medium}.apiKey",
                    f"API key for {medium} generation",
                    "str",
                    _E.NEXT_TURN,
                    secret=True,
                    note="set it in Settings > Tools",
                )
                for medium in ("image", "speech", "video")
            ),
            Setting("tools.media.proxy", "Proxy for media API calls", "str", _E.RELOAD, nullable=True),
            Setting("tools.askUser.timeout", "Seconds a question to the user waits", "int", _E.RELOAD, low=1),
            Setting("tools.toolSearch.enabled", "Defer rarely used tools behind tool search", "bool", _E.RELOAD),
            Setting(
                "tools.mcpServers.*.enabled",
                "Whether a configured MCP server is connected",
                "bool",
                _E.NEXT_TURN,
                note="new MCP servers are added with the plugin tool, not here",
            ),
            Setting(
                "tools.restrictToWorkspace",
                "Confine file and shell tools to the workspace",
                "bool",
                _E.RELOAD,
                sensitive="turning it off lets tools reach files outside the workspace",
            ),
            Setting(
                "tools.sandbox.backend",
                "Sandbox for shell commands",
                "enum",
                _E.RELOAD,
                choices=("none", "auto", "boxlite"),
                sensitive="changes whether shell commands run isolated",
            ),
            Setting(
                "tools.connectionAdd",
                "Let the agent register remote machines",
                "bool",
                _E.RELOAD,
                sensitive="lets the agent write the owner's ssh config",
            ),
            Setting(
                "tools.browser.headfulOnAgentUse",
                "Show the browser window when the agent drives it",
                "bool",
                _E.RESTART,
            ),
            Setting(
                "tools.web.search.maxResults",
                "Results per web search",
                "int",
                _E.INERT,
                writer="inert",
            ),
        ),
    ),
    Section(
        "channels",
        "IM channels the gateway serves; describe channels.<name> for one channel's fields",
        (
            Setting(
                "channels.*.enabled",
                "Whether the channel is connected",
                "bool",
                _E.IMMEDIATE,
                writer="channels",
                note="the gateway starts or stops the adapter at once",
            ),
            Setting(
                "channels.*.allowFrom",
                "Who may talk to Raven on this channel; ['*'] means anyone",
                "list",
                _E.IMMEDIATE,
                writer="channels",
                sensitive="widening it lets more people instruct Raven",
            ),
            Setting(
                "channels.sendProgress",
                "Stream progress text to channels",
                "bool",
                _E.INERT,
                writer="inert",
                note="the gateway does not read it; only `raven agent -m` does",
            ),
            Setting(
                "channels.sendToolHints",
                "Stream tool-call hints to channels",
                "bool",
                _E.INERT,
                writer="inert",
                note="the gateway does not read it; only `raven agent -m` does",
            ),
        ),
    ),
    Section(
        "memory",
        "Long-term memory and its models",
        (
            Setting(
                "memory.memoryTopK",
                "Memories recalled into each turn",
                "int",
                _E.NEXT_TURN,
                writer="settings",
                low=1,
                high=50,
            ),
            Setting("memory.backend", "Memory backend plugin", "str", _E.RELOAD, nullable=True),
            Setting(
                "embedding",
                "Embedding model for memory and the knowledge base",
                "model_ref",
                _E.MEMORY_SERVER,
                writer="settings",
                sensitive="a different embedding model invalidates every vector already stored",
                note='value is {"provider": "<provider>", "model": "<model id>"}',
                keys=("model", "provider"),
            ),
        ),
    ),
    Section(
        "skills",
        "Skill discovery and selection",
        (
            Setting(
                "skillForge.blocklist",
                "Skills never offered",
                "list",
                _E.NEXT_TURN,
                writer="settings",
                note="the list replaces the stored one; read it first",
            ),
            Setting("skillForge.enabled", "Mount the extra local skill directories", "bool", _E.RELOAD),
            Setting(
                "skillForge.autoInstall",
                "Installing a Skill Hub skill the router picked",
                "enum",
                _E.RELOAD,
                choices=("auto", "prompt", "off"),
            ),
            Setting("skillForge.router.topK", "Skills the router offers per turn", "int", _E.RELOAD, low=1),
            Setting("skillForge.llmGateEnabled", "Let a model pick among candidate skills", "bool", _E.RELOAD),
            Setting(
                "skillForge",
                "Model that picks among candidate skills",
                "pin",
                _E.NEXT_TURN,
                writer="settings",
                note='value is {"llmGateModel": "<model>", "llmGateProvider": "<provider>"}',
                keys=("llmGateModel", "llmGateProvider"),
            ),
        ),
    ),
    Section(
        "context",
        "How history is curated into the prompt",
        (
            Setting(
                "context",
                "Model that curates long history",
                "pin",
                _E.NEXT_TURN,
                writer="settings",
                note='value is {"curatorModel": "<model>", "curatorProvider": "<provider>"}',
                keys=("curatorModel", "curatorProvider"),
            ),
            Setting("context.protectFirstN", "Leading messages never archived", "int", _E.RELOAD, low=0),
            Setting("context.pinnedSkillIds", "Skills kept in context once read", "list", _E.RELOAD),
        ),
    ),
    Section(
        "proactive",
        "Raven acting on its own: sentinel nudges, heartbeat, cron",
        (
            Setting(
                "sentinel.enabled",
                "Sentinel (proactive nudges)",
                "bool",
                _E.RESTART,
                note="a gateway reload does not rebuild the sentinel",
            ),
            Setting("sentinel.nudgePolicy.maxNudgesPerHour", "Nudges allowed per hour", "int", _E.RESTART, low=0),
            Setting("sentinel.nudgePolicy.maxNudgesPerDay", "Nudges allowed per day", "int", _E.RESTART, low=0),
            Setting("sentinel.taskDiscoveryEnabled", "Daily task discovery", "bool", _E.RESTART),
            Setting("gateway.heartbeat.enabled", "Periodic heartbeat check of HEARTBEAT.md", "bool", _E.RELOAD),
            Setting("gateway.heartbeat.intervalS", "Seconds between heartbeats", "int", _E.RELOAD, low=60),
            Setting("cron.notifyMissed", "Tell the user about cron jobs missed while down", "bool", _E.RELOAD),
            Setting(
                "cron.defaultTimezone",
                "Default timezone for cron jobs",
                "str",
                _E.INERT,
                writer="inert",
                note="nothing reads it; a job without a timezone uses the host's",
            ),
        ),
    ),
    Section(
        "playbooks",
        "Captured multi-step workflows",
        (
            Setting(
                "playbooks.disabled",
                "Playbooks switched off",
                "list",
                _E.NEXT_TURN,
                note="the list replaces the stored one; read it first",
            ),
            Setting("playbooks.enabled", "The playbook library", "bool", _E.RELOAD),
        ),
    ),
    Section(
        "subagents",
        "Agents Raven can dispatch work to; describe subagents.<name> for one agent",
        (
            Setting(
                "subagents.*.description",
                "What the agent is good at, as the dispatching model reads it",
                "str",
                _E.IMMEDIATE,
                writer="subagents",
                note="the built-in Raven row's description is fixed",
            ),
            Setting(
                "subagents.*.enabled",
                "Whether the agent is on the roster",
                "bool",
                _E.IMMEDIATE,
                writer="subagents",
            ),
            Setting(
                "subagents.*.model",
                "Default model the agent runs on",
                "model_ref",
                _E.IMMEDIATE,
                writer="subagents",
                note=(
                    "for an external agent the value is one of its model_choices (a plain id); for the built-in "
                    'row and agents that borrow Raven\'s model it is {"provider", "model"}; null clears it'
                ),
            ),
        ),
    ),
    Section(
        "observability",
        "Tracing and session housekeeping",
        (
            Setting("tracing.enabled", "Record traces", "bool", _E.NEXT_TURN),
            Setting("tracing.previewLen", "Characters kept per traced payload", "int", _E.NEXT_TURN, low=0),
            Setting("sessionTitle.enabled", "Model-written session titles", "bool", _E.NEXT_TURN),
            Setting(
                "sessions.autoArchiveAfterDays",
                "Archive idle sessions after this many days; null never",
                "int",
                _E.NEXT_TURN,
                writer="settings",
                low=1,
                high=3650,
                nullable=True,
            ),
        ),
    ),
    Section(
        "gateway",
        "The long-running gateway process",
        (
            Setting("gateway.page.enabled", "Serve the web page from the gateway", "bool", _E.RELOAD),
            Setting("gateway.shutdownGrace", "Seconds in-flight turns get during a reload", "float", _E.RELOAD, low=0),
            Setting("gateway.userPool", "Concurrent user turns", "int", _E.RELOAD, low=0),
            Setting("gateway.port", "Gateway health port", "int", _E.RESTART, low=1, high=65535),
            Setting(
                "gateway.log.level",
                "Gateway log level",
                "enum",
                _E.RESTART,
                choices=("TRACE", "DEBUG", "INFO", "WARNING", "ERROR"),
            ),
        ),
    ),
    Section(
        "security",
        "How tool calls are approved",
        (
            Setting(
                "permissions.mode",
                "Approval mode for tool calls",
                "enum",
                _E.NEXT_TURN,
                writer="settings",
                choices=("ask", "smart", "full"),
                sensitive="'full' runs every tool call without asking",
            ),
            Setting(
                "permissions.judgeModel",
                "Model that reviews calls in smart mode",
                "str",
                _E.NEXT_TURN,
                nullable=True,
            ),
        ),
    ),
    Section(
        "general",
        "Everything else",
        (
            Setting(
                "language",
                "Interface language of Raven's own clients",
                "enum",
                _E.NEXT_TURN,
                writer="settings",
                choices=("en", "zh"),
            ),
        ),
    ),
)


def sections() -> tuple[Section, ...]:
    return SECTIONS


def all_settings() -> Iterable[Setting]:
    for section in SECTIONS:
        yield from section.settings


def _matches(pattern: str, path: str) -> dict[str, str] | None:
    """The wildcard bindings when ``path`` is an instance of ``pattern``."""
    want, got = pattern.split("."), path.split(".")
    if len(want) != len(got):
        return None
    bound: dict[str, str] = {}
    for w, g in zip(want, got, strict=True):
        if w == "*":
            if not g:
                return None
            bound[str(len(bound))] = g
        elif w != g:
            return None
    return bound


def find(path: str) -> tuple[Setting, list[str]] | None:
    """The setting ``path`` names and the segments standing for its wildcards.

    An exact entry wins over a wildcard one, so ``tools.web.providers.jina.apiKey``
    is its own entry rather than an instance of some broader pattern.
    """
    for setting in all_settings():
        if setting.path == path:
            return setting, []
    for setting in all_settings():
        if "*" in setting.path and (bound := _matches(setting.path, path)) is not None:
            return setting, list(bound.values())
    return None


def section_of(path: str) -> Section | None:
    for section in SECTIONS:
        if section.name == path:
            return section
    return None


def check_value(setting: Setting, value: Any) -> Any:
    """``value`` if it fits the setting's kind, else ``ValueError`` saying why."""
    if value is None:
        if setting.nullable:
            return None
        raise ValueError(f"{setting.path} cannot be null")
    kind = setting.kind
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{setting.path} takes true or false")
    elif kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{setting.path} takes a number")
        if kind == "int" and not float(value).is_integer():
            raise ValueError(f"{setting.path} takes a whole number")
        value = int(value) if kind == "int" else float(value)
        if setting.low is not None and value < setting.low:
            raise ValueError(f"{setting.path} must be at least {setting.low:g}")
        if setting.high is not None and value > setting.high:
            raise ValueError(f"{setting.path} must be at most {setting.high:g}")
    elif kind == "str":
        if not isinstance(value, str):
            raise ValueError(f"{setting.path} takes a string")
    elif kind == "enum":
        if value not in setting.choices:
            raise ValueError(f"{setting.path} takes one of {list(setting.choices)}")
    elif kind == "list":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"{setting.path} takes a list of strings")
    elif kind == "model_ref":
        if not isinstance(value, dict | str):
            raise ValueError(f'{setting.path} takes {{"provider": ..., "model": ...}}')
        if isinstance(value, dict) and not (isinstance(value.get("model"), str) and value["model"].strip()):
            raise ValueError(f"{setting.path} needs a model")
    elif kind == "pin":
        if not isinstance(value, dict):
            raise ValueError(f"{setting.path} takes an object; see its note")
    return value


def change_line(params: dict[str, Any]) -> str:
    """One sentence for a confirmation prompt about a ``raven_config`` call."""
    action = str(params.get("action") or "")
    path = str(params.get("path") or "")
    if action == "restart":
        target = str(params.get("value") or "reload")
        return f"Restart Raven ({target}) so pending configuration changes take effect"
    if action == "add":
        return f"Add to Raven's configuration at {path}: {_short(params.get('value'))}"
    found = find(path)
    tail = ""
    if found is not None:
        setting = found[0]
        tail = f" ({EFFECT_TEXT[setting.effect]})"
        if setting.sensitive:
            tail += f". Note: {setting.sensitive}"
    if action == "unset":
        return f"Reset {path} to its default{tail}"
    return f"Change {path} to {_short(params.get('value'))}{tail}"


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= 120 else text[:117] + "..."


# ---------------------------------------------------------------------------
# Reading and writing the file
# ---------------------------------------------------------------------------


def _spelled(node: dict[str, Any], name: str) -> str:
    """The spelling ``node`` already uses for ``name``, else ``name`` itself.

    Every block validates under both camelCase and snake_case, and the models
    forbid extras, so writing the other spelling beside an existing key makes
    the whole config stop loading.
    """
    if name in node:
        return name
    for alias in (to_camel(name), to_snake(name)):
        if alias in node:
            return alias
    return name


def read_raw(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or get_config_path()
    if not path.exists():
        return {}
    return read_raw_or_raise(path)


def lookup(data: dict[str, Any], path: str) -> tuple[bool, Any]:
    """``(present, value)`` for ``path`` in a raw config dict, either spelling."""
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict):
            return False, None
        key = _spelled(node, part)
        if key not in node:
            return False, None
        node = node[key]
    return True, node


_SECRET_MARKERS = ("apikey", "api_key", "token", "secret", "password", "credential")


def redacted(value: Any) -> Any:
    """``value`` with anything keyed like a credential replaced by set / not set."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if any(marker in str(key).lower().replace("-", "_") for marker in _SECRET_MARKERS):
                out[key] = "set" if item else "not set"
            else:
                out[key] = redacted(item)
        return out
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return value


def concrete_paths(setting: Setting, data: dict[str, Any]) -> list[str]:
    """The paths ``setting`` stands for in ``data``: its own, or one per instance its wildcard names there."""
    if "*" not in setting.path:
        return [setting.path]
    head, _, tail = setting.path.partition(".*")
    present, node = lookup(data, head)
    if not present or not isinstance(node, dict) or "*" in tail:
        return []
    return [f"{head}.{name}{tail}" for name in node]


def instance_of(setting: Setting, path: str) -> str:
    """The prefix of ``path`` that names the instance a wildcard stands for."""
    parts = setting.path.split(".")
    return ".".join(path.split(".")[: parts.index("*") + 1])


def default_of(path: str) -> Any:
    """The schema default for ``path``, or ``None`` when it has none to give."""
    from raven.config.raven import RavenConfig

    parts = path.split(".")
    node: Any = RavenConfig()
    first = parts[0]
    if first in EXTENSION_KEYS or to_camel(first) in EXTENSION_KEYS:
        pass
    else:
        node = node.base
    for part in parts:
        node = _child(node, part)
        if node is None:
            return None
    if isinstance(node, BaseModel):
        return node.model_dump(by_alias=True, mode="json")
    return node


def _child(node: Any, part: str) -> Any:
    if isinstance(node, BaseModel):
        fields = type(node).model_fields
        snake = to_snake(part)
        if snake in fields:
            return getattr(node, snake, None)
        for name, info in fields.items():
            if info.alias == part:
                return getattr(node, name, None)
        return None
    if isinstance(node, dict):
        return node.get(part)
    return None


def validation_error(data: dict[str, Any]) -> str | None:
    """Why ``data`` would not load as a config, or ``None`` when it would."""
    from pydantic import ValidationError

    from raven.config.raven import RavenConfig
    from raven.config.schema import Config

    base = copy.deepcopy(data)
    extensions = {key: base.pop(key) for key in list(base) if key in EXTENSION_KEYS}
    try:
        cfg = Config.model_validate(base)
        RavenConfig(base=cfg, **{k: v for k, v in extensions.items() if v is not None})
    except ValidationError as exc:
        return str(exc)
    except (TypeError, ValueError) as exc:
        return str(exc)
    return None


def write_value(path: str, value: Any, *, config_path: Path | None = None) -> Any:
    """Set one dotted path in ``config.json``; returns the value it replaced.

    A candidate that fails schema validation is refused and nothing is written
    -- unless the file already failed before this write, in which case the
    write cannot be what broke it and refusing would only strand the user.
    """
    return _mutate(path, value, remove=False, config_path=config_path)


def remove_value(path: str, *, config_path: Path | None = None) -> Any:
    """Drop one dotted path so its schema default applies again."""
    return _mutate(path, None, remove=True, config_path=config_path)


def _mutate(path: str, value: Any, *, remove: bool, config_path: Path | None) -> Any:
    target = config_path or get_config_path()
    parts = path.split(".")

    def _apply(_text: str | None) -> tuple[str | None, Any]:
        data = read_raw_or_raise(target) if target.exists() else {}
        before = copy.deepcopy(data)
        node = data
        for part in parts[:-1]:
            key = _spelled(node, part)
            child = node.get(key)
            if child is None:
                if remove:
                    return None, None
                child = node[key] = {}
            if not isinstance(child, dict):
                raise ValueError(f"{path}: {key} is not an object in config.json")
            node = child
        leaf = _spelled(node, parts[-1])
        previous = node.get(leaf)
        if remove:
            if leaf not in node:
                return None, None
            del node[leaf]
        else:
            node[leaf] = value
        why = validation_error(data)
        if why is not None and validation_error(before) is None:
            raise ValueError(f"{path}: the config would not load with this value: {why}")
        return json.dumps(data, indent=2, ensure_ascii=False), previous

    return atomic_update(target, _apply)


__all__ = [
    "EFFECT_TEXT",
    "PENDING_EFFECTS",
    "SECTIONS",
    "Effect",
    "Section",
    "Setting",
    "all_settings",
    "change_line",
    "check_value",
    "default_of",
    "find",
    "lookup",
    "instance_of",
    "read_raw",
    "redacted",
    "remove_value",
    "section_of",
    "validation_error",
    "write_value",
]
