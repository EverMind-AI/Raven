"""Agent rows discovered from the ``agents/`` product tree.

Raven ships agent products under ``agents/`` -- one folder per product, each a
launcher (``run.py``) that renders the product's config and serves it on the
installed raven, plus a ``subagent.json`` manifest describing how to invoke
it. This module turns that tree into table rows so a raven that *has* the tree
offers them without anyone registering them by hand.

Discovery, deliberately, rather than a hard-coded list: adding a product is
then adding a folder, which is the same reason the onboarding wizard's
discovery scans instead of naming them. And discovery only -- nothing here
writes config. A row is materialized from the manifest on every start, so a
folder whose manifest changes (a new command template after an upgrade) is
picked up without a stored copy of the old one to contradict it.

Every ordinary install has the tree: a wheel carries it at ``raven/agents``,
and :func:`_install_packaged_tree` copies it out to the raven home on first
use, which is why the packaged branch of :func:`agents_root` is live rather
than vestigial. Where it is absent -- a wheel built from an sdist, which has
neither the tree nor a ``.git`` to enumerate it from -- there is nothing to
discover and the table is exactly what it was before this module existed. That
is the intended degradation, not a gap.

**Readiness decides ``enabled``, not whether the row exists.** A folder whose
launcher file is gone, or whose declared engine wheel is not importable,
cannot start: "Registering an agent that cannot start puts a name in the
roster the dispatching model will pick and then fail on." Dropping the row
entirely would hide the folder from the operations view too, where "present
but not set up" is exactly what a user needs to see. So it is listed and
disabled, with the reason on the row. A missing credential is deliberately
*not* a readiness reason any more: the launchers inherit the host's provider
block when the folder holds no key of its own, and a launcher that finds
nothing anywhere refuses loudly at dispatch -- a discovery-time credential
verdict would be a second reader of that fact, free to disagree with the one
that rules.

This module owns the two facts about where the tree is and what state a
folder is in; the onboarding wizard imports them rather than keeping its own
answers. Two readers that disagree about whether a folder is ready would offer
to set up one this refuses to advertise.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, NamedTuple

from loguru import logger

if TYPE_CHECKING:
    from raven.config.schema import ThirdPartyAcpSubagentConfig, ThirdPartyCliSubagentConfig

__all__ = [
    "agents_root",
    "api_key_var",
    "discover_product_rows",
    "host_can_lend_a_key",
    "merge_product_seeds",
    "product_folder",
    "product_state",
    "Readiness",
]

_PLACEHOLDER_FIELDS = ("command", "resumeCommand", "cwd")
"""The manifest fields carrying ``{SUBAGENT_DIR}`` / ``{PYTHON}``. Exactly the
set each product's own ``install.py`` substitutes, and a field resolved here
but not there would mean a hand-registered row and a discovered one
disagreeing. ``resumeCommand`` is on the list because discovery still accepts
``kind: "cli"`` manifests, and the cli backend executes that field as a
command line exactly the way it executes ``command`` -- left as a template, a
stateful cli product would resume on a literal ``{PYTHON}``. ``cwd`` is on
the list for the acp manifests: an acp entry with no ``cwd`` falls back to
the calling task's workspace, which is part of the pool's launch key -- so
every new workspace would relaunch the server and kill the sessions the old
one was serving."""

_ENGINE_FIELD = "engine"
"""The manifest key declaring the product's engine wheel, as
``{"package": "<import name>", "wheel": "<distribution name>"}``. Declared in
the manifest rather than derived here: the launcher already names its engine
(``ENGINE_PACKAGE`` in its ``run.py``), and a mapping kept in this module
would be a hard-coded product list -- the thing discovery exists to avoid.
The config schema ignores the key, so a stored row never carries a stale copy
of it; the manifest is read fresh on every scan."""


def agents_root() -> Path | None:
    """Where the ``agents/`` product tree is, or ``None`` when this install has none.

    Three places, checked in that order:

    - **under the raven home** (``$RAVEN_HOME`` or ``~/.raven/agents``) -- an
      installed tree. First because it is the only writable one that survives
      an upgrade: a product's ``.env`` credential lives inside its folder, so a
      tree under site-packages loses every key when the wheel is replaced;
    - **beside the package** -- an editable install or a plain checkout leaves
      ``raven/__init__.py`` inside the clone, so the tree is two levels up;
    - **inside the package** -- a wheel carries the tree at ``raven/agents``,
      the same way ``bridge`` is packaged.

    An install with none of the three has nothing to discover, and that is the
    whole gate: no flag, no setting, and a table byte-identical to what it was
    before discovery existed.
    """
    import raven
    from raven.home import raven_home

    package = Path(raven.__file__).resolve().parent
    installed, packaged = raven_home() / "agents", package / "agents"
    if packaged.is_dir():
        _install_packaged_tree(packaged, installed)
    for candidate in (installed, package.parent / "agents", packaged):
        if candidate.is_dir():
            return candidate
    return None


def _install_packaged_tree(packaged: Path, installed: Path) -> None:
    """Copy a wheel's own tree out to the raven home, once per raven version.

    The reason this exists at all: a product's ``.env`` credential is written
    into its own folder, and a wheel install is replaced wholesale on every
    upgrade -- so a tree left under site-packages loses every key each time,
    and the agents fall back to inheriting the host's LLM until someone types
    the keys again. Copied out once, the folders sit in a directory no upgrade
    touches.

    Version-stamped rather than content-compared, and the stamp is what makes
    deleting a folder work: within one raven version this is a no-op, so a
    folder the user removed stays removed. An upgrade restores what the new
    release ships, which is the one case where "the release decides" is the
    right answer.

    An existing folder is refreshed in place with its ``.env`` left alone --
    the packaged tree never carries one (the build enumerates git-tracked
    files and refuses secrets), so the copy has nothing to overwrite it with.
    Failure is logged, never raised: not having the products is a smaller
    problem than not starting.
    """
    import shutil

    from raven import __version__

    stamp = installed / ".raven-version"
    try:
        if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == __version__:
            return
        for folder in sorted(packaged.iterdir()):
            if not (folder / "subagent.json").is_file():
                continue
            shutil.copytree(
                folder,
                installed / folder.name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                dirs_exist_ok=True,
            )
        # After the folders, not before: nothing here may create `installed`
        # ahead of a copy that might fail. An existing directory is what makes
        # `agents_root` choose it, so creating it first turns a failed copy
        # from "degrade to the packaged tree" into "an empty tree and no
        # agents at all".
        installed.mkdir(parents=True, exist_ok=True)
        stamp.write_text(__version__, encoding="utf-8")
        logger.info("Installed the packaged agent products into {}", installed)
    except Exception as exc:  # noqa: BLE001 - the products are optional, starting is not
        logger.warning("Could not install the packaged agent products into {}: {}", installed, exc)


def api_key_var(folder_name: str) -> str:
    """``DESIGN_API_KEY`` for ``raven-design``: the folder name without its
    ``raven-`` prefix, upper-cased. Mirrors the ``REQUIRED_SECRETS`` name each
    product's launcher reads from its ``.env``."""
    stem = folder_name[len("raven-") :] if folder_name.startswith("raven-") else folder_name
    return stem.upper().replace("-", "_") + "_API_KEY"


