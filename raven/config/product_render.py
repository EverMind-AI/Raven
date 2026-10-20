"""The launcher library: render a product config and hand it to installed raven.

A product under ``agents/`` is a folder, not a fork: a published
``config.json``, a ``.env`` for secrets, and a launcher that renders the two
into a private copy and execs ``python -m raven acp``. What every such
launcher repeats is here -- reading a setting from the process environment
with a ``.env`` fallback, merging declared secret slots, inheriting the
host's LLM block when the product has no key of its own, pinning a state
root under the host's home and an engine home OUTSIDE it (the home lives in
the raven data directory, the work stays where the work is), regenerating a
product-owned workspace file on every launch, assembling the
``acp.modes`` catalogue from overlay files, sweeping stale renders by pid
liveness, and writing the rendered copy owner-only with the host's deny
rules merged in.

What stays in each product is its tables and its judgement: which secrets go
where, what its modes are called, how a mode's budget resolves, what must
refuse to launch. The split is deliberate -- the machine here is the trunk's
contract (config paths, the ``acp.modes`` shape, the rendered file's
location being load-bearing for the data dir), while every name in a table
is product vocabulary the trunk must not learn.

Importing this widens a launcher's import surface beyond the two kernel
symbols the first launcher deliberately limited itself to; that boundary was
given up knowingly (ruled 2026-08-31): a launcher execs this interpreter's
own raven anyway, so importing the library it is about to run adds no new
requirement.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from raven.config.raven import MEMORY_BACKEND_DISTRIBUTIONS, SHIPPED_DEFAULT_BACKEND
from raven.contracts.path_policy import CONFIG_FILENAME, WORKSPACE_DEFAULT_SENTINEL
from raven.home import raven_home
from raven.utils.atomic_io import atomic_replace, write_transaction
from raven.utils.paths import safe_path_segment
from raven.utils.pid import pid_alive


def env_value(name: str, *, env_file: Path | None = None) -> str | None:
    """Read a setting from the process environment, falling back to ``env_file``.

    The environment wins so a caller can override one value without editing
    the file that holds the others.
    """
    if value := os.environ.get(name):
        return value.strip()
    if env_file is not None and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip()
    return None


def host_config() -> dict:
    """The host raven's config, or an empty dict when there is none to read.

    Read as JSON through the path paper's answer. ``raven_home()`` here is the
    host's home because the host passes its ``RAVEN_HOME`` to a launcher when
    it has one set, and with none set both normally resolve ``~/.raven``; a
    ``RAVEN_HOME`` the login profile exports while the host has none sends the
    launcher elsewhere.
    """
    try:
        return json.loads((raven_home() / CONFIG_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def dig(data: dict, path: tuple) -> str:
    """The string at a dotted path, or ``""`` for anything absent or non-string."""
    node: Any = data
    for part in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(part)
    return node if isinstance(node, str) else ""


def put(data: dict, path: tuple, value: str) -> None:
    """Write ``value`` at a dotted path, creating the dicts on the way."""
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def apply_secret_slots(
    config: dict,
    host: dict,
    *,
    slots: dict[str, tuple],
    required: Iterable[str],
    lookup: Callable[[str], str | None],
) -> None:
    """Merge each declared secret into its config path.

    ``slots`` is the product's table (env var name -> config path) and
    ``lookup`` its resolution order (typically :func:`env_value` over its own
    ``.env``). An optional secret the product does not set falls back to the
    same path in the host config; a ``required`` one never falls back
    per-slot -- its absence is the product's own signal (the first launcher
    switches the whole LLM block to host inheritance on it, see
    :func:`inherit_llm`).
    """
    required = set(required)
    for name, path in slots.items():
        value = lookup(name)
        if not value and name not in required:
            value = dig(host, path)
        if value:
            put(config, path, value)


def _inherited_defaults(config: dict, host: dict) -> dict:
    """The ``agents.defaults`` an agent would run on after taking the host's LLM.

    The agent's own defaults with the host's provider, model and reasoning
    effort over them, then the dispatcher's riders, then a provider derived
    from the model when none is named. Reads both arguments, changes neither.
    """
    providers = host.get("providers") or {}
    defaults = dict((config.get("agents") or {}).get("defaults") or {})
    host_defaults = (host.get("agents") or {}).get("defaults") or {}
    for key in ("provider", "model", "reasoningEffort"):
        if key in host_defaults:
            defaults[key] = host_defaults[key]
    if parent_model := os.environ.get("RAVEN_PARENT_MODEL", "").strip():
        defaults["model"] = parent_model
        # Re-derived below when the dispatcher could not name the provider: a
        # LiteLLM-backed parent carries no ``provider_name``, and an empty
        # string here launched a child with no provider at all.
        defaults["provider"] = os.environ.get("RAVEN_PARENT_PROVIDER", "").strip()
    if parent_effort := os.environ.get("RAVEN_PARENT_REASONING_EFFORT", "").strip():
        defaults["reasoningEffort"] = parent_effort
    if not defaults.get("provider"):
        model = defaults.get("model") or ""
        for name, block in providers.items():
            if isinstance(block, dict) and model in (block.get("models") or []):
                defaults["provider"] = name
                break
        else:
            # A stored model id leads with its provider (``openrouter/x``); a
            # model listed under none of the blocks still names one that way.
            # Failing that, the host's own choice beats launching with none.
            from raven.providers.registry import split_model_id

            # A non-string model names no provider. It is left to ``Config``
            # validation, whose ValueError host_can_lend_a_key catches; splitting
            # it would raise an AttributeError that escapes that except clause.
            head = split_model_id(model)[0] if isinstance(model, str) else ""
            defaults["provider"] = head if head in providers else host_defaults.get("provider", "")
    return defaults


def _credential_refusal(host: dict, defaults: dict) -> str | None:
    """What the inherited binding is missing, or ``None`` when it can authenticate."""
    from raven.config.schema import Config
    from raven.providers.auth import MissingCredentialsError
    from raven.providers.factory import check_provider_credentials

    inherited = Config.model_validate(
        {
            "providers": host.get("providers") or {},
            "agents": {
                "defaults": {key: defaults[key] for key in ("provider", "model", "reasoningEffort") if key in defaults}
            },
        }
    )
    try:
        check_provider_credentials(inherited)
    except MissingCredentialsError as exc:
        return exc.summary or "the inherited model has no usable credentials"
    return None


def inherit_llm(config: dict, host: dict) -> str:
    """Take the host raven's whole LLM configuration; return what was taken.

    Only reached when the product has no key of its own. The provider block
    is copied wholesale rather than matched by name -- two providers spelled
    the same can be two different endpoints. What is inherited is which
    brains are reachable, which one is chosen, how a model name routes, and
    the host's reasoning effort; deliberately not the rest of
    ``agents.defaults``, which are the product's own operating limits. ``""``
    when the inherited model binding has no usable credentials, which the
    caller treats as a refusal to launch (:func:`inherit_refusal` says why).
    Only the LLM settings are parsed for that check; the original sections
    are copied so newer fields survive.

    ``RAVEN_PARENT_MODEL`` / ``RAVEN_PARENT_REASONING_EFFORT`` are honoured
    on this branch, the fork launchers' own riders: the trunk cli dispatcher
    injects them per spawn so a cli-hosted child follows the parent session's
    model. For a pooled acp product this is a LAUNCH-TIME capture -- the env
    is read once, when the server starts, so a parent ``/model`` switch made
    mid-session does not follow into an already-running child (the fork's
    per-turn form was a cli-lane property; ledgered, D3). On the own-key
    branch the riders are deliberately ignored, as they always were.
    """
    defaults = _inherited_defaults(config, host)
    if _credential_refusal(host, defaults) is not None:
        return ""
    for key in ("providers", "routing"):
        if key in host:
            config[key] = host[key]
    config.setdefault("agents", {}).setdefault("defaults", {}).update(defaults)
    parent_model = os.environ.get("RAVEN_PARENT_MODEL", "").strip()
    if parent_protocol := os.environ.get("RAVEN_PARENT_PROTOCOL", "").strip():
        provider = (host.get("providers") or {}).get(defaults.get("provider") or "")
        if isinstance(provider, dict) and parent_model:
            overrides = provider.setdefault("modelProtocols", {})
            if isinstance(overrides, dict):
                overrides[parent_model] = parent_protocol
    return (
        f"provider={defaults.get('provider')} model={defaults.get('model')} "
        f"reasoning_effort={defaults.get('reasoningEffort')}"
    )


def inherit_refusal(config: dict, host: dict) -> str:
    """Why :func:`inherit_llm` would refuse ``config`` and ``host``, or ``""`` when it would not.

    The launchers' refusal message names the missing credential through
    this call rather than through ``inherit_llm``'s return value: that value
    being ``""`` on a refusal is a contract every launcher branches on,
    scaffolded copies outside this repository included. Changes neither
    argument.
    """
    return _credential_refusal(host, _inherited_defaults(config, host)) or ""


_DENY = "deny"
_EXTRA_DENY_KEYS = ("extraDenyPatterns", "extra_deny_patterns")


def _compiles(pattern: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def inherit_host_denials(config: dict, host: dict) -> list[str]:
    """Carry every refusal the host operator configured into the product config.

    A product runs as a raven of its own, and its permission gate reads only
    the rendered file. So a deny rule written on the host bound the host's own
    tool calls and nothing the host dispatched: the product's gate had never
    heard of it, and the host answers a product's approval requests itself
    (``raven/acp_client/permissions.py``), which is how a command the host
    refuses ran anyway once it was handed to a product.

    Only refusals travel -- the ``deny`` entries of ``permissions.tools`` and
    ``tools.exec.extraDenyPatterns`` -- because they are the half of a policy
    that needs nobody to answer it. A host ``allow`` would loosen the product,
    and a host ``ask`` would only reach an approver that grants it.

    Merged into what the product already says, never replacing it, and a
    refusal always wins: a product ``exec`` rule written as one tier becomes a
    table with that tier as its ``*`` fallback, so the host's patterns can sit
    beside it. An extra deny pattern that does not compile is left behind, as
    the host's own live reader leaves it (``BuiltinRulings``), rather than
    stopping the product from starting. Returns what was carried.
    """
    carried: list[str] = []
    host_permissions = host.get("permissions")
    host_tools = host_permissions.get("tools") if isinstance(host_permissions, dict) else None
    for tool, entry in (host_tools if isinstance(host_tools, dict) else {}).items():
        if entry == _DENY:
            patterns: list[str] = []
        elif isinstance(entry, dict):
            patterns = [pattern for pattern, tier in entry.items() if tier == _DENY and isinstance(pattern, str)]
            if not patterns:
                continue
        else:
            continue
        tools = config.setdefault("permissions", {}).setdefault("tools", {})
        own = tools.get(tool)
        if not patterns:
            tools[tool] = _DENY
            carried.append(tool)
            continue
        if own == _DENY:
            continue
        table = dict(own) if isinstance(own, dict) else ({"*": own} if isinstance(own, str) else {})
        for pattern in patterns:
            table[pattern] = _DENY
            carried.append(f"{tool} {pattern}")
        tools[tool] = table

    host_node = host.get("tools")
    host_exec = host_node.get("exec") if isinstance(host_node, dict) else None
    host_exec = host_exec if isinstance(host_exec, dict) else {}
    extras = next((host_exec[key] for key in _EXTRA_DENY_KEYS if key in host_exec), None)
    wanted = [p for p in extras if isinstance(p, str) and _compiles(p)] if isinstance(extras, list) else []
    if wanted:
        exec_node = config.setdefault("tools", {}).setdefault("exec", {})
        key = next((key for key in _EXTRA_DENY_KEYS if key in exec_node), _EXTRA_DENY_KEYS[0])
        own_extras = exec_node.get(key) if isinstance(exec_node.get(key), list) else []
        added = [pattern for pattern in wanted if pattern not in own_extras]
        exec_node[key] = [*own_extras, *added]
        carried.extend(added)
    return carried


def inherit_plugin_opt_outs(config: dict, host: dict, *, own: Iterable[str] = ()) -> list[str]:
    """Carry the host's ``plugins.disabled`` into the product config.

    An agent's engine scans the host's plugin roots -- ``<home>/plugins`` is
    the host's, since the launcher normally resolves the host's home -- and
    the entry points of the interpreter they share, but it reads its opt-outs
    only from the rendered file. So a plugin the host operator switched off,
    most often one that fails to load, came back in every agent the host
    dispatched.

    ``own`` names the product's own engine plugins, which never travel: the
    product is that plugin, and a host turning it off for its own agent is not
    a request to run the product without it. Merged after what the product
    already disables, in order, without repeats. Returns what was carried.
    """
    host_plugins = host.get("plugins")
    wanted = host_plugins.get("disabled") if isinstance(host_plugins, dict) else None
    if not isinstance(wanted, list):
        return []
    keep = set(own)
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    already = plugins.get("disabled") if isinstance(plugins.get("disabled"), list) else []
    carried: list[str] = []
    for plugin_id in wanted:
        if (
            isinstance(plugin_id, str)
            and plugin_id not in keep
            and plugin_id not in already
            and plugin_id not in carried
        ):
            carried.append(plugin_id)
    if carried:
        config.setdefault("plugins", {})["disabled"] = [*already, *carried]
    return carried


#: Per distribution: the slice keys the host's value replaces, then the keys
#: the host only fills in when the product carries none of its own. Identity is
#: deliberately absent from both -- see :func:`inherit_memory_backend`.
_MEMORY_SLICE_INHERITED: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "everos-memory": (("base_url",), ()),
    "everos-cloud-memory": (("base_url",), ("api_key",)),
}


def inherit_memory_backend(config: dict, host: dict) -> str | None:
    """Point the product's long-term memory where the host's points.

    Which store memories live in is a property of the installation, not of the
    product: the host reads a sub-agent's memories back through *its own*
    backend (``SubagentManager._memory_backend``), so a product writing
    anywhere else leaves that record empty with nothing reporting it. The
    address is the same question one level down -- a product config used to
    carry the stock EverOS port, and a host whose own server moved (every
    second instance on one machine does) would be addressed at whatever
    answered on the stock one.

    What never travels is identity. ``memory.userId`` / ``agentId`` and the
    local slice's ``user_id`` / ``agent_id`` are the product's own, and they
    are what keeps five products' memories apart inside one store. A
    credential the product holds itself is kept for the same reason the secret
    slots keep one; an address is not a credential, so the host's wins.

    ``owned`` is forced off whenever the local backend is the one in force,
    recorded on the host or not: that EverOS process belongs to the host,
    which starts and stops it with its own backend. A product infers ownership
    from a slice naming no root -- the shape every product ships -- so without
    this it answers a host that is merely not serving by starting a second
    server against a root of its own, stranding everything written so far.

    Returns the backend in force, or ``None`` when it is off or neither side
    names one.
    """
    host_memory = host.get("memory") if isinstance(host.get("memory"), dict) else {}
    if "backend" in host_memory:
        # Present and null is the host's off switch, not an absent answer, and
        # it travels like any other value: an operator who turned long-term
        # memory off would otherwise find the products still writing, through
        # a backend their own raven no longer builds.
        named = host_memory["backend"]
        backend = named if isinstance(named, str) and named else None
        config.setdefault("memory", {})["backend"] = backend
    else:
        own_memory = config.get("memory") if isinstance(config.get("memory"), dict) else {}
        backend = own_memory.get("backend")
    if not isinstance(backend, str) or not backend:
        return None
    distribution = MEMORY_BACKEND_DISTRIBUTIONS.get(backend)
    if distribution is None:
        return backend
    replaces, fills = _MEMORY_SLICE_INHERITED.get(distribution, ((), ()))
    host_slice = _plugin_slice(host, distribution)
    own_slice = _plugin_slice(config, distribution)
    carried = {key: host_slice[key] for key in replaces if str(host_slice.get(key) or "").strip()}
    carried.update(
        {
            key: host_slice[key]
            for key in fills
            if str(host_slice.get(key) or "").strip() and not str(own_slice.get(key) or "").strip()
        }
    )
    if backend == SHIPPED_DEFAULT_BACKEND:
        carried["owned"] = False
    if carried:
        config.setdefault("plugins", {}).setdefault("config", {}).setdefault(distribution, {}).update(carried)
    return backend


def _plugin_slice(data: dict, distribution: str) -> dict:
    """``plugins.config[distribution]`` of a raw config, or an empty dict."""
    plugins = data.get("plugins")
    block = plugins.get("config") if isinstance(plugins, dict) else None
    found = block.get(distribution) if isinstance(block, dict) else None
    return found if isinstance(found, dict) else {}


def inherit_media_image(config: dict, host: dict) -> dict:
    """Take the host raven's image-generation section, and keep following it.

    The section is resolved the way the host resolves it for its own
    ``image_generate`` -- ``tools.media.image`` with what it borrows, the named
    provider's address and key or, for a keyless section calling OpenRouter, the
    OpenRouter key -- then written whole into the agent's config, and
    ``selectionConfig`` is pointed at the host's config file so a model or
    quality changed in Settings later reaches the product's next generation
    without a re-render. A host with no image section leaves an empty section,
    which is how the product's tool learns to withhold itself. Returns the
    section written.

    Called before a product's own ``.env`` overrides land, so an explicit
    product-side key or base still wins over the inherited one.
    """
    from raven.config.schema import live_media_tool_config

    host_tools = host.get("tools") or {}
    host_image = (host_tools.get("media") or {}).get("image")
    section = live_media_tool_config(host_image, host.get("providers"), "image")
    image = section.model_dump(by_alias=True, exclude_unset=True) if section is not None else {}
    image["selectionConfig"] = str(raven_home() / CONFIG_FILENAME)
    media = config.setdefault("tools", {}).setdefault("media", {})
    media["image"] = image
    proxy = (host_tools.get("media") or {}).get("proxy")
    if proxy and not media.get("proxy"):
        media["proxy"] = proxy
    return image


def product_state_root(product: str, *, override: str | None = None) -> Path:
    """Where a product keeps its WORK -- never in its own folder.

    The default seats every product's working state (repos, instance buckets,
    flow stores, rendered configs) under the host home's sub-agent sessions,
    one directory per product name; ``override`` is the resolved value of the
    product's own state-root variable, when it names one. The engine's own
    Agent home is deliberately NOT here: it goes through
    :func:`product_acp_home`, outside the host Agent home, because the host
    hands its home over as a session's working directory and the runtime
    refuses a working directory that contains the engine's home. Work where
    the work is, the home in the data directory.
    """
    if override:
        return Path(override).expanduser()
    return raven_home() / "workspace" / "subagent_sessions" / product


def host_agent_home() -> Path:
    """The Agent home the HOST raven is configured to use.

    Read from the host config rather than assumed: an operator who moved
    ``agents.defaults.workspace`` moved the tree the host hands out as a
    session's working directory, and the containment guard below must be
    measured against that tree, not against the default. The sentinel and the
    comparison are the trunk's own (``Config.workspace_path`` compares the raw
    value against the paper's ``WORKSPACE_DEFAULT_SENTINEL``, no trimming), so
    the two readers of one question cannot drift apart.
    """
    raw = ((host_config().get("agents") or {}).get("defaults") or {}).get("workspace")
    if isinstance(raw, str) and raw and raw != WORKSPACE_DEFAULT_SENTINEL:
        return Path(raw).expanduser()
    return raven_home() / "workspace"


def _outside(path: Path, home: Path) -> bool:
    """True when ``path`` is neither ``home`` nor anything under it.

    The one property the runtime's own guard cares about: a working directory
    must not contain the agent home it is handed to, and the host hands its
    Agent home over as the working directory.

    Asked of the filesystem, not only of the spelling: ``resolve()`` does not
    fold case, APFS default volumes do, so a home spelled as a case variant of
    ``RAVEN_HOME`` would pass a string comparison while naming the very tree
    being handed over -- the alias-blind fail-open an adversarial review
    reproduced live. Every EXISTING ancestor of the candidate is compared with
    ``os.path.samefile`` (st_dev/st_ino, which sees through case variants and
    firmlinks alike); the string comparison stays for tails that do not exist
    yet and for a home that does not exist at all.
    """
    p = Path(path).expanduser().resolve()
    h = Path(home).expanduser().resolve()
    if p == h or h in p.parents:
        return False
    if h.exists():
        for ancestor in (p, *p.parents):
            if not ancestor.exists():
                continue
            try:
                if os.path.samefile(ancestor, h):
                    return False
            except OSError:
                continue
    return True


def _creatable(path: Path) -> bool:
    """True when ``path`` could be made -- asked without making anything.

    Walk up to the nearest ancestor that exists and ask whether it is a
    directory this process may create under. Advisory, not authoritative: a
    run whose effective uid overrides the write bit is told yes and proceeds
    exactly as before this check. What it removes is the case where the
    answer is knowably no and the operator would otherwise learn it from a
    bare ``PermissionError`` naming nothing they can act on.
    """
    probe = Path(path).expanduser()
    for ancestor in (probe, *probe.parents):
        if ancestor.exists():
            return ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK)
    return False


def _instance_tag() -> str:
    """A name for THIS raven instance, for a path shared with its siblings.

    Two instances on one machine are told apart by their ``RAVEN_HOME``, and
    the sibling fallback in :func:`product_acp_home` puts its directory beside
    the host Agent home -- a place their siblings can reach. Without the
    instance in the name, two instances under one parent would share one
    engine home, which is the isolation ``RAVEN_HOME`` exists to give. The
    directory's own name for legibility, and a digest of the resolved path
    because two instances can be named the same under different parents.
    """
    resolved = raven_home().expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{safe_path_segment(resolved.name)}-{digest}"


def _acp_home_var(product: str) -> str:
    """The product's own override variable, derived the way its siblings are.

    ``raven-code`` answers to ``CODE_ACP_HOME`` the way it answers to
    ``CODE_STATE_ROOT``: the ``raven-`` prefix drops, the rest upper-cases
    with hyphens as underscores.
    """
    stem = product.removeprefix("raven-").replace("-", "_").upper()
    return f"{stem}_ACP_HOME"


def product_acp_home(product: str, *, override: str | None = None) -> Path:
    """The ACP engine's own Agent home -- never inside the host's.

    The host hands a session's working directory to whatever it dispatches
    to, and on a surface with no checkout of its own -- a web page -- that
    directory is the host's Agent home. A raven engine refuses to work in a
    directory that CONTAINS its own home, because the per-turn checkpoint
    runs ``add -A`` over the working directory and would commit its config
    and its provider tokens into a shadow repository. Homing an engine under
    the host's Agent home therefore made every dispatch to it fail before it
    started, while capability probing still passed (it opens its session on a
    temporary directory). So the engine is homed under the raven DATA
    directory, which is never handed out as a working directory.

    Checked against the CONFIGURED host Agent home (:func:`host_agent_home`):
    an operator who points ``agents.defaults.workspace`` at ``$RAVEN_HOME``
    -- or any ancestor of it -- puts the data directory back inside the very
    tree being handed over, and the refusal returns. When the default lands
    inside it, the engine is homed beside the host's Agent home instead,
    tagged per instance so two ravens under one parent do not share it.

    Being outside is not on its own enough: beside a home like ``~`` sits
    ``/Users``, which no run may write to -- so a placement is refused when
    it is unusable as well as when it is inside, with the product's own
    override variable named instead of a bare ``PermissionError``.

    ``override`` (the resolved value of that variable) wins outright: where
    the home goes is then the operator's business, reachability included.
    The product's state root is untouched by all of this -- repos, instance
    buckets and flow stores are work, and stay where the work is.
    """
    if override:
        return Path(override).expanduser()
    host = host_agent_home()
    candidate = raven_home() / "subagent_sessions" / product / "acp"
    if not _outside(candidate, host):
        candidate = host.parent / f".{product}-{_instance_tag()}" / "acp"
    if not _outside(candidate, host):
        raise SystemExit(
            f"error: cannot place the {product} ACP home outside the host Agent home "
            f"({host}); set {_acp_home_var(product)} to a directory outside it"
        )
    if not _creatable(candidate):
        raise SystemExit(
            f"error: the {product} ACP home {candidate} cannot be created -- its nearest "
            f"existing parent is not a directory this run may write to; set "
            f"{_acp_home_var(product)} to a writable directory outside the host Agent home ({host})"
        )
    return candidate


def seed_once(target: Path, render: Callable[[], str]) -> bool:
    """Write ``render()`` to ``target`` unless it already exists.

    Once is the contract: the workspace copy is the live one afterwards. That
    suits a file the operator owns from then on; text the product owns -- an
    identity or a contract the product's gates enforce -- goes through
    :func:`write_generated`, or a later release ships gates against a prompt
    nobody refreshed. The render is not evaluated when the target exists.
    Returns whether it wrote.
    """
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(), encoding="utf-8", newline="")
    return True


def local_addendum(target: Path) -> Path:
    """``agent.local.md`` beside ``agent.md``: the operator's half of a generated file."""
    return target.with_name(f"{target.stem}.local{target.suffix}")


