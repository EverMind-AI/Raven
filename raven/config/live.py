"""Config values that are allowed to change while the process runs.

Most of what a loop reads out of ``config.json`` is settled when it is built: a
provider is constructed, MCP servers are connected, a workspace root is resolved.
Those are expensive or stateful, and re-doing them mid-turn is a different
feature with different risks.

A preference is not like that. "Do not offer me this tool" is a sentence about
the *next* request, and reading it once at startup makes it a sentence about the
next restart -- so a switch on the page changed a file and nothing else, and the
only way to be believed was to quit. This module is the small amount of
machinery that closes that gap, and it is deliberately the only thing in it:

- **one file, compared by content.** A parse happens only when the bytes
  actually changed. Every writer is covered by construction, because what is
  watched is the file rather than any particular writer -- the page, the TUI,
  another process, a hand edit.
- **failures keep the last good answer.** A config being rewritten is briefly
  unparseable, and a torn read must not empty the answer: withholding every tool
  for one turn because a file was mid-write is worse than answering with the
  value from a second ago.

What must NOT be read through here: anything whose change implies work rather
than a different answer. Constructing a provider imports litellm (seconds),
connecting an MCP server touches the network, and moving the workspace root
mid-turn changes what a path means halfway through a tool call. Those keep their
explicit apply paths (``apply_mcp_config``, ``apply_agents``) where the cost is
visible at the call site.
"""

from __future__ import annotations

import json
import weakref
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

__all__ = [
    "LiveConfig",
    "default_model",
    "disabled_playbook_names",
    "disabled_tool_names",
    "exec_extra_deny_patterns",
    "mcp_server_configs",
    "media_tool_config",
    "permissions_config",
    "routing_profile",
    "web_search_key",
]


class LiveConfig:
    """One config file, re-parsed only when its bytes change.

    Not a cache with a timeout: a timeout answers staleness with a delay, and the
    question here has an exact answer available for the price of one small read.

    The comparison is the file's bytes, not a ``stat`` fingerprint:
    ``(mtime_ns, size)`` is not a fingerprint of the content, because two writes
    of equal length land on the same pair wherever the clock granularity is
    coarser than the gap between them, and the second one is then invisible for
    good. Reading a config-sized file is cheap next to the LLM call it precedes;
    being wrong about it is not.
    """

    def __init__(self, path: Path | None = None):
        self._path = path
        self._bytes: bytes | None = None
        self._raw: dict[str, Any] = {}
        self._loaded = False
        # The last slice each reader successfully admitted, keyed by reader
        # slot. The slice-level form of the torn-read rule above: a candidate
        # the schema rejects keeps the last admitted answer serving, and a
        # section that leaves the file forgets it (see ``_admit``).
        self._admitted: dict[str, Any] = {}

    def path(self) -> Path:
        """Resolved per read, not captured: the tests and ``raven --config`` move
        it, and a path captured at construction outlives the move."""
        if self._path is not None:
            return self._path
        from raven.config.loader import get_config_path

        return get_config_path()

    def raw(self) -> dict[str, Any]:
        """The file as parsed JSON, re-parsing it only if its bytes changed."""
        path = self.path()
        try:
            data = path.read_bytes()
        except OSError:
            # Absent is a real answer and a stable one: no file, no preferences.
            # Distinguished from a failed parse below, which keeps what it had.
            self._bytes, self._raw, self._loaded = None, {}, True
            return self._raw
        if self._loaded and data == self._bytes:
            return self._raw
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - a torn read is not worth a turn
            # These exact bytes are remembered so the same broken file is not
            # re-parsed every turn, while ``_raw`` keeps the last good answer: a
            # config is briefly unparseable every time something rewrites it, and
            # that instant must not change any answer. The write that lands next
            # differs in content, so it is seen.
            logger.debug("live config: {} is not readable right now ({})", path, exc)
            self._bytes = data
            self._loaded = True
            return self._raw
        self._bytes = data
        self._raw = parsed if isinstance(parsed, dict) else {}
        self._loaded = True
        return self._raw

    def get(self, dotted: str, default: Any = None) -> Any:
        """One dotted key, the way the settings surface names them."""
        node: Any = self.raw()
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def disabled_playbook_names(live: LiveConfig) -> frozenset[str]:
    """The playbooks the user has switched off, read live.

    One spelling, unlike :func:`disabled_tool_names`: nothing writes this list
    from a settings page, so ``playbooks.disabled`` is the only name it has on
    disk (``config/update.set_playbook_disabled``, ``raven playbook disable``).

    Read here rather than captured at loop start so that switching one off takes
    effect on the next model call instead of the next process. The list is the
    only per-machine playbook state -- playbook.md is the distribution unit and
    carries no switch -- so this is the whole of what disabling can enforce.
    """
    value = live.get("playbooks.disabled")
    if not isinstance(value, list):
        return frozenset()
    return frozenset(str(x) for x in value if isinstance(x, str))