def host_can_lend_a_key() -> bool:
    """Whether ``inherit_llm`` in the launchers would find anything to inherit.

    A folder with no key of its own is not stranded: each launcher reads the
    host raven's ``config.json`` (through ``raven.config.product_render``) and
    copies its whole provider block, so the common case needs no credential
    anywhere near the tree.

    Mirrors ``inherit_llm``'s own test rather than asking ``providers.auth``,
    and the difference is the whole point. ``inherit_llm`` accepts exactly one
    shape -- a literal ``apiKey`` on some provider section. A host signed in
    through OAuth is configured by auth's rule and has nothing to lend by the
    launcher's, because those credentials live under ``~/.raven/oauth/``.
    Asking auth here would advertise an agent that dies at the first dispatch.

    The same file the launcher reads (``$RAVEN_HOME`` or ``~/.raven``), for
    the same reason: two readers of one credential that disagree would have
    the roster offer what the launcher then refuses.
    """
    from raven.contracts.path_policy import CONFIG_FILENAME
    from raven.home import raven_home

    config = raven_home() / CONFIG_FILENAME
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    providers = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(providers, dict):
        return False
    return any(isinstance(p, dict) and str(p.get("apiKey") or "").strip() for p in providers.values())


def _resolved_python() -> str:
    """The interpreter the discovered command invokes.

    ``SUBAGENT_PYTHON`` then this interpreter, which is ``install.py``'s own
    default order minus its ``--python`` flag (there is no flag to pass here).
    The launchers import raven (``raven.config.product_render``) and exec
    ``python -m raven acp``, so the interpreter must be one with raven
    installed -- which this process's own is by definition, and an override
    must be too.
    """
    return os.environ.get("SUBAGENT_PYTHON", "").strip() or sys.executable


