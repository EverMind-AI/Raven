"""Agent rows discovered from the ``subagents/`` tree.

Several raven builds ship beside this one under ``subagents/`` -- separate forks at
separate versions, each with its own checkout, venv and credential -- and each
carries a ``subagent.json`` describing how to invoke it. This module turns that
tree into table rows so a raven that *has* the tree offers them without anyone
registering them by hand.

Discovery, deliberately, rather than a hard-coded list: adding a folder is then
adding a folder, which is the same reason the onboarding wizard's discovery
scans instead of naming them. And discovery only -- nothing here writes config.
A row is materialized from the manifest on every start, so a folder whose
manifest changes (a new command template after an upgrade) is picked up without a
stored copy of the old one to contradict it.

Every ordinary install has the tree: a wheel carries it at ``raven/subagents``, and
:func:`_install_packaged_tree` copies it out to the raven home on first use, which is
why the packaged branch of :func:`subagents_root` is live rather than vestigial.
Where it is absent -- a wheel built from an sdist, which has neither the tree nor a
``.git`` to enumerate it from -- there is nothing to discover and the table is exactly
what it was before this module existed. That is the intended degradation, not a gap.

**Readiness decides ``enabled``, not whether the row exists.** A folder whose venv
is unbuilt or whose credential is missing cannot start, and
:attr:`SubagentFolder.venv_ready` already records why that must not reach the
roster: "Registering an agent that cannot start puts a name in the roster the
dispatching model will pick and then fail on." Dropping the row entirely would
hide the folder from the operations view too, where "present but not set up" is
exactly what a user needs to see. So it is listed and disabled.

This module owns the three facts about where the tree is and what state a folder
is in; the onboarding wizard imports them rather than keeping its own
answers. Two readers that disagree about whether a
folder is ready would offer to install one this refuses to advertise.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from loguru import logger

if TYPE_CHECKING:
    from raven.config.schema import ThirdPartyAcpSubagentConfig, ThirdPartyCliSubagentConfig

__all__ = [
    "api_key_present",
    "api_key_var",
    "checkout_of",
    "credential_ready",
    "discover_vendored_rows",
    "installer_for",
    "Readiness",
    "vendored_folder",
    "vendored_state",
    "merge_vendored_seeds",
    "host_can_lend_a_key",
    "subagents_root",
    "venv_ready",
]

_PLACEHOLDER_FIELDS = ("command", "resumeCommand", "cwd")
"""The manifest fields carrying ``{SUBAGENT_DIR}`` / ``{PYTHON}``. Exactly the
set each folder's own ``install.py`` substitutes in its ``resolve()``, and a
field resolved here but not there would mean a hand-registered row and a
discovered one disagreeing. ``cwd`` is on the list for the acp manifests: an
acp entry with no ``cwd`` falls back to the calling task's workspace, which is
part of the pool's launch key -- so every new workspace would relaunch the
server and kill the sessions the old one was serving."""


def subagents_root() -> Path | None:
    """Where the ``subagents/`` tree is, or ``None`` when this install has none.

    Three places, checked in that order:

    - **under the raven home** (``$RAVEN_HOME`` or ``~/.raven/subagents``) -- an
      installed tree. First because it is the only writable one that survives an
      upgrade: each folder's venv lives inside its own checkout, so a tree under
      site-packages loses every venv when the wheel is replaced, and every
      agent would silently drop out of the roster after each update until
      something rebuilt them;
    - **beside the package** -- an editable install or a plain checkout leaves
      ``raven/__init__.py`` inside the clone, so the tree is two levels up;
    - **inside the package** -- a wheel built with the tree force-included lands
      it at ``raven/subagents``, the same way ``bridge`` is packaged.

    An install with none of the three has nothing to discover, and that is the
    whole gate: no flag, no setting, and a table byte-identical to what it was
    before discovery existed.
    """
    import raven
    from raven.home import raven_home

    package = Path(raven.__file__).resolve().parent
    installed, packaged = raven_home() / "subagents", package / "subagents"
    if packaged.is_dir():
        _install_packaged_tree(packaged, installed)
    for candidate in (installed, package.parent / "subagents", packaged):
        if candidate.is_dir():
            return candidate
    return None


def _install_packaged_tree(packaged: Path, installed: Path) -> None:
    """Copy a wheel's own tree out to the raven home, once per raven version.

    The reason this exists at all: each folder's venv is built *inside* its own
    checkout, and a wheel install is replaced wholesale on every upgrade -- so a
    tree left under site-packages loses every venv each time, and the agents
    drop out of the roster until something spends minutes rebuilding them.
    Copied out once, the venvs sit in a directory no upgrade touches.

    Version-stamped rather than content-compared, and the stamp is what makes
    deleting a folder work: within one raven version this is a no-op, so a folder
    the user removed stays removed. An upgrade restores what the new release
    ships, which is the one case where "the release decides" is the right answer.

    An existing folder is refreshed in place with its ``.venv`` left alone -- the
    venv is the expensive part and a new release of the same fork does not
    invalidate it. Failure is logged, never raised: not having the vendored agents
    is a smaller problem than not starting.
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
                ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc"),
                dirs_exist_ok=True,
            )
        # The tree's own files, `install.sh` above all: it builds a folder's venv,
        # it knows each folder's optional-dependency extra, and it lives at the root
        # rather than inside any folder. Copying only the folders produced a tree
        # that listed every agent and could build none -- the page offered Install
        # and the call answered "no install.sh", which reached the reader as a bare
        # "subagent not found".
        #
        # After the folders, not before: nothing here may create `installed` ahead
        # of a copy that might fail. An existing directory is what makes
        # `subagents_root` choose it, so creating it first turns a failed copy from
        # "degrade to the packaged tree" into "an empty tree and no agents at all".
        installed.mkdir(parents=True, exist_ok=True)
        for entry in sorted(packaged.iterdir()):
            if entry.is_file() and not entry.name.startswith("."):
                shutil.copy2(entry, installed / entry.name)
        stamp.write_text(__version__, encoding="utf-8")
        logger.info("Installed the packaged sub-agent tree into {}", installed)
    except Exception as exc:  # noqa: BLE001 - the agents are optional, starting is not
        logger.warning("Could not install the packaged sub-agent tree into {}: {}", installed, exc)


