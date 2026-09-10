"""The plugin contribution surface: the grants a plugin is handed.

A plugin is built OUTSIDE this repository against these three shapes -- the
everos-memory distribution already is -- which makes them papers rather than
plugin machinery: a factory receives a :class:`ServiceLocator`
(construction-time grants), a contributed tool that declares
``bind_runtime(handles)`` receives :class:`RuntimeHandles` (the assembled
loop's late-bound grants), and a binder declines with
:class:`BindDeclinedError`. The envelope that carries them
(``PluginContext``) and the discovery/registry machinery stay in
``raven/plugins``: the papers hold what a third party implements against,
never the machinery that serves it.

A contributed tool may also declare ``configured() -> bool``, an authored
member ``admit_tool`` checks and dispenses on the frozen ``ToolSpec``: the
loop asks it per tool-array assembly and withholds a False for that turn, the
reversible lane the built-in media tools are withheld on. Undeclared: always offered.

Address note: ``raven/plugins/context.py`` re-exports all three, and that
spelling stays the documented import for plugin authors -- moving the
definitions under the papers changes who guards them, not who serves them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider


@dataclass(frozen=True)
class ServiceLocator:
    """Narrow grant of host services to a plugin factory: every field is a
    capability grant, and the dataclass is frozen so adding one is an edit here."""

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
    """How a plugin tells the user something they can act on; the host supplies
    the renderer, a plugin never owns a terminal. ``None``: fall back to the log."""

    provider: "LLMProvider | None" = None
    """The connection's language model, as the loop calls it: a hook or tool
    needing a judgement asks this one instead of building its own from the
    config. ``None`` where the host has no model to lend."""

    media_config: "Callable[[str], Any] | None" = None
    """``tools.media.<kind>`` read live and resolved as the host's own media tools
    resolve it; ``None`` where the deployment never asked for that tool."""

    media_proxy: str | None = None
    """``tools.media.proxy``: generation state, so a value, not a reader."""

    web_config: "Callable[[], Any] | None" = None
    """The host's ``tools.web``: the default a plugin's web-facing tool falls back to."""


@dataclass(frozen=True)
class RuntimeHandles:
    """Late-bound grants for a contributed tool that needs the assembled loop.

    What only the living loop owns cannot be a ``ServiceLocator`` field: the
    loop hands these to a tool declaring ``bind_runtime(handles)`` once, right
    after plugin tools register (the register-first-bind-later idiom of
    ``ask_user``). A tool that raises while binding is unregistered loudly.
    Same discipline as :class:`ServiceLocator`: every field is a grant, frozen.
    """

    session_dir: Path | None = None
    """Where the host keeps session records; a tool that files per-session
    artifacts (a playbook run's transcript) roots them here."""

    subagent_registry: Any = None
    """The live sub-agent registry. A BIG grant -- whoever holds it can
    enumerate and drive sub-agents; a tool asking for it is asking to orchestrate."""

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

    wake_scheduler: Any = None
    """Keyed one-shot wakes on the host's scheduler, namespaced to the
    contributing plugin (paper: contracts/scheduling.py) -- a holder's keys
    can neither see nor move another plugin's wakes, nor any plain reminder.
    ``None`` where the host runs no scheduler (a one-shot ``raven agent -m``,
    a test locator), which a binder treats as a decline."""

    usage_recorder: Any = None
    """The loop's image-usage recorder, ``async (UsageSnapshot) -> None``; ``None``: unrecorded."""

    direct_ask: Any = None
    """Put one question to the user mid-turn and await the answer, as the loop
    itself does: ``async (prompt, choices, conversation_id, timeout_s) -> str | None``,
    None when no asking transport is bound or the conversation is still busy with
    another question at the deadline (one question per conversation at a time).
    A BIG grant: whoever holds it can interrupt the user."""

    rebind_workdir: Any = None
    """Repoint one session's working directory: ``(session_key, target) -> Path``
    persists the override into the session's metadata (the durable truth
    ``WorkdirResolver`` reads back) and, from inside that session's own turn,
    repoints the live binding too. Targets pass ``workdir.validate_override``.
    A BIG grant: whoever holds it moves where every subsequent write lands."""


class BindDeclinedError(Exception):
    """Raised inside ``bind_runtime`` to decline serving.

    The late-bound twin of a factory returning ``None``: the grant this tool
    needs is not in the handles (the feature is off in this loop, the organ
    absent), so the tool asks to be taken off the table. The loop unregisters
    it quietly -- a decline is a configuration fact, not a plugin bug, so no
    traceback."""


__tier__ = "contract"
__all__ = ["BindDeclinedError", "RuntimeHandles", "ServiceLocator"]