class Readiness(NamedTuple):
    """Why a folder cannot run, as a kind the caller can branch on plus the text.

    The text alone was not enough for the fork-era tree, where every reason
    reached the page as one status and the page offered the wrong action for
    half of them. The kind survives that lesson: a client branches on it
    without matching a sentence.
    """

    kind: str
    """"" when the folder is ready; otherwise ``launcher`` / ``engine`` / ``route``."""
    detail: str

    @property
    def ready(self) -> bool:
        return not self.kind


_READY = Readiness("", "")


def _is_absolute_path(token: str) -> bool:
    """Whether a command token is an absolute path, in either OS's spelling.

    Both pure flavors rather than ``token.startswith("/")``: the prefix test
    called every drive-letter token relative, so on native Windows the
    launcher probes below matched no token, checked no file, and reported
    every folder ready -- a product whose launcher was gone still reached the
    roster as exactly the name "the dispatching model will pick and then fail
    on". The pure classes judge the token's shape on any platform, which is
    also what makes the Windows arm testable from POSIX.

    Cross-shape matches are deliberately fail-closed: a Windows-shaped token
    on a POSIX host still goes through the existence check and reads as
    missing, disabling the row rather than advertising a command this host
    cannot run (and symmetrically for ``/``-rooted tokens on Windows).

    Tokens come from ``str.split()``, so a path containing spaces arrives here
    as fragments. The command templates the manifests and each ``install.py``
    emit keep their paths space-free, and that constraint is cheaper than
    re-tokenizing every stored row's command line.
    """
    return PureWindowsPath(token).is_absolute() or PurePosixPath(token).is_absolute()


def _launcher_missing(entry: dict) -> str:
    """The first file the resolved command names that is not on disk, or ``""``.

    Read off the command's own tokens rather than a hard-coded filename: a row
    is a command line, and whether the files it names exist is the only
    question that decides whether it can start. A token that is not an
    absolute path (a flag, something on ``PATH``) is not checked -- the
    interpreter and the launcher are the two absolute paths every product
    command carries.
    """
    command = str(entry.get("command") or "")
    for token in command.split():
        if _is_absolute_path(token) and not Path(token).exists():
            return token
    return ""


def _declared_engine(entry: dict) -> tuple[str, str]:
    """The engine the manifest declares, as ``(import package, wheel name)``.

    ``("", "")`` for a product whose whole capability is raven's own -- most
    are. A malformed declaration reads as none: the launcher still refuses at
    dispatch, so a manifest typo degrades to a late loud failure rather than a
    scan crash.
    """
    declared = entry.get(_ENGINE_FIELD)
    if not isinstance(declared, dict):
        return "", ""
    package = str(declared.get("package") or "").strip()
    wheel = str(declared.get("wheel") or "").strip() or package
    return package, wheel


def _engine_ready(package: str) -> bool:
    """Whether the declared engine package is importable where raven runs.

    ``find_spec`` rather than an import: the question is presence, and
    importing a whole engine to answer it would pay its import cost on every
    scan. The same probe the launcher's own refusal uses (``run.py`` checks
    ``importlib.util.find_spec(ENGINE_PACKAGE)``), so the roster and the
    launcher cannot disagree about whether the engine is there.
    """
    try:
        return importlib.util.find_spec(package) is not None
    except (ImportError, ValueError):
        return False