def checkout_of(folder: Path) -> Path | None:
    """The folder's raven checkout: its one subdirectory that is a python project.

    Discovered rather than named - the folders that ship spell it a different way
    each (``Raven-main``, ``Raven-Design``, ``Raven-Oncall``, ``Raven-X``,
    ``Raven-PPT``) and the next one is free to spell it its own.
    ``subagents/install.sh`` finds it the same way, and disagreeing with it would
    mean two answers to one question.
    """
    found = [project.parent for project in folder.glob("*/pyproject.toml")]
    return found[0] if len(found) == 1 else None


def venv_ready(checkout: Path | None) -> bool:
    """Whether the checkout's venv is built.

    Executability, not existence: ``subagents/install.sh`` classifies the same
    folder with ``[ -x ]``, and two readers of one fact that disagree on a
    present-but-unexecutable file would have the installer call a folder unbuilt
    while this advertised it.
    """
    if checkout is None:
        return False
    return os.access(checkout / ".venv" / "bin" / "raven", os.X_OK)


def api_key_var(folder_name: str) -> str:
    """``CODE_API_KEY`` for ``raven-code``: the folder name without its
    ``raven-`` prefix, upper-cased. Mirrors ``prefix_of`` in
    ``subagents/install.sh`` and the ``REQUIRED_SECRETS`` each launcher reads."""
    stem = folder_name[len("raven-") :] if folder_name.startswith("raven-") else folder_name
    return stem.upper().replace("-", "_") + "_API_KEY"


def api_key_present(folder: Path, var: str) -> bool:
    """Whether the folder holds a credential of its own.

    Read the way the launcher reads it (``run.py``'s ``env_value``): the process
    environment first, then a non-empty assignment in the folder's ``.env``. An
    assignment with an empty value counts as absent, because that is what
    ``write_key`` scaffolds from ``.env.example`` before a key is supplied -- a
    file full of bare ``NAME=`` lines is an uninstalled folder, not a configured
    one.
    """
    if os.environ.get(var, "").strip():
        return True
    env_file = folder / ".env"
    if not env_file.is_file():
        return False
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError, which OSError does not: one
        # non-UTF-8 byte in one folder's .env used to propagate out of
        # AgentRegistry.apply, and from there out of AgentLoop's constructor.
        return False
    for line in lines:
        key, _, value = line.strip().partition("=")
        if key.strip() == var and value.strip():
            return True
    return False


