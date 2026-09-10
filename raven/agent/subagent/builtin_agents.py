"""The built-in agent row the package ships, and how config overrides it.

There is exactly one: the in-process raven loop, on the same table as the
external agents so that ``spawn`` and a DAG node pick from one roster -- a
built-in agent only reachable by omitting the ``subagent`` argument is an agent
the model cannot be told about. The transport is the seed's, with one deliberate
exception: a config row of a seed's name with ``kind: "acp"`` redeclares it as
the host's own ``raven acp`` (see :func:`merge_builtin_seeds`), which is the
established way to run the generic agent's work outside the main process.

It is a seed rather than a default-in-a-field: the row exists whether or not
config mentions it, and a config row of the same name is a *field-level*
override -- writing ``{"name": "raven", "skills": [...]}`` retunes its skill menu
and leaves its description alone. There is deliberately no way to take it off the
roster: not writing a row already means "use the package's row", and ``enabled``
is the one field an override may not speak to.

**Four more rows used to be here** -- ``research-raven``, ``code-raven``,
``data-raven``, ``content-raven`` -- inherited from the playbook role pool that
predated this table. They were removed because they were not agents. The backend
factory reads four fields off a row (``model``, ``restrict_to_workspace``,
``tools``, ``skills``) and all four were unset on all four rows, so every one of
them dispatched the same provider, the same model, the same tool set and the same
system prompt; the sub-agent was never even told which name it was running under.
What they did have was five distinct descriptions on the roster, which is why the
dispatching model wrote ``research-raven`` on a node -- it was picking off a menu,
not from the work. The per-step differentiation that mattered was always in the
node's ``promptTemplate``, and that is untouched by their removal. Re-adding a
specialised row is a matter of writing one with a real narrowing on it; a row
whose only content is a description is a label the table lends false authority to.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.config.agent_names import (
    BUILTIN_AGENT_NAMES as BUILTIN_AGENT_NAMES,
)
from raven.config.agent_names import (
    GENERIC_AGENT as GENERIC_AGENT,
)
from raven.config.agent_names import (
    LEGACY_AGENT_ALIASES as LEGACY_AGENT_ALIASES,
)
from raven.config.agent_names import (
    canonical_agent_name as canonical_agent_name,
)
from raven.config.agent_names import (
    is_builtin_agent_name as is_builtin_agent_name,
)
from raven.config.loader import get_config_path

if TYPE_CHECKING:
    from raven.config.schema import BuiltinAgentConfig


_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "name": GENERIC_AGENT,
        # Says what the row cannot do, because nothing else tells the dispatching
        # model: ``RavenLoopBackend`` builds its own registry (files, shell, web)
        # and registers neither ``spawn`` nor ``run_subagent_dag``, so a step that
        # has to delegate cannot be done here and cannot be handed on. "The skills
        # those tools support" is the same honesty about the menu -- the sub-agent
        # prompt withholds any skill whose ``requires.tools`` this row lacks.
        "description": (
            "Raven's own in-process sub-agent: files, shell and web, plus the skills those tools "
            "support, with no capability bias. Put what this step must accomplish in its prompt -- "
            "that is the only thing that shapes the run. IMPORTANT: it cannot call sub-agents, so "
            "give it only work it can finish on its own."
        ),
    },
)


def builtin_agent_seeds() -> list["BuiltinAgentConfig"]:
    """The package's built-in rows, freshly validated.

    Built per call rather than as a module constant: the rows are mutable pydantic
    models and the registry hands them out, so one shared instance would let a
    caller's ``model_copy(update=...)`` target leak into the next materialization.
    """
    from raven.config.schema import BuiltinAgentConfig

    return [BuiltinAgentConfig.model_validate(seed) for seed in _SEEDS]


_NOT_OVERRIDABLE = frozenset({"enabled", "name"})
"""Seed fields a config row cannot speak to. See :func:`_overrides`."""


def _overrides(cfg: Any) -> dict[str, Any]:
    """The fields of one config row that actually say something.

    Fields still holding the schema default are dropped, rather than trusting
    ``model_fields_set``. A row does not stay in the shape it was written in: the
    write path validates and dumps the whole model, so a stored override of one
    field comes back with *every* field present -- and applied literally, an
    override that only meant to flip ``enabled`` would also reset the seed's
    description to ``""`` and blank the agent out of the roster the model reads.

    The cost is that a value equal to the default cannot be written *as* an
    override. For the fields here that reads correctly: ``description: ""`` is not
    an edit anyone means, and every other default is "inherit".

    ``enabled`` is dropped whatever it says: a seed row's switch is the package's,
    not config's. See :func:`merge_builtin_seeds` for why.

    ``name`` is dropped for the same shape of reason: it is what identified the seed
    to override in the first place, so it can only say what is already true -- or,
    for a row written under a legacy alias, rename the merged row back to the name
    the alias exists to move away from.
    """
    from raven.config.schema import BuiltinAgentConfig

    fields = BuiltinAgentConfig.model_fields
    out: dict[str, Any] = {}
    for name in cfg.model_fields_set:
        if name in _NOT_OVERRIDABLE:
            continue
        field = fields.get(name)
        value = getattr(cfg, name)
        if field is not None and value == field.default:
            continue
        out[name] = value
    return out


def _without_shadowed_aliases(configs: list[Any]) -> list[Any]:
    """Config rows with any legacy-named row dropped whose current name is also written.

    Two spellings of one seed are the ambiguity ``SubagentsConfig._dedupe_names``
    removes for two rows of one name, and it cannot see this pair because the
    strings differ. Resolved here rather than by order of appearance, so which
    override wins does not depend on where in the file the rows sit.
    """
    written = {getattr(cfg, "name", None) for cfg in configs}
    kept: list[Any] = []
    for cfg in configs:
        name = getattr(cfg, "name", None)
        canonical = canonical_agent_name(name) if name else None
        if canonical is not None and canonical != name and canonical in written:
            logger.warning(
                "sub-agent {!r} is the former name of built-in agent {!r}, and config declares both; "
                "the entry under {!r} wins and this one is ignored -- delete it to silence this",
                name,
                canonical,
                canonical,
            )
            continue
        kept.append(cfg)
    return kept


def merge_builtin_seeds(configs: list[Any] | None) -> list[Any]:
    """Config rows over the package seeds: the table the registry materializes.

    Seeds keep their declared order and stay in place when overridden, so the
    roster the model reads does not reshuffle because a user retuned one agent.
    Rows naming something the package does not ship are appended in config order.

    A config row overrides a seed field-by-field, using only the fields that say
    something (see :func:`_overrides` -- not ``model_fields_set``, which a stored
    row arrives with fully populated) -- a row that mentions ``skills`` must not
    silently reset ``description`` to the schema default and blank the agent out of
    the roster.

    ``enabled`` is not among the fields an override can set. An unnamed ``spawn``
    and a DAG node with no ``subagent`` both normalize to the generic seed, so a
    roster without it is a roster with a hole where the default lands. The seed's
    switch therefore wins over a stored one -- including one hand-written into
    config, which is the case no UI guard can reach.

    A row naming a seed the way it used to be named is that seed's override too
    (see :data:`LEGACY_AGENT_ALIASES`); if config declares both spellings, the
    current one wins and the legacy row is dropped with a warning.

    A cli / openai row that claims a seed's name does not take the slot, with a
    warning: two rows of one name is the ambiguity ``SubagentsConfig._dedupe_names``
    exists to remove, and between the two the one the user wrote by hand is the one
    they meant -- except that a seed row is mandatory, and an unnamed ``spawn``
    and a dag node with no ``subagent`` both resolve to this one, so letting a
    cli / openai row take the slot would silently put an external backend behind
    every such dispatch. An ``acp`` row of a seed's name is exactly that tradeoff
    named by the user, so it wins: the generic agent is then served by a spawned
    ``raven acp`` (see :func:`host_raven_acp_command`), not the in-process loop.
    The seed is not recoverable either way except by renaming that row, which
    the warning says.
    """
    by_name: dict[str, Any] = {}
    order: list[str] = []
    for seed in builtin_agent_seeds():
        by_name[seed.name] = seed
        order.append(seed.name)

    for cfg in _without_shadowed_aliases(configs or []):
        name = getattr(cfg, "name", None)
        if not name:
            continue
        # A stored row may spell a seed's name the way that seed used to be
        # spelled; it is still that seed's override, and keying it under the
        # current name is what keeps it from landing beside the seed as a second,
        # phantom agent.
        name = canonical_agent_name(name)
        seed = by_name.get(name)
        if seed is None:
            by_name[name] = cfg
            order.append(name)
            continue
        if getattr(cfg, "kind", None) == "acp":
            # A deliberate transport switch: the generic agent is served over
            # ACP, so every dispatch to it -- an unnamed ``spawn``, a dag node
            # with no ``subagent`` -- spawns the host's own ``raven acp``. The
            # row takes the seed's slot wholesale rather than field-by-field: the
            # seed's fields are in-process-loop fields (``model``, ``tools``,
            # ``skills``) and a third of an acp row would not run.
            by_name[name] = _acp_host_override(cfg, name=name)
            logger.info(
                "sub-agent {!r} is served over acp: the built-in row's transport has been overridden "
                "to this raven's own `raven acp`",
                name,
            )
            continue
        if getattr(cfg, "kind", None) != "builtin":
            # The seed wins, and the configured row is dropped from the table as
            # before -- see the docstring for which kinds and why.
            logger.warning(
                "sub-agent {!r} is declared as kind {!r} but that name belongs to a built-in agent; "
                "the built-in row wins and this entry is ignored -- rename it in config to keep both",
                name,
                getattr(cfg, "kind", None),
            )
            continue
        by_name[name] = seed.model_copy(update=_overrides(cfg))

    return [by_name[name] for name in order]


def host_raven_acp_command() -> str:
    """This installation's own ``raven acp`` command line, or "" if it cannot be resolved.

    The console script beside the running interpreter wins -- an editable
    checkout's ``.venv/bin/raven`` -- with ``$PATH`` as the fallback, so an acp
    redeclaration of a seed row names the raven build that is actually running,
    not whichever ``raven`` comes first on the login shell's PATH. ``shlex.quote``
    keeps a path with a space one argv token.
    """
    exe = "raven.exe" if os.name == "nt" else "raven"
    candidate = Path(sys.executable).with_name(exe)
    if candidate.is_file():
        return f"{shlex.quote(str(candidate))} acp"
    found = shutil.which(exe)
    return f"{shlex.quote(found)} acp" if found else ""


_ACP_HOST_DESCRIPTION = (
    "Raven's own agent served over ACP: this raven as a separate process, on the same config and "
    "model, with its own sub-agent delegation."
)


def _acp_host_override(cfg: Any, *, name: str) -> Any:
    """An acp row that takes a seed's slot, with the host defaults filled in.

    The config row brings the transport choice and nothing else: ``command``
    defaults to this install's own ``raven acp``, ``env`` carries the host's
    ``RAVEN_HOME`` (unless the row sets one) so the child engine reads the same
    config and provider the host does, and a blank ``description`` gets the
    host-raven line rather than leaving the roster entry with the in-process
    description that no longer applies.

    ``RAVEN_HOME`` alone does not carry a host started with ``--config``: that
    flag sets a path inside the loader and never touches the environment, so the
    child derived its config from a home the host was not using and came up on a
    different model and provider than the row promises.

    Two things stop that from overriding a choice somebody made. The flag is
    appended only to a command this function generated -- a row that brought its
    own command line chose it, and is not somewhere to inject arguments -- and
    only when the row named no ``RAVEN_HOME`` of its own. An explicit one is the
    row asking for a different instance, and ``--config`` outranks ``RAVEN_HOME``
    in the child, so propagating regardless would serve that row the host's model
    and provider under its own name.
    """
    from raven.contracts.path_policy import CONFIG_FILENAME, HOME_ENV_VAR
    from raven.home import raven_home

    home = raven_home()
    env = dict(getattr(cfg, "env", None) or {})
    row_named_its_own_home = HOME_ENV_VAR in env
    env.setdefault(HOME_ENV_VAR, str(home))
    command = str(getattr(cfg, "command", "") or "").strip()
    if not command:
        command = host_raven_acp_command()
        if not command:
            logger.warning(
                "sub-agent {!r}: acp redeclaration has no command and this install's own `raven` "
                "could not be resolved; the row will fail at dispatch",
                name,
            )
        elif not row_named_its_own_home and (config_path := get_config_path()) != home / CONFIG_FILENAME:
            command = f"{command} --config {shlex.quote(str(config_path))}"
    description = str(getattr(cfg, "description", "") or "").strip()
    if not description:
        description = _ACP_HOST_DESCRIPTION
    update: dict[str, Any] = {
        "name": name,
        "command": command,
        "env": env,
        "description": description,
    }
    return cfg.model_copy(update=update)


__all__ = [
    "BUILTIN_AGENT_NAMES",
    "GENERIC_AGENT",
    "LEGACY_AGENT_ALIASES",
    "builtin_agent_seeds",
    "canonical_agent_name",
    "host_raven_acp_command",
    "is_builtin_agent_name",
    "merge_builtin_seeds",
]