def _scan(root: Path | None) -> Iterator[tuple[Path, dict, Readiness]]:
    """Each folder that ships a manifest, with the manifest read and its
    placeholders resolved, plus why it is not ready ("" when it is).

    One scan behind every public reader, so "is this folder ready" cannot get
    two answers -- the roster's ``enabled`` and the operations view's status
    line are the same verdict rendered twice.

    A folder that fails to parse is skipped with a warning: one malformed
    manifest must not take the other products down.
    """
    found = list(_scan_folders(root))
    by_name = {str(entry.get("name") or folder.name): (entry, reason) for folder, entry, reason in found}
    for folder, entry, reason in found:
        yield folder, entry, _routes_ready(entry, by_name) if reason.ready else reason


def _routes_ready(entry: dict, by_name: dict[str, tuple[dict, Readiness]]) -> Readiness:
    """A fronting row is only as ready as the rows it routes to.

    Its roster line claims the targets' work (that claim is what sends the work
    here), and its routing entry silently drops a target that is not on the
    table; a product with nobody behind its door must not report itself ready,
    or every task for the missing half would run on the wrong implementation
    with nothing anywhere to say so.
    """
    for route in entry.get("routes") or []:
        to = str(route.get("to") or "") if isinstance(route, dict) else ""
        if not to:
            continue
        target = by_name.get(to)
        if target is None:
            return Readiness("route", f"routes to {to!r}, which is not a product here")
        target_entry, target_reason = target
        if not target_reason.ready:
            return Readiness("route", f"routes to {to!r}, which is not ready: {target_reason.detail}")
        if not bool(target_entry.get("enabled", True)):
            return Readiness("route", f"routes to {to!r}, which its own manifest switches off")
    return _READY


def _scan_folders(root: Path | None) -> Iterator[tuple[Path, dict, Readiness]]:
    """One folder at a time, judged on its own: launcher present, engine importable."""
    root = agents_root() if root is None else root
    if root is None:
        return

    python = _resolved_python()
    for manifest in sorted(root.glob("*/subagent.json")):
        folder = manifest.parent
        # pathlib's glob matches dot-directories, and a hidden folder here is
        # never a legitimate agent (every real folder name starts with a
        # letter) -- it is crash residue, most likely a scaffold staging
        # directory a SIGKILL orphaned mid-write. Advertising one puts an
        # invisible-to-ls row on the roster.
        if folder.name.startswith("."):
            continue
        try:
            entry = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(entry, dict):
                raise ValueError("manifest is not an object")
            for field in _PLACEHOLDER_FIELDS:
                if template := entry.get(field):
                    entry[field] = str(template).replace("{SUBAGENT_DIR}", str(folder)).replace("{PYTHON}", python)
        except Exception as exc:  # noqa: BLE001 - one bad folder must not sink the rest
            logger.warning("Skipping the agent product in {}: {}", folder.name, exc)
            continue

        if missing := _launcher_missing(entry):
            reason = Readiness("launcher", f"launcher file missing: {missing}")
        else:
            package, wheel = _declared_engine(entry)
            if package and not _engine_ready(package):
                reason = Readiness(
                    "engine",
                    f"the {wheel} engine wheel is not installed in this raven's environment"
                    " -- install it where raven is installed, then restart",
                )
            else:
                reason = _READY
        yield folder, entry, reason