def _generated_receipt(target: Path) -> Path:
    return target.with_name(f".{target.name}.generated.sha256")


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_receipt(receipt: Path) -> dict[str, set[str]]:
    """Each writer named in ``receipt`` and the digests it recorded; empty when unreadable."""
    try:
        lines = receipt.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    writers: dict[str, set[str]] = {}
    for line in lines:
        parts = line.split()
        if len(parts) == 2:
            writers.setdefault(parts[0], set()).add(parts[1])
    return writers


def _receipt_text(owner: str, digests: Iterable[str]) -> str:
    return "".join(f"{owner} {digest}\n" for digest in sorted(digests))


# A new Windows file takes its folder's inherited ACL whatever mode os.open is
# given, so a copy of a prompt kept private in a shared folder would be open
# to every account the folder lets in.
_BACK_UP_BY_LINK = os.name == "nt"


def _back_up(target: Path, raw: bytes) -> Path:
    """Keep ``target``'s bytes beside it under a name no earlier backup holds.

    Never readable by more accounts than ``target``, since it holds the
    operator's private text. On POSIX, a copy of ``raw`` created owner-only
    and never wider than ``target``. On Windows, a hard link to ``target``
    itself, so the backup is the same file with the same ACL and no bytes are
    written; a volume that cannot link refuses the backup instead of copying.
    """
    stem = f"{target.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    names = (target.with_name(f"{stem}-{attempt}" if attempt else stem) for attempt in itertools.count())
    if _BACK_UP_BY_LINK:
        for backup in names:
            try:
                os.link(target, backup)
            except FileExistsError:
                continue
            except OSError as exc:
                raise OSError(
                    f"cannot back it up: on Windows only a hard link keeps the file's access rights, "
                    f"and this volume refused one ({exc})"
                ) from exc
            return backup
    try:
        mode = stat.S_IMODE(target.stat().st_mode) & 0o600
    except OSError:
        mode = 0o600
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for backup in names:
        try:
            fd = os.open(backup, flags, mode)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return backup
    raise AssertionError("unreachable: itertools.count does not end")


