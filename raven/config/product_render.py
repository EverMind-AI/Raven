"""The launcher library: render a product config and hand it to installed raven.

A product under ``agents/`` is a folder, not a fork: a published
``config.json``, a ``.env`` for secrets, and a launcher that renders the two
into a private copy and execs ``python -m raven acp``. What every such
launcher repeats is here -- reading a setting from the process environment
with a ``.env`` fallback, merging declared secret slots, inheriting the
host's LLM block when the product has no key of its own, pinning a state
root under the host's home and an engine home OUTSIDE it (the home lives in
the raven data directory, the work stays where the work is), seeding a
workspace file once, assembling the
``acp.modes`` catalogue from overlay files, sweeping stale renders by pid
liveness, and writing the rendered copy owner-only.

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
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from raven.contracts.path_policy import CONFIG_FILENAME, WORKSPACE_DEFAULT_SENTINEL
from raven.home import raven_home
from raven.utils.paths import safe_path_segment


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

    Read as JSON through the path paper's answer -- the host propagates its
    ``RAVEN_HOME`` into a launcher process (builtin_agents does), so
    ``raven_home()`` here is the host's home.
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


def inherit_llm(config: dict, host: dict) -> str:
    """Take the host raven's whole LLM configuration; return what was taken.

    Only reached when the product has no key of its own. The provider block
    is copied wholesale rather than matched by name -- two providers spelled
    the same can be two different endpoints. What is inherited is which
    brains are reachable, which one is chosen, how a model name routes, and
    the host's reasoning effort; deliberately not the rest of
    ``agents.defaults``, which are the product's own operating limits. ``""``
    when the host has no provider key to lend, which the caller treats as a
    refusal to launch.

    ``RAVEN_PARENT_MODEL`` / ``RAVEN_PARENT_REASONING_EFFORT`` are honoured
    on this branch, the fork launchers' own riders: the trunk cli dispatcher
    injects them per spawn so a cli-hosted child follows the parent session's
    model. For a pooled acp product this is a LAUNCH-TIME capture -- the env
    is read once, when the server starts, so a parent ``/model`` switch made
    mid-session does not follow into an already-running child (the fork's
    per-turn form was a cli-lane property; ledgered, D3). On the own-key
    branch the riders are deliberately ignored, as they always were.
    """
    providers = host.get("providers") or {}
    if not any(isinstance(p, dict) and p.get("apiKey") for p in providers.values()):
        return ""
    for key in ("providers", "routing"):
        if key in host:
            config[key] = host[key]
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
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

            head = split_model_id(model)[0]
            defaults["provider"] = head if head in providers else host_defaults.get("provider", "")
    if parent_protocol := os.environ.get("RAVEN_PARENT_PROTOCOL", "").strip():
        provider = providers.get(defaults.get("provider") or "")
        if isinstance(provider, dict) and parent_model:
            overrides = provider.setdefault("modelProtocols", {})
            if isinstance(overrides, dict):
                overrides[parent_model] = parent_protocol
    return (
        f"provider={defaults.get('provider')} model={defaults.get('model')} "
        f"reasoning_effort={defaults.get('reasoningEffort')}"
    )


def inherit_exec_policy(config: dict, host: dict) -> None:
    """Carry the host's deletion-safety choice into the product's config.

    ``tools.exec.allowDestructiveCommands`` is set at the host (the page's own
    toggle) by a person who expects every agent on this machine to follow it;
    without this the design worker kept refusing ``rm -rf`` after the toggle
    went on. Only a value the host states travels, and never over one the
    product's own config already states.
    """
    host_exec = (host.get("tools") or {}).get("exec") or {}
    if "allowDestructiveCommands" not in host_exec:
        return
    exec_section = config.setdefault("tools", {}).setdefault("exec", {})
    exec_section.setdefault("allowDestructiveCommands", host_exec["allowDestructiveCommands"])


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

    Once is the contract: the workspace copy is the live one afterwards, and
    a product update must not silently overwrite what an operator tuned in
    place. The render is not evaluated when the target exists. Returns
    whether it wrote.
    """
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(), encoding="utf-8")
    return True


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
        catalogue[mode] = {
            "name": name,
            "description": description,
            "maxToolIterations": cap,
            "overlay": entry_overlay,
        }
    return catalogue


def sweep_stale_renders(root: Path) -> None:
    """Remove rendered configs whose server is gone, by pid liveness."""
    for stale in root.glob(".config.rendered.*.json"):
        try:
            pid = int(stale.name.split(".")[3])
            os.kill(pid, 0)
        except (IndexError, ValueError, ProcessLookupError):
            stale.unlink(missing_ok=True)
        except PermissionError:
            continue


def write_rendered(config: dict, root: Path) -> Path:
    """Write the rendered config under ``root``, owner-only, named by pid.

    The location is the mechanism: raven derives its data dir from the
    config file's own parent, so wherever this file goes, sessions and cache
    go too. Owner-only because the render is where the secrets landed; the
    pid in the name is what :func:`sweep_stale_renders` reads back.
    """
    rendered = root / f".config.rendered.{os.getpid()}.json"
    fd = os.open(rendered, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2, ensure_ascii=False)
    return rendered