def host_can_lend_a_key() -> bool:
    """Whether ``inherit_llm`` in the launchers would find anything to inherit.

    A folder with no key of its own is not stranded: each launcher reads the host
    raven's ``config.json`` itself and copies its whole provider block, so the
    common case needs no credential anywhere near this tree.

    Mirrors that function's own test rather than asking ``providers.auth``, and
    the difference is the whole point. The launchers are standard-library-only
    scripts outside this package: they cannot import auth, and they accept exactly
    one shape -- a literal ``apiKey`` on some provider section. A host signed in
    through OAuth is configured by auth's rule and has nothing to lend by the
    launcher's, because those credentials live under ``~/.raven/oauth/``. Asking
    auth here would advertise an agent that dies at the first dispatch.

    The same file the launcher reads (``$RAVEN_HOME`` or ``~/.raven``), for the
    same reason: two readers of one credential that disagree would have the
    roster offer what the launcher then refuses.
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


def credential_ready(folder: Path, var: str) -> bool:
    """Whether this folder's launcher will find an LLM credential at all.

    Its own key, or the host's to inherit. Both are checked because the launcher
    checks both, in that order -- gating on the folder's own key alone would
    disable every row on a perfectly configured machine, which is the normal
    case: no folder ships a key and none needs one.
    """
    return api_key_present(folder, var) or host_can_lend_a_key()


def _resolved_python() -> str:
    """The interpreter the discovered command invokes.

    ``SUBAGENT_PYTHON`` then this interpreter, which is ``install.py``'s own
    default order minus its ``--python`` flag (there is no flag to pass here).
    Any python3 satisfies it: each launcher is standard-library only and shells
    out to its checkout's venv for the real work.
    """
    return os.environ.get("SUBAGENT_PYTHON", "").strip() or sys.executable


class Readiness(NamedTuple):
    """Why a folder cannot run, as a kind the caller can branch on plus the text.

    The text alone was not enough. Every reason reached the page as one status,
    so the page offered "Install" for all of them -- including the folder whose
    venv is already built and whose problem is a missing credential, which
    ``install.sh`` cannot produce (it scaffolds ``.env`` from ``.env.example``,
    whose bare ``NAME=`` lines are correctly read as absent). The alternative was
    matching the text in the client, which couples a button to a sentence.
    """

    kind: str
    """"" when the folder is ready; otherwise ``checkout`` / ``venv`` / ``credential``."""
    detail: str

    @property
    def ready(self) -> bool:
        return not self.kind

    @property
    def buildable(self) -> bool:
        """Whether running the tree's installer would fix this.

        Only the unbuilt venv. An ambiguous checkout defeats the installer the
        same way it defeats us, and a credential is not something it can mint.
        """
        return self.kind == "venv"


_READY = Readiness("", "")


def _scan(root: Path | None) -> Iterator[tuple[Path, dict, Readiness]]:
    """Each folder that ships both a manifest and an installer, with its manifest
    read and its placeholders resolved, plus why it is not ready ("" when it is).

    One scan behind both public readers, so "is this folder ready" cannot get two
    answers -- the roster's ``enabled`` and the operations view's status line are
    the same verdict rendered twice, and they were briefly not: the probe checks
    the command's first token, which is the interpreter and always exists, so the
    page called an unbuilt folder "installed, ready" while the roster correctly
    refused to advertise it.

    A folder that fails to parse is skipped with a warning: one malformed manifest
    must not take the other three down.
    """
    root = subagents_root() if root is None else root
    if root is None:
        return

    python = _resolved_python()
    for manifest in sorted(root.glob("*/subagent.json")):
        folder = manifest.parent
        if not (folder / "install.py").is_file():
            continue
        try:
            entry = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(entry, dict):
                raise ValueError("manifest is not an object")
            for field in _PLACEHOLDER_FIELDS:
                if template := entry.get(field):
                    entry[field] = str(template).replace("{SUBAGENT_DIR}", str(folder)).replace("{PYTHON}", python)
        except Exception as exc:  # noqa: BLE001 - one bad folder must not sink the rest
            logger.warning("Skipping vendored sub-agent in {}: {}", folder.name, exc)
            continue

        checkout = checkout_of(folder)
        if checkout is None:
            reason = Readiness("checkout", "cannot tell which subdirectory is the checkout")
        elif not venv_ready(checkout):
            reason = Readiness(
                "venv", f"venv not built in {checkout.name} -- run {folder.parent / 'install.sh'} {folder.name}"
            )
        elif not credential_ready(folder, api_key_var(folder.name)):
            reason = Readiness(
                "credential",
                f"no LLM credential: neither {api_key_var(folder.name)} nor a host provider key to inherit",
            )
        else:
            reason = _READY
        yield folder, entry, reason


def discover_vendored_rows(
    root: Path | None = None,
) -> list["ThirdPartyCliSubagentConfig | ThirdPartyAcpSubagentConfig"]:
    """Every discovered folder as a config row, name-sorted for a stable roster.

    The name is the manifest's own (``Raven-Code``, not ``raven-code``) so a row
    written by that folder's ``install.py`` collides with the discovered one and
    overrides it instead of appearing twice.

    The manifest's own ``kind`` picks the schema: ``acp`` for a folder that
    serves its agent over ACP (raven-research), ``cli`` for the rest. Validating
    an acp manifest as cli would reject it on the ``kind`` literal, and the
    folder would silently vanish from the roster.

    ``enabled`` is the readiness verdict: a folder that cannot start is listed and
    disabled rather than dropped, so it stays visible to the operations view while
    staying out of the roster the dispatching model reads.
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
            logger.warning("Skipping vendored sub-agent in {}: {}", folder.name, exc)
    return rows


