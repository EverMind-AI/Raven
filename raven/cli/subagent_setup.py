"""Onboard's sub-agent step: set up the agent products that ship with raven.

``agents/`` holds one folder per product - a launcher (``run.py``) over the
installed raven, a ``config.json`` pinning the LLM it is tuned for, and a
``subagent.json`` manifest.

**Getting on the roster is not this step's job.**
:mod:`raven.agent.subagent.vendored_agents` discovers the tree and materializes a
row per folder on every table build, so an agent appears without being written
anywhere and disappears when its folder is deleted. What is left here is the part
that needs a human: choosing whether each product runs on its own key or inherits
the host's. A product whose readiness verdict says it cannot start (its launcher
file gone, its engine wheel not installed) has nothing this step can fix - the
reason is printed and the product skipped, because a key written for an agent
that cannot list would read as this step having broken something.

The tree is not a checkout-only thing: every wheel carries it, and it is
installed out to the raven home on first use so a product's ``.env`` survives an
upgrade. An install genuinely without it finds nothing and the step says so.

The choice offered per folder is not "working or not". An agent with no key of
its own still runs: its launcher copies the host's provider block whenever the
folder's own key is unset. What that costs is the model - inheritance brings the
host's `agents.defaults.model` along with its credentials, so the folder stops
running the one it was tuned for. A key of its own is the only way to keep it,
which is why the recommended model leads the menu.

Every folder is tuned for models served through OpenRouter, so a host that
already has an OpenRouter key needs no second copy of it: the step reuses that
one and asks nothing. The reuse is keyed on `providers.openrouter` specifically -
a key sitting in `custom` belongs to whatever private gateway that section points
at, and spending it against openrouter.ai would read as a bad credential rather
than as the configuration mistake it is.

One write remains the step's job: removing config rows that shadow a folder's
manifest (leftovers of the registration this step no longer performs). A stale
row outranks the discovered one, so pruning it is how an upgrade reaches the
roster; the prune backs the list up first and is skipped entirely in
non-interactive runs, like everything else here.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, NamedTuple, Optional

import typer

from raven.agent.subagent.vendored_agents import (
    agents_root,
    host_can_lend_a_key,
    product_state,
)
from raven.agent.subagent.vendored_agents import (
    api_key_var as _env_var,
)
from raven.config.loader import ConfigReadError, get_config_path, read_raw_or_raise
from raven.config.update_subagents import remove_agent
from raven.i18n import t


class SubagentFolder(NamedTuple):
    """One product folder under ``agents/``."""

    path: Path
    name: str
    description: str
    recommended_model: str
    api_base: str
    env_var: str

    @property
    def on_openrouter(self) -> bool:
        """Whether the model this folder is tuned for is served through OpenRouter.

        Decided from the base in its own ``config.json``, not from a provider
        name: every folder spells the section ``custom``, and ``custom`` says
        nothing about which gateway answers.
        """
        return "openrouter.ai" in self.api_base


def host_openrouter_key() -> str:
    """This raven's own OpenRouter key, or "" if it has none to lend.

    Whether the provider is set up is asked of ``_configured_providers``, which
    rules through ``providers.auth`` - a seventh opinion on what "configured"
    means is the defect ``test_only_the_auth_module_decides_configuredness_from_a_key``
    exists to prevent. Only once that verdict is in is the value read, and only
    to copy it.

    ``openrouter`` specifically, never whichever provider happens to carry a
    key: a host whose ``custom`` section points at a private gateway has a key
    that is valid there and nowhere else, and spending it against openrouter.ai
    would surface as a bad credential rather than as the mistake it is.
    """
    from raven.cli.onboard_commands import _configured_providers
    from raven.config.update_providers import get_provider_config

    try:
        if "openrouter" not in _configured_providers():
            return ""
        return str(get_provider_config("openrouter", redact_secrets=False).get("api_key") or "")
    except Exception:
        return ""


def host_model() -> str:
    """The model this raven answers with, for naming the inherit option."""
    from raven.cli.onboard_commands import _load_raw_config

    try:
        return str(((_load_raw_config().get("agents") or {}).get("defaults") or {}).get("model") or "")
    except Exception:
        return ""


def discover(root: Path) -> list[SubagentFolder]:
    """Every folder shipping a manifest, name-sorted.

    Discovery rather than a hard-coded list, so adding a product is adding a
    folder. The same marker the agent layer's scan uses -- a wizard that
    required more would set up fewer agents than the roster lists.
    """
    folders: list[SubagentFolder] = []
    for manifest in sorted(root.glob("*/subagent.json")):
        folder = manifest.parent
        try:
            entry = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict):
            # The same guard as the agent layer's scan: a manifest holding a
            # list parses fine and then breaks every field read below.
            continue
        try:
            config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # The baseline profile only refines the answer (which model actually
            # runs); a product without one is still offered on its manifest.
            config = {}
        recommended = entry.get("recommendedLlm") or {}
        defaults = (config.get("agents") or {}).get("defaults") or {}
        section = ((config.get("providers") or {}).get(defaults.get("provider") or "")) or {}
        folders.append(
            SubagentFolder(
                path=folder,
                name=entry.get("name") or folder.name,
                description=entry.get("description") or "",
                # config.json is what actually runs; the manifest only annotates.
                recommended_model=defaults.get("model") or recommended.get("model") or "",
                api_base=section.get("apiBase") or recommended.get("apiBase") or "",
                env_var=_env_var(folder.name),
            )
        )
    return folders


def write_key(folder: SubagentFolder, key: str) -> None:
    """Put ``key`` in the folder's ``.env``, the only file a launcher reads
    secrets from.

    The template is copied first when the installer has not scaffolded one. The
    first matching assignment is replaced rather than a second appended: the
    launchers take the first non-empty value, so appending would leave the file
    disagreeing with itself. Mode 600 - it holds a live credential.
    """
    env_path = folder.path / ".env"
    if not env_path.exists():
        template = folder.path / ".env.example"
        env_path.write_text(template.read_text(encoding="utf-8") if template.is_file() else "", encoding="utf-8")
    assignment = f"{folder.env_var}="
    lines = env_path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith(assignment):
            lines[index] = assignment + key
            break
    else:
        lines.append(assignment + key)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)


def _write_private_json(path: Path, data: Any) -> None:
    """Write ``data`` to ``path``, private from the first byte.

    The backup can hold an openai row's api key, so the file must never exist
    world-readable, not even for the length of a ``write_text`` + ``chmod``
    pair. ``O_CREAT`` sets the mode only when it creates, and the path can
    pre-exist (two runs in one second), so the ``fchmod`` is what makes the
    mode true either way.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)