def write_generated(
    target: Path,
    product_text: str | None,
    *,
    owner: str,
    legacy_digests: Iterable[str] = (),
    adopt: bool = True,
    notify: Callable[[str], None] | None = None,
) -> str:
    """Write the product's current text over ``target``; the action taken.

    For a workspace file the product owns -- an identity, a contract its gates
    enforce, a tool guide. Regenerated on every launch, so a release or a
    config change reaches the model at the next launch instead of never.
    The operator's half lives beside it in :func:`local_addendum` and is
    appended after the product text.

    Whether the file on disk is the product's is read off a receipt beside it
    naming ``owner`` and the digest of the last text it wrote, so a file
    another binding's launch rewrote is still recognised, and one another
    product wrote into the same home is not taken for this one's.
    ``legacy_digests`` names texts written before receipts existed. A file
    that is neither -- edited in place, another product's, or an older
    release's copy no digest names -- is backed up to a ``.bak-*`` file first
    (:func:`_back_up`) and ``notify`` names it, and the target is replaced
    only once that backup is on disk. ``adopt=False`` is for a caller whose
    directory may not be its own -- a plugin writes into whatever home it is
    granted: a file this owner's receipt never named and no digest matches is
    then left as it is.

    ``product_text=None`` turns the file off: a file this product wrote is
    removed; an edited one is kept, being the operator's now, and stops being
    tracked; another product's file and receipt are left alone.

    Returns ``"written"``, ``"unchanged"``, ``"removed"``, ``"kept"`` or
    ``"absent"``. Raises ``OSError`` or ``UnicodeDecodeError`` when a file
    cannot be read or written; an addendum that is not UTF-8 raises the
    latter naming the addendum. An edit is never replaced before its backup
    lands, and whatever text a failure leaves in the target, the receipt
    still names it.
    """
    if not owner or any(char.isspace() for char in owner):
        raise ValueError(f"owner must be one word, not {owner!r}")
    say = notify or (lambda _message: None)
    receipt = _generated_receipt(target)
    local = local_addendum(target)
    with write_transaction(target):
        raw = target.read_bytes() if target.is_file() else None
        # Line endings folded the way read_text folds them, so a copy written
        # with CRLF before seeds pinned newline="" still matches its digest.
        current = None if raw is None else raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        writers = _read_receipt(receipt)
        recorded = writers.get(owner, set())
        wanted = product_text
        if wanted is not None and local.is_file():
            try:
                addendum = local.read_text(encoding="utf-8").strip("\n")
            except UnicodeDecodeError as exc:
                # Hand-written, often by an editor saving in a legacy code
                # page: the error names this file, not the generated one.
                exc.reason = f"{local} is not UTF-8 text; save it as UTF-8"
                raise
            if addendum.strip():
                wanted = wanted.rstrip("\n") + "\n\n" + addendum + "\n"
        ours = current is not None and (current == wanted or _text_digest(current) in recorded | set(legacy_digests))

        if wanted is None:
            if local.is_file():
                say(f"{local} is not applied: {target.name} is turned off")
            if current is None:
                action = "absent"
            elif ours:
                target.unlink()
                action = "removed"
            else:
                if recorded:
                    say(f"kept {target}: it no longer holds the text this product wrote, and it is turned off")
                action = "kept"
            if recorded or action == "removed":
                receipt.unlink(missing_ok=True)
            return action

        if current is not None and not ours:
            if not adopt and not recorded:
                say(
                    f"left {target} as it is: nothing shows this product wrote it. If it is an edit of "
                    f"this product's own prompt from before prompts were regenerated, move the lines you "
                    f"still need into {local} and delete it; the next write puts the product text there"
                )
                return "kept"
            backup = _back_up(target, raw or b"")
            held = _text_digest(current)
            writer = next((name for name, digests in writers.items() if held in digests), None)
            # Not "edited" unless nothing else explains it: a copy an older
            # release wrote before receipts existed lands here too, and so
            # does another product's file, and nobody touched either.
            cause = f"it holds what {writer} wrote" if writer else "it was edited in place, or left by an older release"
            say(
                f"{target} does not hold the text this product last wrote ({cause}); backed up "
                f"to {backup}. It is regenerated on every launch: keep only the lines you still "
                f"need in {local}"
            )
        digest = _text_digest(wanted)
        if current == wanted:
            if writers != {owner: {digest}}:
                atomic_replace(receipt, _receipt_text(owner, {digest}), newline="")
            return "unchanged"
        # Two files cannot change in one step, so the receipt names both texts
        # while the target changes: a failure between the steps leaves a
        # target the next launch still recognises, whichever text it holds.
        # The old text is safe to name even when it was an edit -- its
        # backup is already on disk.
        pending = {digest} if current is None else {digest, _text_digest(current)}
        atomic_replace(receipt, _receipt_text(owner, pending), newline="")
        atomic_replace(target, wanted, newline="")
        if pending != {digest}:
            atomic_replace(receipt, _receipt_text(owner, {digest}), newline="")
        return "written"


