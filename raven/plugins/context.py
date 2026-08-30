"""Plugin runtime context.

A factory (the ``module.path:callable`` named in a manifest) receives
exactly one :class:`PluginContext`. From it, the factory pulls:

- ``config``  — the plugin's own config slice from RavenConfig, after
  admission: declared keys are type-checked and their declared defaults
  applied, unknown keys pass through with a warning. An empty
  ``config_schema`` keeps verbatim pass-through.
- ``services`` — a :class:`ServiceLocator` exposing only the host
  services a backend is allowed to touch. The locator is intentionally
  narrow so plugins don't grow ambient dependencies on arbitrary host
  internals — every field here is a deliberate capability grant.
- ``logger`` — a logger pre-bound with ``plugin=<id>`` so plugin output
  is grep-able in mixed logs.

The locator is a frozen dataclass: factories cannot mutate the host's
view of available services, only read from it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider


@dataclass(frozen=True)
class ServiceLocator:
    """Narrow grant of host services to a plugin factory.

    Every field is a capability grant, and the dataclass is frozen so adding
    one is an explicit edit here rather than an ambient setattr somewhere else.
    """

    workspace: Path
    """Agent home (``~/.raven/<workspace>``): the global memory, skills and
    transcript root, not the per-session working directory a turn writes in."""

    user_id: str
    """User-track owner identity.

    The single source of truth is ``MemoryConfig.user_id`` (the ``userId``
    key of the ``memory`` block in ``~/.raven/config.json``). A backend must
    never take this from its own plugin config slice: that is a second place
    holding the same value, and a user who edits only one silently splits
    store and recall onto different owner ids, making every written memory
    unrecallable with no warning."""

    agent_id: str
    """Agent-track owner identity. Same single-source rule as ``user_id``."""

    notify: Callable[[str], None] | None = None
    """How a plugin tells the user something they can act on ("long-term memory
    is off: ..."). The host supplies the renderer (a console, a notice channel);
    a plugin never owns a terminal. ``None`` means the host offers no channel and
    the plugin falls back to its log."""

    provider: "LLMProvider | None" = None
    """The connection's language model, as the loop itself calls it. A hook or
    tool that needs a judgement (a draft reviewer, a sufficiency check, a page
    digest) asks this one instead of building its own from the config: one
    credential, one pool, one place a model switch lands. ``None`` where the
    host has no model to lend (a CLI listing plugins, a test building a locator)."""


@dataclass(frozen=True)
class RuntimeHandles:
    """Late-bound grants for a contributed tool that needs the assembled loop.

    A factory runs in the assembly root, before the loop exists, so anything
    only the living loop owns cannot be a ``ServiceLocator`` field -- it does
    not exist yet. The loop hands these to a contributed tool that declares
    ``bind_runtime(handles)``, once, right after plugin tools register: the
    same register-first-bind-later idiom ``ask_user`` has always used for its
    transport broker, made a first-class contribution shape. A tool that
    raises while binding is unregistered loudly rather than left half-bound.

    Same discipline as :class:`ServiceLocator`: every field is a deliberate
    capability grant, and the dataclass is frozen.
    """

    session_dir: Path | None = None
    """Where the host keeps session records; a tool that files per-session
    artifacts (a playbook run's transcript) roots them here."""

    subagent_registry: Any = None
    """The live sub-agent registry. A BIG grant -- whoever holds it can
    enumerate and drive sub-agents -- named as such on purpose: a tool asking
    for it is asking to orchestrate, and a reviewer should see that in the
    manifest's own vocabulary rather than discover it in a traceback."""

    subagents_paused: "Callable[[], bool] | None" = None
    """Whether the operator paused sub-agent work; an orchestrating tool
    consults this before starting more."""

    playbook_runtime: Any = None
    """The loop's assembled playbook funnel (library, executor, creation's
    composer), for the bundled playbook tools to bind. The loop assembles it
    so dispatch discipline stays single -- one gate, one quota, one announce
    path -- and the plugin only serves it. ``None`` when the feature is off
    in this loop or the funnel failed to build, which a binder treats as a
    decline."""


class BindDeclinedError(Exception):
    """Raised inside ``bind_runtime`` to decline serving.

    The late-bound twin of a factory returning ``None``: the grant this tool
    needs is not in the handles (the feature is off in this loop, the organ
    absent), so the tool asks to be taken off the table. The loop unregisters
    it quietly -- a decline is a configuration fact, not a plugin bug, so no
    traceback."""


@dataclass(frozen=True)
class PluginContext:
    """What a plugin factory sees at activation time."""

    config: dict[str, Any]
    services: ServiceLocator
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("raven.plugins"),
    )


__all__ = ["PluginContext", "ServiceLocator"]