def _prune_shadowing_rows(
    folders: list[SubagentFolder],
    root: Path,
    console: Any,
    q: Any,
    warnings: list[str],
    *,
    config_path: Path | None = None,
) -> int:
    """Delete the config rows that shadow a folder's manifest; return how many.

    A row an older ``install.py`` wrote outranks the discovered row of the
    folder it names, so a manifest a ``git pull`` updated never reaches the
    roster. The rule is the name: any configured row sharing a folder
    manifest's name is deleted, whether or not its content still matches -
    it outranks the discovered row either way. That same-name key is also how
    a user edits a vendored agent, so nothing is deleted unasked: the rows
    are listed, a disabled row is flagged (removing it would re-enable the
    agent), and a decline keeps everything. The full list is backed up
    first (the same fail-safe as the retired vendored installer's ``--prune-stale``): a
    backup that cannot be written stops the prune.
    """
    path = config_path or get_config_path()
    if not path.exists():
        return 0
    try:
        raw = read_raw_or_raise(path)
    except ConfigReadError as exc:
        warnings.append(f"sub-agents: config could not be read ({exc}); shadowing rows left alone")
        return 0
    stored = raw.get("subagents") or {}
    # All three spellings, for the same reason install.sh reads them: a backup
    # taken from the wrong one would be an empty list on a config that has
    # already migrated.
    rows = stored.get("agents") or stored.get("thirdParty") or stored.get("third_party") or []
    shadowed = {folder.name for folder in folders}
    colliding = [row for row in rows if isinstance(row, dict) and row.get("name") in shadowed]
    if not colliding:
        return 0

    from raven.cli._styles import RAVEN_STYLE
    from raven.cli.onboard_commands import _QMARK

    console.print(t("  These config rows shadow a folder's manifest and would be removed:"))
    for row in colliding:
        flag = t("  [! enabled=false - removing it would re-enable this agent]") if row.get("enabled") is False else ""
        console.print(f"    {row.get('name')}{flag}")
    if not q.confirm(
        t("  Remove these {a0} sub-agent row(s)? The previous list is backed up first.", a0=len(colliding)),
        default=True,
        qmark=_QMARK,
        style=RAVEN_STYLE,
    ).ask():
        console.print(t("  [dim]Kept - a stored row still outranks its folder's manifest.[/dim]"))
        return 0

    backup = root / f"subagents-backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    try:
        _write_private_json(backup, rows)
    except OSError as exc:
        warnings.append(
            f"sub-agents: not removing {len(colliding)} shadowing row(s): cannot write the backup at {backup} ({exc})"
        )
        return 0
    # Announced before the deletions rather than with the tally after them: a
    # removal that fails partway leaves the config half-pruned, and the one
    # thing the reader needs then is the path to the backup.
    console.print(
        t(
            "  Backed up the previous list ({a0} rows) to {backup} (private - it may hold an api key).",
            a0=len(rows),
            backup=backup,
        )
    )

    removed = 0
    for row in colliding:
        name = row["name"]
        try:
            if remove_agent(name, config_path=path):
                removed += 1
        except Exception as exc:  # noqa: BLE001 - a write that cannot land is a warning, not a wizard crash
            # remove_agent validates the surviving rows before writing, so a
            # config the schema rejects fails the whole removal with nothing
            # written -- report which row was being pruned and leave the rest.
            warnings.append(
                f"sub-agents: removing {name} failed ({exc}); the remaining shadowing rows were left in place"
            )
            return removed
    console.print(
        t(
            "  Removed {removed} shadowing sub-agent row(s); the folders' manifests now own the roster - restart raven to pick them up.",
            removed=removed,
        )
    )
    return removed