def mode_catalogue(
    modes_dir: Path,
    labels: dict[str, tuple[str, str]],
    *,
    baseline: str,
    overlay_keys: frozenset[str],
    resolve: Callable[[dict], tuple[Any, dict]],
) -> dict:
    """The ``acp.modes`` block: one entry per mode, each carrying its own diff.

    Diffs, not merged blocks: the overlay reaches a product plugin's hooks
    through the session policy as ``ctx.metadata["mode_overlay"]``, merged
    over the baseline per session. The machine here owns the trunk contract
    -- loading ``<mode>.json`` overlays, refusing unknown top-level keys,
    skipping a labeled mode whose overlay file is absent, and stamping the
    resolved cap into the entry so every mode declares its own budget (the
    baseline included, whose diff is empty but whose cap is not; the overlay
    carries no copy -- the loop tells its hooks the enforced cap directly as
    ``ctx.max_iterations``). ``resolve(overlay)`` is the product's half: it
    returns the
    mode's iteration cap and the diff the plugin should see -- both sides of
    vocabulary the trunk must not learn. An empty dict when the product ships
    no modes directory, which leaves ``acp.modes`` unset in the rendered
    config rather than absent from the schema: raven defaults an unset
    ``acp.modes`` to its own three built-in tiers, so ``session/set_mode``
    answers with those instead of method-not-found.
    """
    if not modes_dir.is_dir():
        return {}
    catalogue: dict = {}
    for mode, (name, description) in labels.items():
        overlay: dict = {}
        if mode != baseline:
            overlay_file = modes_dir / f"{mode}.json"
            if not overlay_file.is_file():
                continue
            overlay = json.loads(overlay_file.read_text(encoding="utf-8"))
            unknown = sorted(set(overlay) - overlay_keys)
            if unknown:
                raise SystemExit(
                    f"{overlay_file}: unsupported top-level key(s) {', '.join(unknown)}; "
                    f"an overlay carries only {', '.join(sorted(overlay_keys))}"
                )
        cap, entry_overlay = resolve(overlay)
        entry: dict = {
            "name": name,
            "description": description,
            "maxToolIterations": cap,
            "overlay": entry_overlay,
        }
        # A mode's reasoning effort is the trunk's own knob (`AcpModeConfig.
        # reasoningEffort`: every call a session in that mode makes asks for
        # it), declared in the same place a product writes its other defaults.
        # Lifted out of the overlay onto the entry so the trunk dispenses it,
        # rather than the loop reading a hook bag for it.
        effort = ((overlay.get("agents") or {}).get("defaults") or {}).get("reasoningEffort")
        if isinstance(effort, str) and effort.strip():
            entry["reasoningEffort"] = effort.strip()
        catalogue[mode] = entry
    return catalogue