def discover_product_rows(
    root: Path | None = None,
) -> list["ThirdPartyCliSubagentConfig | ThirdPartyAcpSubagentConfig"]:
    """Every discovered folder as a config row, name-sorted for a stable roster.

    The name is the manifest's own (``Raven-Code``, not ``raven-code``) so a
    row written by that folder's ``install.py`` collides with the discovered
    one and overrides it instead of appearing twice.

    The manifest's own ``kind`` picks the schema: ``acp`` for a folder that
    serves its agent over ACP (all five shipped products), ``cli`` for one
    spawned per task. Validating an acp manifest as cli would reject it on the
    ``kind`` literal, and the folder would silently vanish from the roster.

    ``enabled`` is the readiness verdict: a folder that cannot start is listed
    and disabled rather than dropped, so it stays visible to the operations
    view while staying out of the roster the dispatching model reads.
    """
    from raven.config.schema import ThirdPartyAcpSubagentConfig, ThirdPartyCliSubagentConfig

    rows: list[ThirdPartyCliSubagentConfig | ThirdPartyAcpSubagentConfig] = []
    for folder, entry, reason in _scan(root):
        try:
            # Both have to hold: a folder that declares itself off stays off, and
            # one that cannot start is off whatever it declares. Reading the
            # manifest's own value was missing at first -- the readiness verdict
            # simply overwrote it, so `"enabled": false` in a manifest was a field
            # the code accepted and ignored.
            declared = bool(entry.get("enabled", True))
            model = ThirdPartyAcpSubagentConfig if entry.get("kind") == "acp" else ThirdPartyCliSubagentConfig
            rows.append(model.model_validate({**entry, "enabled": declared and reason.ready}))
        except Exception as exc:  # noqa: BLE001 - one bad folder must not sink the rest
            logger.warning("Skipping the agent product in {}: {}", folder.name, exc)
    return rows


def product_state(root: Path | None = None) -> dict[str, Readiness]:
    """Row name -> its readiness verdict, from one scan.

    The whole verdict rather than one rendering of it: the operations view
    needs both whether the folder can run and what to say when it cannot, and
    answering each from its own function meant a scan of the tree per
    question, with the answers free to disagree about a folder written to
    between them.
    """
    return {str(entry.get("name") or folder.name): reason for folder, entry, reason in _scan(root)}


def product_folder(name: str, root: Path | None = None) -> Path | None:
    """The folder behind one discovered row's name, or ``None``.

    Looked up rather than derived from the name: the manifest names itself
    (``Raven-Code``) and the folder is spelled differently (``raven-code``),
    and a sixth folder is free to break any mapping between the two.
    """
    for folder, entry, _reason in _scan(root):
        if str(entry.get("name") or folder.name) == name:
            return folder
    return None