def configure_subagents(*, non_interactive: bool = False, warnings: Optional[list[str]] = None) -> int:
    """Set up each discovered product: choose whose LLM it runs on.

    Returns how many are set up after this run. **Registration is not part of
    it** -- ``vendored_agents`` materializes a row per folder on every table
    build, so the folder being there is what puts it on the table. A written
    config row would be worse than nothing: it bakes in the folder's absolute
    path, it outranks the discovered row, and an upgrade that moves the tree
    turns it into a launcher that no longer exists.

    What is left is the part that needs a person: whose credit the agent
    spends. An unready product (launcher gone, engine wheel not installed) is
    reported and skipped -- neither is something a wizard prompt can fix.

    Non-interactive skips the whole step, so an unattended install leaves every
    folder discovered-and-disabled rather than half-configured.
    """
    warnings = warnings if warnings is not None else []
    from raven.cli._styles import RAVEN_STYLE
    from raven.cli.onboard_commands import _QMARK, _require_questionary, console

    root = agents_root()
    if root is None:
        console.print(
            t(
                "  [dim]No agent products in this installation. A release wheel carries them; "
                "reinstall from a release, or run from a source checkout.[/dim]"
            )
        )
        return 0

    folders = discover(root)
    if not folders:
        console.print(t("  [dim]No agent product folders found.[/dim]"))
        return 0

    if non_interactive:
        warnings.append("sub-agents: skipped (non-interactive; run `raven onboard` to set up their keys)")
        return 0

    # Offering an option the launcher cannot honour is the failure this step
    # already refuses for an unbuilt venv.
    can_inherit = host_can_lend_a_key()
    if not can_inherit:
        console.print(
            t(
                "  [dim]This raven has no provider key to lend (an OAuth sign-in is not one),"
                " so each agent needs a key of its own.[/dim]"
            )
        )

    q = _require_questionary()
    _prune_shadowing_rows(folders, root, console, q, warnings)
    # One verdict per folder, from the same scan the roster reads: a wizard
    # that judged readiness its own way would offer to set up an agent the
    # registry then refuses to advertise.
    unready = product_state(root)
    set_up = 0
    for folder in folders:
        console.print(f"\n[bold]{folder.name}[/bold] [dim]{folder.description[:100]}[/dim]")
        verdict = unready.get(folder.name)
        if verdict is not None and not verdict.ready:
            console.print(t("  [yellow]Not ready[/yellow]: {a0}", a0=verdict.detail))
            continue

        # The recommended model goes first: it is what the folder was tuned for,
        # and it is only reachable through a key of its own -- with none set the
        # launcher inherits this raven's model too, not just its credentials.
        reuse = host_openrouter_key() if folder.on_openrouter else ""
        if reuse:
            recommended = t(
                "Recommended: {a0} via OpenRouter (reusing this raven's OpenRouter key)", a0=folder.recommended_model
            )
        elif folder.on_openrouter:
            recommended = t("Recommended: {a0} via OpenRouter (needs an OpenRouter key)", a0=folder.recommended_model)
        else:
            recommended = t("Recommended: {a0} (needs its own key)", a0=folder.recommended_model)
        mine = host_model()
        inherit = t("This raven's LLM{a0}", a0=f" ({mine})" if mine else "", a1=f"({mine})" if mine else "")
        choices = [q.Choice(recommended, value="own")]
        if can_inherit:
            choices.append(q.Choice(inherit, value="inherit"))
        choices.append(q.Choice(t("Skip"), value="skip"))
        choice = q.select(
            t("Set up {a0}?", a0=folder.name),
            choices=choices,
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if choice is None:
            raise typer.Exit(1)  # Ctrl+C
        if choice == "skip":
            continue

        if choice == "own":
            if reuse:
                write_key(folder, reuse)
                console.print(t("  reused this raven's OpenRouter key"))
            elif not _take_key(folder, q):
                continue
        set_up += 1
        console.print(f"  [green]✓[/green] {t('ready')}")

    if set_up:
        console.print(t("\n  {set_up} sub-agent(s) ready.", set_up=set_up))
    return set_up


def _take_key(folder: SubagentFolder, q: Any) -> bool:
    """Prompt for this folder's key, probe it, and write it. False = give up.

    A failed probe is offered as a choice rather than enforced: a base that
    blocks the metadata endpoint is not a bad key, and the user knows which of
    the two they are looking at.
    """
    from raven.cli._key_probe import probe_models
    from raven.cli._styles import RAVEN_STYLE
    from raven.cli.onboard_commands import _BACK, _QMARK, _prompt_api_key, console

    while True:
        key = _prompt_api_key(
            folder.name,
            allow_back=True,
            back_label=t("empty enter to skip this one"),
        )
        if key is _BACK:
            return False
        result = probe_models(key, folder.api_base)
        if result["ok"]:
            break
        console.print(f"  [yellow]⚠[/yellow] {t('Key validation failed')}: {result['status']}")
        action = q.select(
            t("What now?"),
            choices=[
                q.Choice(t("Re-enter key"), value="retry"),
                q.Choice(t("Save anyway"), value="save"),
                *([q.Choice(t("Use this raven's LLM instead"), value="inherit")] if host_can_lend_a_key() else []),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if action is None:
            raise typer.Exit(1)  # Ctrl+C
        if action == "retry":
            continue
        if action == "inherit":
            return True  # register with no key of its own; the launcher inherits
        break

    write_key(folder, key)
    return True