def _admit(live: LiveConfig, slot: str, *, present: bool, value: Any) -> Any:
    """Dispense one reader's answer with last-good memory.

    ``present`` False -- the section left the file -- forgets the memory and
    answers None, which is the callers' constructor-fallback lane. A present
    candidate that failed validation (``value`` None) keeps the last admitted
    answer serving: a rejected edit must not roll a credential back to the
    boot value, and must not revoke what the last valid file granted.
    """
    if not present:
        live._admitted.pop(slot, None)
        return None
    if value is None:
        return live._admitted.get(slot)
    live._admitted[slot] = value
    return value


def exec_extra_deny_patterns(live: LiveConfig) -> list[str] | None:
    """``tools.exec.extra_deny_patterns`` as the file has it, or None for "no answer".

    None lets the caller keep what it has: no key on disk under either
    spelling (the lane eval harnesses pass patterns through with no file
    behind them), and equally a value the schema rejects -- validated through
    ``ExecToolConfig``, the same door the loader applies, because this list is
    a safety gate and a coerced answer here would *replace* the last valid
    deny rule rather than tighten it.
    """
    for key in ("tools.exec.extraDenyPatterns", "tools.exec.extra_deny_patterns"):
        value = live.get(key)
        if value is None:
            continue
        from raven.config.schema import ExecToolConfig

        try:
            return list(ExecToolConfig.model_validate({"extra_deny_patterns": value}).extra_deny_patterns)
        except Exception:  # noqa: BLE001 - an invalid candidate dispenses no new answer
            return None
    return None


def web_search_key(live: LiveConfig) -> str | None:
    """The Serper key as the file has it, or None for "no answer".

    The file's own section, when present and valid, governs entirely --
    including an empty value, which is how a key gets revoked without a
    restart. None means the file has no ``tools.web.search`` section at all,
    which is the constructor-fallback lane. A section the schema rejects
    (validated through ``WebSearchConfig``) dispenses no new answer: the last
    admitted key keeps serving, so a bad edit can neither roll the credential
    back to the boot value nor revoke what the last valid file granted.
    """
    from raven.config.schema import live_web_search_key

    raw = live.get("tools.web.search")
    if raw is None:
        return _admit(live, "web_search_key", present=False, value=None)
    return _admit(live, "web_search_key", present=True, value=live_web_search_key(raw))


def web_provider_key(live: LiveConfig, vendor: str) -> str | None:
    """One web vendor's key as the file has it, or None for "no answer".

    The canonical half of :func:`web_search_key`: keys live at
    ``tools.web.providers.<vendor>.apiKey`` now, and the pre-vendor leaf that
    function reads is Serper's alone. Same contract otherwise -- a present and
    valid subtree governs entirely, including an empty key, and one the schema
    rejects dispenses no new answer.

    Memoised per vendor, so two vendors' last-good answers cannot overwrite
    each other in the one slot.
    """
    from raven.config.schema import live_web_provider_key

    raw = live.get("tools.web.providers")
    slot = f"web_provider_key:{vendor}"
    if raw is None:
        return _admit(live, slot, present=False, value=None)
    return _admit(live, slot, present=True, value=live_web_provider_key(raw, vendor))


def media_tool_config(live: LiveConfig, kind: str):
    """``tools.media.<kind>`` as the file has it, resolved the way
    ``Config.effective_media_config`` resolves it, or None for "no answer".

    The resolution itself -- validation and the key-borrow rule -- lives in
    ``config.schema`` next to ``effective_media_config``, so the rule is stated
    once and this module never handles a credential field. Only the per-tool
    section (key / base / model) is a live preference; the surrounding wiring
    (proxy, output directory, workspace restriction) is generation state and
    changes with a swap. None means the file has no ``tools.media`` section at
    all -- the constructor-fallback lane. A candidate that fails validation
    mid-rewrite dispenses no new answer and the last admitted slice keeps
    serving, the slice-level form of the rule ``raw`` applies to the file.
    """
    from raven.config.schema import live_media_tool_config

    slot = f"media_tool_config:{kind}"
    media = live.get("tools.media")
    if media is None:
        value = _admit(live, slot, present=False, value=None)
    else:
        candidate = (
            live_media_tool_config(media.get(kind), live.get("providers.openrouter"))
            if isinstance(media, dict)
            else None
        )
        value = _admit(live, slot, present=True, value=candidate)
    return resolve_media_selection(value, kind)