def merge_product_seeds(configs: list[Any] | None, discovered: list[Any] | None) -> list[Any]:
    """Config rows over the discovered rows, in discovery order then config order.

    The same shape as :func:`raven.agent.subagent.builtin_agents.merge_builtin_seeds`
    and for the same reason: a discovered row is a baseline, and a config row of
    the same name is the user's edit of it. Unlike a package seed the whole
    config row wins rather than merging field-by-field -- a stored row for one of
    these folders was written by its ``install.py`` from the same manifest, so it
    is a complete entry, and taking half of each would produce a command line
    neither file contains.

    Discovered rows keep discovery order and stay in place when overridden, so
    the roster does not reshuffle because one folder got a key.
    """
    by_name: dict[str, Any] = {}
    order: list[str] = []
    for row in discovered or []:
        name = getattr(row, "name", None)
        if name:
            by_name[name] = row
            order.append(name)

    for cfg in configs or []:
        name = getattr(cfg, "name", None)
        if not name:
            continue
        marked = bool(getattr(cfg, "switch_only", False))
        # `hasattr`, not a falsy read: an openai row declares no command at all,
        # and reading one as empty made every one of them look like a switch for
        # a folder -- which dropped it from the roster.
        no_launcher = hasattr(cfg, "command") and not str(getattr(cfg, "command", "") or "").strip()
        found = by_name.get(name)
        # An empty command means "switch" only for a name the scan just produced.
        # On its own it means nothing: an acp row is allowed to carry one, and
        # reading that as a switch dropped a configured agent that no folder had
        # anything to do with. The marker still stands alone, because only this
        # switch writes it.
        if no_launcher and found is None and not marked:
            # A row that names no launcher and no folder. It cannot start
            # anything, whoever wrote it and whatever its flag says, so it is
            # carried through disabled rather than dropped: deleting a row nobody
            # asked to delete is not this function's business, and advertising an
            # agent with nothing to run is not either. This is where a switch
            # stub ends up once an older rewrite has taken its marker off and the
            # folder it named has gone. A stub that still has its marker is
            # provably a switch for a folder that is not there, and drops out
            # below instead.
            if name not in by_name:
                order.append(name)
            by_name[name] = _with_enabled(cfg, False)
            continue
        if marked or (found is not None and (no_launcher or _same_but_enabled(cfg, found))):
            # A row that carries nothing but the switch. Three ways of telling,
            # because a row has to survive being rewritten by a raven that does
            # not know every field in it: the marker says so outright; an empty
            # ``command`` says so structurally, in a field every version keeps
            # and that says nothing about the folder; and being the discovered
            # entry with only its flag changed comes to the same thing, for a row
            # written before either.
            #
            # The empty command is what closes the downgrade: a bundled folder
            # travels with the raven that ships it, so a rollback and a
            # re-upgrade drift the manifest at the same time as they drop the
            # marker, and a row that copied the manifest then looked exactly like
            # somebody's override of a folder that had moved on.
            #
            # Only the flag is read, and only to take the row out: readiness and
            # the manifest still have to agree, which is what ``and`` says below.
            # A marked switch for a folder that is gone is a switch for nothing.
            if found is None:
                logger.info("Dropping the stored switch for {!r}: nothing discovered under that name", name)
                continue
            if not getattr(cfg, "enabled", True):
                by_name[name] = _with_enabled(found, False)
            continue
        stale = None
        if name in by_name and _launcher_is_gone(cfg):
            stale = "points at a launcher that no longer exists"
        elif name in by_name and _kind_changed(cfg, by_name[name]):
            stale = "is kind {!r} but its folder now declares {!r}".format(
                getattr(cfg, "kind", None), getattr(by_name[name], "kind", None)
            )
        if stale is not None:
            # A stale row decomposes the way the switch branch above splits a
            # stub: a definition the folder has outgrown, dropped, and the
            # operator's switch, kept. Every fork-era ``install.py`` wrote the
            # off flag onto the full definition row, so discarding the row whole
            # turned each of those stored "no"s into a silent re-enable. Only a
            # "no" carries over -- a stored true must not put a row the folder's
            # readiness refused back on the roster, the same asymmetry the
            # toggle writes ("on removes the row").
            keep_off = not getattr(cfg, "enabled", True)
            logger.info(
                "Stored sub-agent {!r} {}; using the discovered one{}",
                name,
                stale,
                ", kept switched off as the stored row said" if keep_off else "",
            )
            if keep_off:
                by_name[name] = _with_enabled(by_name[name], False)
            continue
        if name not in by_name:
            order.append(name)
        else:
            # The one field taken back off the discovered row when the config row
            # wins. `owns` is a manifest fact about what the agent is for, not a
            # user preference, and config rows written before the field existed
            # carry no answer at all -- without this the section it feeds would
            # stay empty on every install until each agent was reinstalled.
            # `None` is that absence; `""` is the user saying "owns nothing" and
            # is left alone.
            cfg = _fill_routing(_fill_owns(cfg, by_name[name]), by_name[name])
        by_name[name] = cfg

    return [by_name[name] for name in order]


def _same_but_enabled(cfg: Any, discovered: Any) -> bool:
    """Is ``cfg`` the discovered row with nothing but its flag changed?

    Then it carries no information beyond that flag, whoever wrote it, and may
    be read as a switch. ``enabled`` and the provenance marker are both excluded
    -- they are the two fields the switch owns.
    """
    dump = getattr(cfg, "model_dump", None)
    other = getattr(discovered, "model_dump", None)
    if dump is None or other is None:
        return False
    drop = {"enabled", "switch_only"}
    return {k: v for k, v in dump().items() if k not in drop} == {k: v for k, v in other().items() if k not in drop}


