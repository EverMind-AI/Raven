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
from pathlib import Path
from typing import Any

from loguru import logger

__all__ = ["LiveConfig", "disabled_tool_names"]


class LiveConfig:
    """One config file, re-parsed only when its bytes change.

    Not a cache with a timeout: a timeout answers staleness with a delay, and the
    question here has an exact answer available for the price of one small read.

    The comparison is the file's bytes and not a ``stat`` fingerprint, which is
    what this started as. ``(mtime_ns, size)`` is not a fingerprint of the
    content: two writes of equal length land on the same pair wherever the clock
    granularity is coarser than the gap between them, and the second one is then
    invisible for good. That is not hypothetical -- one CI filesystem could not
    separate "exec" from "grep". Reading a config-sized file is cheap next to the
    LLM call it precedes; being wrong about it is not.
    """

    def __init__(self, path: Path | None = None):
        self._path = path
        self._bytes: bytes | None = None
        self._raw: dict[str, Any] = {}
        self._loaded = False

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