def resolve_media_selection(config, kind: str = "image"):
    """Resolve a host-owned selection for both tool availability and execution."""
    source = getattr(config, "selection_config", "")
    if not source:
        return config
    from raven.config.schema import MediaToolConfig, live_media_tool_config

    live = _selection_live(source)
    media = live.get("tools.media")
    if media is None:
        _admit(live, f"media_selection:{kind}", present=False, value=None)
        return MediaToolConfig()
    value = (
        live_media_tool_config(media.get(kind), live.get("providers.openrouter")) if isinstance(media, dict) else None
    )
    return _admit(live, f"media_selection:{kind}", present=True, value=value) or MediaToolConfig()


@lru_cache(maxsize=32)
def _selection_live(source: str) -> LiveConfig:
    return LiveConfig(Path(source))


def mcp_server_configs(live: LiveConfig) -> dict | None:
    """``tools.mcpServers`` as the file has it, validated, or None for "no answer".

    None -- no section under either spelling, or one that fails validation
    mid-rewrite -- keeps the servers the process already has. An *empty*
    section is a real answer and detaches everything; removing the whole key
    is indistinguishable from never having managed it here, so it
    deliberately changes nothing.
    """
    for key in ("tools.mcpServers", "tools.mcp_servers"):
        raw = live.get(key)
        if isinstance(raw, dict):
            from raven.config.schema import MCPServerConfig

            try:
                return {str(name): MCPServerConfig.model_validate(cfg) for name, cfg in raw.items()}
            except Exception:  # noqa: BLE001 - a torn read is not worth a reconnect storm
                return None
    return None


def routing_profile(live: LiveConfig) -> str | None:
    """``routing.profile`` as the file has it, or None for "no answer"."""
    value = live.get("routing.profile")
    return value if isinstance(value, str) and value else None


def default_model(live: LiveConfig) -> str | None:
    """``agents.defaults.model`` as the file has it, or None for "no answer"."""
    value = live.get("agents.defaults.model")
    return value if isinstance(value, str) and value else None


def disabled_tool_names(live: LiveConfig) -> frozenset[str]:
    """The operator's off switches, as the tool registry wants them.

    Both spellings, because both are on disk: ``settings.set`` writes
    ``tools.disabledTools`` (the wire name the page uses) and the loader's schema
    reads ``tools.disabled_tools``. A switch that only counts under one of them is
    a switch that works from one surface.
    """
    names: set[str] = set()
    for key in ("tools.disabledTools", "tools.disabled_tools"):
        value = live.get(key)
        if isinstance(value, list):
            names.update(str(x) for x in value if isinstance(x, str))
    return frozenset(names)


_LAST_VALID_PERMISSIONS: "weakref.WeakKeyDictionary[LiveConfig, Any]" = weakref.WeakKeyDictionary()


def permissions_config(live: LiveConfig) -> "Any":
    """The permission gate's config, read live and validated.

    Validated on every byte change rather than at loop start so that a mode
    switched on a settings surface reads on the next tool call, which is the
    whole point of the gate re-asking.

    A node that does not validate keeps the LAST VALIDATED policy, mirroring
    ``LiveConfig.raw()``'s stance on a torn file -- and for the same security
    reason stated there, one step up: resetting to the schema's defaults would
    drop the user's explicit deny rules, and a default-allow tool a broken
    edit just un-denied is a policy change nobody made. Only a config that has
    never validated in this process answers with the defaults, because there
    is nothing better to keep.
    """
    from raven.config.schema import PermissionsConfig

    node = live.raw().get("permissions")
    if not isinstance(node, dict):
        node = {}
    try:
        validated = PermissionsConfig.model_validate(node)
    except Exception as exc:  # noqa: BLE001 - a torn or wrong node keeps the last good policy
        kept = _LAST_VALID_PERMISSIONS.get(live)
        logger.warning(
            "live config: permissions node is not valid right now ({}); keeping the {} policy",
            exc,
            "last validated" if kept is not None else "default",
        )
        return kept if kept is not None else PermissionsConfig()
    _LAST_VALID_PERMISSIONS[live] = validated
    return validated
