"""The one table of dispatchable agents, materialized once per process.

Every consumer that has to answer "which agents exist, what can each do, and how
do I run one" reads this: ``spawn``, the DAG tool, the playbook generator's
roster. Before it there were four derivations of the same config -- the spawn
manager's backend dict, the DAG tool's, a snapshot inside the playbook
executor's private tool, and a hardcoded four-name tuple in the playbook layer --
and no two of them agreed. A hot-apply refreshed the first two independently, so
"the manager has hermes, the DAG tool does not" was a reachable state.

Two facts are kept apart on purpose:

- **how an agent is reached** (``kind`` and the transport fields) is config, and
  only the backend factory reads it;
- **what an agent can do** (:class:`AgentCaps`, :class:`Injectable`) is derived
  here and is what every consumer branches on.

That split is why a consumer can hold a name and nothing else. A caller that
switched on ``kind`` would have to grow a branch for every transport ever added.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.subagent.backends import (
    AgentMeta,
    SubagentBackend,
    acp_snapshot_for,
    agent_meta,
    build_third_party_backend,
    format_agent_listing,
    session_mcp_effective,
)
from raven.agent.subagent.builtin_agents import LEGACY_AGENT_ALIASES, canonical_agent_name, merge_builtin_seeds
from raven.agent.subagent.vendored_agents import discover_product_rows, merge_product_seeds

if TYPE_CHECKING:
    from raven.agent.subagent.mcp_grant import McpSource


@dataclass(frozen=True)
class AgentCaps:
    """What one agent can do, as the roster advertises it.

    Derived from the mechanism that would have to deliver each capability, never
    from a wish -- see ``agent_meta`` for the per-kind derivation and why each one
    reads what it reads.
    """

    stateful: bool
    reads_local_files: bool
    live_progress: bool
    modes: tuple[Any, ...] = ()
    """The agent's operating profiles, when its transport has them (acp only).

    A menu rather than a capability, so unlike the three above it is not
    rendered as a roster tag -- see ``AgentMeta.modes``."""


@dataclass(frozen=True)
class Injectable:
    """Whether per-node configuration can be pushed into this agent's session.

    Only an in-process raven loop has a skill menu and an MCP client raven owns,
    so only ``builtin`` rows can take either. Recorded as a field rather than
    left as a verbal rule so a node that declares ``skills`` for a cli agent gets
    told it will do nothing, instead of appearing to work.
    """

    skills: bool
    mcps: bool


@dataclass(frozen=True)
class AgentRow:
    """One dispatchable agent: identity, advertised capability, and its config."""

    name: str
    kind: str
    description: str
    enabled: bool
    caps: AgentCaps
    injectable: Injectable
    owns: str
    config: Any
    """The validated config object. Only the backend factory reads it."""

    owns_watched_work: bool = False
    """Whether this agent owns run-and-watch work; see ``AgentMeta``."""

    def meta(self) -> AgentMeta:
        """This row as the roster renders it."""
        return AgentMeta(
            self.name,
            self.description,
            self.caps.stateful,
            self.caps.reads_local_files,
            self.caps.live_progress,
            self.owns,
            self.caps.modes,
            self.owns_watched_work,
        )


# What ``build`` narrowing a builtin backend is read off. Duck-typed so the DAG
# runner, the playbook executor and a test double can all supply one without a
# shared import.
BuiltinBuilder = Callable[[Any, Any], SubagentBackend]


def _row_for(cfg: Any) -> AgentRow:
    """One config entry as a table row."""
    kind = getattr(cfg, "kind", None) or ""
    builtin = kind == "builtin"
    snapshot = acp_snapshot_for(cfg) if kind == "acp" else None
    meta = agent_meta(cfg, snapshot=snapshot)
    if kind == "cli":
        templates = [cfg.command, *([cfg.resume_command] if cfg.resume_command else [])]
        injectable_mcps = all("{mcp_file}" in template for template in templates)
    elif kind == "acp":
        # The effective verdict, not the transport-level one. An acp peer that
        # takes the field but is not isolated has its servers withheld at
        # dispatch, and a row advertising injection is how the playbook generator
        # comes to assign a required server to it (measured: the opencode preset).
        injectable_mcps = session_mcp_effective(cfg, snapshot=snapshot)
    else:
        injectable_mcps = kind == "builtin"
    return AgentRow(
        name=meta.name,
        kind=kind,
        description=meta.description,
        enabled=bool(getattr(cfg, "enabled", True)),
        caps=AgentCaps(
            stateful=meta.stateful,
            reads_local_files=meta.reads_local_files,
            live_progress=meta.live_progress,
            modes=meta.modes,
        ),
        injectable=Injectable(skills=builtin, mcps=injectable_mcps),
        owns=meta.owns,
        config=cfg,
        owns_watched_work=meta.owns_watched_work,
    )


class AgentRegistry:
    """The materialized agent table: ``name -> (row, backend)``.

    ``apply`` is the only writer, and startup and hot-apply both go through it,
    so the two cannot produce different tables. Consumers hold a reference to the
    registry rather than a copy of its contents -- a copy is how the pre-registry
    code ended up with two rosters that drifted.
    """

    def __init__(self, *, build_builtin: BuiltinBuilder | None = None) -> None:
        # Injected rather than constructed here: an in-process raven loop needs a
        # provider, a model, agent home, the exec config and the sandbox -- all of
        # which the sub-agent manager already owns. Reproducing that list here
        # would be a second copy of it, and the copy would drift the moment a
        # runtime dependency is added.
        self._build_builtin = build_builtin
        self._rows: dict[str, AgentRow] = {}
        self._order: list[str] = []
        self._backends: dict[str, SubagentBackend] = {}
        self._mcp_source: "McpSource | None" = None

    def set_builtin_builder(self, build_builtin: BuiltinBuilder | None) -> None:
        """Late-bind the in-process backend factory.

        The manager builds this registry in its own constructor, so the bound
        method cannot be passed in at construction time without a half-built
        ``self`` escaping. Clears the cache: backends already built against the
        previous builder would answer on the old provider.
        """
        self._build_builtin = build_builtin
        self._backends = {name: b for name, b in self._backends.items() if self._rows[name].kind != "builtin"}

    def set_mcp_source(self, source: "McpSource | None") -> None:
        """Late-bind the host MCP view into cached and future backends."""
        self._mcp_source = source
        for backend in self._backends.values():
            setter = getattr(backend, "set_mcp_source", None)
            if setter is not None:
                setter(source)

    def apply(self, configs: Sequence[Any] | None) -> None:
        """(Re)build the whole table from config. The only write path.

        Three sources compose here, weakest first: rows discovered from the
        ``agents/`` product tree (see :func:`discover_product_rows`), then the
        package seed rows (:func:`merge_builtin_seeds`), then config -- so the
        built-in and discovered agents are on the table whether or not config mentions them, and
        a config row of the same name is the user's edit of one. One bad entry is
        skipped with a warning rather than sinking the table: a table that fails to
        build takes every agent down, including the ones that were fine.

        Discovery happens *here* rather than at the call sites for the reason
        ``enabled_agents`` documents about its own filter: six paths hand a config
        list to this method (four CLI entry points, the manager's construction and
        its hot-apply), so composing at the boundary would be six places to keep in
        step and the seventh would be written without it. The cost is that this
        method reads the filesystem, which makes the table depend on whether an
        install has the tree -- the intended behaviour in production, and pinned in
        tests by the ``no_discovered_products`` fixture so the machine's own tree
        cannot change what the suite sees.
        """
        rows: dict[str, AgentRow] = {}
        order: list[str] = []
        backends: dict[str, SubagentBackend] = {}
        merged = merge_product_seeds(list(configs or []), discover_product_rows())
        for cfg in merge_builtin_seeds(merged):
            name = getattr(cfg, "name", None)
            try:
                row = _row_for(cfg)
                if not row.name:
                    raise ValueError("entry has no name")
                if row.kind != "builtin":
                    # Built eagerly, as before: an external backend holds a
                    # subprocess pool or an HTTP client that is meant to be shared
                    # across dispatches, and a build failure has to be visible here
                    # rather than at the first spawn.
                    backend = build_third_party_backend(cfg)
                    setter = getattr(backend, "set_mcp_source", None)
                    if setter is not None:
                        setter(self._mcp_source)
                    backends[row.name] = backend
                rows[row.name] = row
                order.append(row.name)
            except Exception as exc:  # noqa: BLE001 - a bad entry must not sink the table
                logger.warning("Skipping sub-agent {!r}: {}", name, exc)
        self._rows = rows
        self._order = order
        self._backends = backends

    def rows(self) -> list[AgentRow]:
        """Every row, disabled included -- the operations view.

        Config order, with the package seeds first, so a list rendered for a human
        does not reshuffle between reads.
        """
        return [self._rows[name] for name in self._order]

    def enabled(self) -> list[AgentRow]:
        """The rows the model may dispatch to.

        ``enabled`` records what the user wants and is deliberately never derived
        from a probe: a roster that shrank on a failed PATH lookup would let an
        agent vanish mid-session, and the model would then plan against a roster
        that changed underneath it -- worse than a spawn that fails with a clear
        error.
        """
        return [row for row in self.rows() if row.enabled]

    def get(self, name: str) -> AgentRow | None:
        """One row by name, enabled or not.

        Disabled rows are returned because "exists but is turned off here" and
        "was never registered" need different errors -- a playbook that names a
        disabled agent is not a broken playbook.

        A name a seed used to answer to resolves to the seed (see
        :data:`LEGACY_AGENT_ALIASES`), so a stored instance row, direct-chat record
        or dag node written before a rename still finds its agent. Exact first: an
        agent that really holds the name owns it.
        """
        return self._rows.get(name) or self._rows.get(canonical_agent_name(name))

    def meta(self) -> list[AgentMeta]:
        """Roster entries for the enabled rows, in table order."""
        return [row.meta() for row in self.enabled()]

    def roster_text(self) -> str:
        """The enabled roster as a tool description renders it."""
        return format_agent_listing(self.meta())

    def descriptions(self) -> dict[str, str]:
        """``name -> description`` for the enabled rows.

        What the playbook generator casts nodes against; it used to be a
        hardcoded four-name pool with no connection to the table.
        """
        return {row.name: row.description for row in self.enabled()}

    def backend(self, name: str, *, build: Any = None) -> SubagentBackend | None:
        """The backend for one agent, or ``None`` if the name is not on the table.

        ``build`` narrows an in-process agent for one dispatch (a DAG node's
        ``skills``, a playbook step's) and is ignored by the external kinds, which
        have no menu to narrow. It is *narrowing only*: whatever the row allows
        bounds what ``build`` can ask for, so a node cannot widen its own reach by
        naming a skill the agent's row excludes.
        """
        # Resolved the same way as :meth:`get`, and for the same stored references:
        # a resume that found its row here would still fail to dispatch if only one
        # of the two lookups understood the name it was written under.
        name = name if name in self._rows else canonical_agent_name(name)
        row = self._rows.get(name)
        if row is None:
            return None
        if row.kind != "builtin":
            return self._backends.get(name)
        if self._build_builtin is None:
            logger.warning(
                "sub-agent {!r} is a built-in agent but no in-process backend factory is wired; cannot dispatch to it",
                name,
            )
            return None
        narrowed = _narrow_build(row, build)
        if build is None and (cached := self._backends.get(name)) is not None:
            return cached
        backend = self._build_builtin(row, narrowed)
        setter = getattr(backend, "set_mcp_source", None)
        if setter is not None:
            setter(self._mcp_source)
        if build is None:
            self._backends[name] = backend
        return backend

    def backends(self) -> list[Any]:
        """Every external backend the table currently holds, for a manager to bind.

        The DAG path resolves a node's backend through this registry directly,
        never through the manager's per-dispatch resolver, so a manager that
        only bound what it resolved left graph-only agents with no route for an
        unprompted turn. A generation binds them all here, once, at apply time.
        """
        return list(self._backends.values())

    def names(self) -> list[str]:
        """Enabled agent names, sorted -- the ``enum`` a tool schema constrains to."""
        return sorted(row.name for row in self.enabled())

    def all_names(self) -> list[str]:
        """Every name on the table, disabled included -- what *validating* checks against.

        Deliberately not the same view as :meth:`names`. A playbook is a
        distribution unit, and being on the table is what makes its agent
        reference resolvable; whether this machine currently has that agent
        switched on is a runtime condition with its own error, not a reason to
        call the file malformed.

        Includes the legacy aliases of the rows on the table: a playbook written
        before an agent was renamed names it by its old name, and that reference
        resolves (see :meth:`get`), so calling the file malformed would be wrong.
        Not the same view as :meth:`names`, which is the enum the model picks from
        and offers current names only.
        """
        aliases = {alias for alias, current in LEGACY_AGENT_ALIASES.items() if current in self._rows}
        return sorted(set(self._rows) | aliases)


@dataclass(frozen=True)
class _Narrowed:
    """The effective allow-lists for one dispatch to a builtin agent.

    Duck-typed on ``tools_allow`` / ``skills_allow``, which is what
    ``SubagentManager.build_builtin_backend`` reads.
    """

    tools_allow: list[str] | None
    skills_allow: list[str] | None


def _narrow_build(row: AgentRow, build: Any) -> _Narrowed:
    """Intersect the row's allow-lists with this dispatch's.

    Three-valued on both sides: ``None`` is "no narrowing at this level", an empty
    list is "nothing allowed", and a list narrows. The empty case has to survive
    the merge -- folding it into ``None`` would turn "this step gets no skills"
    into "this step gets all of them", which is how ``skills: []`` in a playbook
    came to mean its own opposite.
    """
    return _Narrowed(
        tools_allow=_intersect(getattr(row.config, "tools", None), getattr(build, "tools_allow", None)),
        skills_allow=_intersect(getattr(row.config, "skills", None), getattr(build, "skills_allow", None)),
    )


def _intersect(row_allow: list[str] | None, node_allow: list[str] | None) -> list[str] | None:
    if node_allow is None:
        return row_allow
    if row_allow is None:
        return node_allow
    allowed = set(row_allow)
    return [entry for entry in node_allow if entry in allowed]


__all__ = ["AgentCaps", "AgentRegistry", "AgentRow", "Injectable"]