def vendored_state(root: Path | None = None) -> dict[str, Readiness]:
    """Row name -> its readiness verdict, from one scan.

    The whole verdict rather than one rendering of it. The operations view needs
    three different things from it -- whether the folder can run, what to say when
    it cannot, and whether its own install button would help -- and answering each
    from its own function meant a scan of the tree per question, with the answers
    free to disagree about a folder written to between them.
    """
    return {str(entry.get("name") or folder.name): reason for folder, entry, reason in _scan(root)}


def vendored_folder(name: str, root: Path | None = None) -> Path | None:
    """The folder behind one discovered row's name, or ``None``.

    Looked up rather than derived from the name: the manifest names itself
    (``Raven-Code``) and the folder is spelled differently (``raven-code``), and
    a fifth folder is free to break any mapping between the two.
    """
    for folder, entry, _reason in _scan(root):
        if str(entry.get("name") or folder.name) == name:
            return folder
    return None


def installer_for(folder: Path) -> Path | None:
    """The tree's own ``install.sh``, which builds one folder's venv.

    Through that script rather than calling ``uv sync`` here: it knows which
    optional-dependency extra each folder needs (``ppt`` for ``raven-ppt``), and a
    second implementation of that mapping would build a venv missing exactly the
    extra the agent's job depends on. ``None`` when the tree has no installer,
    which is a tree nothing here can build.
    """
    installer = folder.parent / "install.sh"
    return installer if installer.is_file() else None


def merge_vendored_seeds(configs: list[Any] | None, vendored: list[Any] | None) -> list[Any]:
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
    for row in vendored or []:
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
        discovered = by_name.get(name)
        # An empty command means "switch" only for a name the scan just produced.
        # On its own it means nothing: an acp row is allowed to carry one, and
        # reading that as a switch dropped a configured agent that no folder had
        # anything to do with. The marker still stands alone, because only this
        # switch writes it.
        if no_launcher and discovered is None and not marked:
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
        if marked or (discovered is not None and (no_launcher or _same_but_enabled(cfg, discovered))):
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
            if discovered is None:
                logger.info("Dropping the stored switch for {!r}: nothing discovered under that name", name)
                continue
            if not getattr(cfg, "enabled", True):
                by_name[name] = _with_enabled(discovered, False)
            continue
        if name in by_name and _launcher_is_gone(cfg):
            logger.info(
                "Stored sub-agent {!r} points at a launcher that no longer exists; using the discovered one",
                name,
            )
            continue
        if name in by_name and _kind_changed(cfg, by_name[name]):
            logger.info(
                "Stored sub-agent {!r} is kind {!r} but its folder now declares {!r}; using the discovered one",
                name,
                getattr(cfg, "kind", None),
                getattr(by_name[name], "kind", None),
            )
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
            cfg = _fill_owns(cfg, by_name[name])
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
    The alternative was asking each user to re-run the tree's installer with
    ``--prune-stale``, which is a migration note nobody reads and no way to tell
    who is still on the old transport.

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
    absolute = [token for token in command.split() if token.startswith("/")]
    return bool(absolute) and not all(Path(token).exists() for token in absolute)