def sweep_stale_renders(root: Path) -> None:
    """Remove rendered configs whose server is gone, by pid liveness."""
    for stale in root.glob(".config.rendered.*.json"):
        try:
            pid = int(stale.name.split(".")[3])
        except (IndexError, ValueError):
            stale.unlink(missing_ok=True)
            continue
        if not pid_alive(pid):
            stale.unlink(missing_ok=True)


def write_rendered(config: dict, root: Path, *, own_plugins: Iterable[str] = ()) -> Path:
    """Write the rendered config under ``root``, owner-only on POSIX, named by pid.

    The location is the mechanism: raven derives its data dir from the
    config file's own parent, so wherever this file goes, sessions and cache
    go too. POSIX permissions are owner-only because the render is where the
    secrets landed; Windows uses the containing directory's inherited ACLs.
    The pid in the name is what :func:`sweep_stale_renders` reads back.

    The host's refusals (:func:`inherit_host_denials`), plugin opt-outs
    (:func:`inherit_plugin_opt_outs`) and memory address
    (:func:`inherit_memory_backend`) are merged in here rather than by each
    launcher: every product render ends in this call, so a launcher cannot
    write a config that forgot them. ``own_plugins`` is the product's engine,
    which the opt-outs leave alone; a launcher that names none inherits them
    all.
    """
    host = host_config()
    inherit_host_denials(config, host)
    inherit_plugin_opt_outs(config, host, own=own_plugins)
    inherit_memory_backend(config, host)
    rendered = root / f".config.rendered.{os.getpid()}.json"
    fd = os.open(rendered, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        if hasattr(os, "fchmod"):
            os.fchmod(stream.fileno(), 0o600)
        json.dump(config, stream, indent=2, ensure_ascii=False)
    return rendered
