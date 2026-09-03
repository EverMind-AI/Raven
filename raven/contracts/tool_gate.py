"""The per-call tool adjudication grant: a gate cast over the registry.

A plugin whose policy must see every tool call before it runs -- a
first-write approval, a data-egress rule, a per-effect quota -- contributes
a gate the way it contributes a tool: a manifest entry, a factory taking
``PluginContext``, and -- when it declares ``bind_runtime`` -- the late-bound
``RuntimeHandles`` grants. Three disciplines are the paper, not a convention:

- **Cast at assembly, fixed for the generation.** The registry receives its
  gates at construction and exposes no way to install, swap or remove one
  afterwards -- the generation doors law governs everything a running gate
  could wish to change. A registry built with no gates is byte-identical to
  one that never heard of them.
- **A verdict replaces one call.** ``adjudicate`` runs after the registry
  has validated and cast the parameters and before dispatch; a non-None
  return is handed to the model as that call's result, the call does not
  execute, and the rest of the batch is unaffected. None waves the call
  through to the next gate, then to the tool.
- **An error refuses the call.** A gate that raises blocks the call it was
  adjudicating (the registry answers with an error result naming the gate):
  a gate exists to refuse, and failing open would make its bugs silent
  permission grants.

Gates run in lexicographic (name, contributing plugin id) order; the first
non-None verdict wins. ``session_workdir`` is the turn's bound working
directory (None outside a bound turn), passed explicitly so a gate never
fishes it out of params.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class ToolGate(Protocol):
    """What a ``[[plugin.contributes.tool_gates]]`` factory returns."""

    name: str
    """The gate's own name. A gate without a usable name is refused at the
    manifest door; the registry orders its gates by (name, contributed_by)
    so adjudication is deterministic."""

    async def adjudicate(
        self,
        name: str,
        params: Mapping[str, Any],
        *,
        session_workdir: Path | None,
    ) -> str | None: ...


__tier__ = "contract"
__all__ = ["ToolGate"]