def _with_enabled(row: Any, enabled: bool) -> Any:
    """``row`` with its ``enabled`` set. Pydantic rows are copied, not mutated:
    the discovered list is built once per scan and shared with its other
    readers."""
    copy = getattr(row, "model_copy", None)
    if copy is not None:
        return copy(update={"enabled": enabled})
    return row


def _fill_owns(cfg: Any, discovered: Any) -> Any:
    """``cfg`` with ``owns`` taken from ``discovered`` when it declares none."""
    if getattr(cfg, "owns", None) is not None:
        return cfg
    found = getattr(discovered, "owns", None)
    if not found:
        return cfg
    try:
        return cfg.model_copy(update={"owns": found})
    except Exception:  # noqa: BLE001 - a duck-typed row must not sink the table
        return cfg


def _fill_routing(cfg: Any, discovered: Any) -> Any:
    """``cfg`` with ``hidden`` and ``routes`` taken from ``discovered``.

    Unconditionally, unlike ``owns``: both are facts about how the folder's
    agent is reached, a stored row has no user meaning for either, and a
    ``False`` cannot be told from "written before the field existed".
    """
    update = {
        "hidden": bool(getattr(discovered, "hidden", False)),
        "routes": list(getattr(discovered, "routes", None) or []),
    }
    try:
        return cfg.model_copy(update=update)
    except Exception:  # noqa: BLE001 - a duck-typed row must not sink the table
        return cfg


def _kind_changed(cfg: Any, discovered: Any) -> bool:
    """Whether a stored row's transport disagrees with what its folder declares now.

    The same class of staleness as :func:`_launcher_is_gone`, replaced for the
    same reason. ``kind`` is not a user preference but a structural fact about how
    the folder is launched -- an acp entry's ``command`` starts a server spawned
    once per connection, a cli entry's starts a process spawned once per task --
    so a row naming the old transport does not launch the agent a different way,
    it fails to launch it as the folder now works.

    Without this, a folder that moves to acp is invisible to every install that
    had already registered it: ``install.py`` wrote a complete cli row, that row
    wins on name, and the manifest an upgrade updated never reaches the roster.
    The alternative was asking each user to re-register by hand, which is a
    migration note nobody reads and no way to tell who is still on the old
    transport.

    A wholesale swap rather than a field merge, for the reason the caller's
    docstring gives: the two kinds carry different fields -- ``resumeCommand`` and
    ``idSource`` on one, ``cwd`` and ``readyTimeoutMs`` on the other -- so there
    is no field-by-field result that is a valid entry of either kind.

    Both sides must actually declare one: a duck-typed row that reports no kind is
    left to the config-wins path rather than replaced on a missing attribute.
    """
    stored, found = getattr(cfg, "kind", None), getattr(discovered, "kind", None)
    return bool(stored and found and stored != found)


def _launcher_is_gone(cfg: Any) -> bool:
    """Whether a stored row's command names a script that is not there.

    Only asked of a row that collides with a discovered one, and it exists
    because the two disagree about *where* after a tree moves. Each folder's
    ``install.py`` bakes an absolute path into the row it writes, so a row written
    against a tree under site-packages keeps naming that path after an upgrade has
    replaced the wheel and the tree has been installed out to the raven home. The
    stored row otherwise wins, which would turn "upgrade" into "every agent
    fails at dispatch with a file-not-found".

    Read off the command's own tokens rather than any recorded provenance: a row
    is a command line, and whether the files it names exist is the only question
    that decides if it can run. A command with no absolute path in it is left
    alone -- a hand-written row invoking something on ``PATH`` is not stale.

    ``all``, not ``any``, and that is the whole guard. Every one of these commands
    names two absolute paths, an interpreter and a launcher
    (``/.../python3 /.../raven-code/run.py ...``), and the interpreter is this
    raven's own -- so it exists whatever happened to the tree. Asking whether
    *some* named file exists therefore answered "not stale" for every row the
    manifests can produce, which is exactly the shape this exists to catch.
    """
    command = str(getattr(cfg, "command", "") or "")
    absolute = [token for token in command.split() if _is_absolute_path(token)]
    return bool(absolute) and not all(Path(token).exists() for token in absolute)
