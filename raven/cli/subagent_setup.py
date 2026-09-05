"""Onboard's sub-agent step: register the agents that ship in this checkout.

``subagents/`` holds one folder per vendored agent - its own raven checkout, a
``config.json`` pinning the LLM it is tuned for, a ``subagent.json`` manifest,
and an ``install.py``.

**Getting on the roster is no longer this step's job.**
:mod:`raven.agent.subagent.vendored_agents` discovers the tree and materializes a
row per folder on every table build, so an agent appears without being written
anywhere and disappears when its folder is deleted. What is left here is the part
that needs a human: building a folder's venv (minutes of downloads) and choosing
whether it runs on its own key or inherits the host's. Until the venv is built the
discovered row is listed and disabled, which is why this step offers to build it
rather than only mentioning that it is unbuilt.

The tree is not a checkout-only thing any more either: every wheel carries it,
and it is installed out to the raven home on first use so the venvs survive an
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
import subprocess
import time
from pathlib import Path
from typing import Any, NamedTuple, Optional

import typer

from raven.agent.subagent.vendored_agents import (
    api_key_var as _env_var,
)
from raven.agent.subagent.vendored_agents import (
    checkout_of as _checkout_of,
)
from raven.agent.subagent.vendored_agents import (
    host_can_lend_a_key,
    subagents_root,
    venv_ready,
)
from raven.config.loader import ConfigReadError, get_config_path, read_raw_or_raise
from raven.config.update_subagents import remove_agent


class SubagentFolder(NamedTuple):
    """One installable folder under ``subagents/``."""

    path: Path
    name: str
    description: str
    recommended_model: str
    api_base: str
    env_var: str
    checkout: Optional[Path]

    @property
    def on_openrouter(self) -> bool:
        """Whether the model this folder is tuned for is served through OpenRouter.

        Decided from the base in its own ``config.json``, not from a provider
        name: every folder spells the section ``custom``, and ``custom`` says
        nothing about which gateway answers.
        """
        return "openrouter.ai" in self.api_base

    @property
    def venv_ready(self) -> bool:
        """Whether the checkout's venv is built.

        Registering an agent that cannot start puts a name in the roster the
        dispatching model will pick and then fail on, so this gates the offer.
        Delegated to the agent layer, which decides the same fact for the rows it
        discovers -- an installer that called a folder ready while the registry
        refused to advertise it would be one question with two answers.
        """
        return venv_ready(self.checkout)


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
    """Every folder shipping both a manifest and an installer, name-sorted.

    Discovery rather than a hard-coded list, so adding a folder is adding a
    folder.
    """
    folders: list[SubagentFolder] = []
    for manifest in sorted(root.glob("*/subagent.json")):
        folder = manifest.parent
        if not (folder / "install.py").is_file():
            continue
        try:
            entry = json.loads(manifest.read_text(encoding="utf-8"))
            config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
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
                checkout=_checkout_of(folder),
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
    first (the same fail-safe as ``subagents/install.sh --prune-stale``): a
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
    from raven.cli.onboard_commands import _QMARK, _t

    console.print(
        _t(
            "  These config rows shadow a folder's manifest and would be removed:",
            "  以下配置行遮蔽了对应 folder 的 manifest,将被删除:",
        )
    )
    for row in colliding:
        flag = (
            _t(
                "  [! enabled=false - removing it would re-enable this agent]",
                "  [! enabled=false - 删除该行会让这个 agent 重新启用]",
            )
            if row.get("enabled") is False
            else ""
        )
        console.print(f"    {row.get('name')}{flag}")
    if not q.confirm(
        _t(
            f"  Remove these {len(colliding)} sub-agent row(s)? The previous list is backed up first.",
            f"  删除这 {len(colliding)} 条子代理配置行吗?会先备份之前的列表。",
        ),
        default=True,
        qmark=_QMARK,
        style=RAVEN_STYLE,
    ).ask():
        console.print(
            _t(
                "  [dim]Kept - a stored row still outranks its folder's manifest.[/dim]",
                "  [dim]已保留 - 存储行仍会盖过对应 folder 的 manifest。[/dim]",
            )
        )
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
        _t(
            f"  Backed up the previous list ({len(rows)} rows) to {backup} (private - it may hold an api key).",
            f"  已备份之前的列表({len(rows)} 条)到 {backup}(私密文件,可能含 api key)。",
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
        _t(
            f"  Removed {removed} shadowing sub-agent row(s); the folders' manifests now own the roster - restart raven to pick them up.",
            f"  已删除 {removed} 条遮蔽子代理的旧配置;现在由各 folder 的 manifest 决定 roster - 重启 raven 后生效。",
        )
    )
    return removed


def _offer_to_build(folder: SubagentFolder, root: Path, q: Any, warnings: list[str]) -> bool:
    """Offer to build this folder's venv now. True once it is built.

    Asked rather than done, and asked rather than only mentioned. Only mentioning
    it -- which is what this used to do -- leaves the agent on the table and out
    of the roster indefinitely, because the reader has to find a shell, find the
    tree, and come back; most never do, and the agents read as broken rather
    than as unbuilt. Doing it silently is the other failure: ``uv sync`` on a raven
    checkout is a minutes-long download, and a first-run wizard that stalls with
    no explanation is worse than one that asks.

    Delegated to ``subagents/install.sh`` for the single folder rather than
    calling ``uv sync`` here: that script knows which optional-dependency extra
    each folder needs, and a second implementation of that mapping would build a
    venv missing exactly the extra the agent's job depends on.
    """
    from raven.cli._styles import RAVEN_STYLE
    from raven.cli.onboard_commands import _QMARK, _t, console

    installer = root / "install.sh"
    if not installer.is_file():
        console.print(
            _t(
                f"  [yellow]⚠[/yellow] Not built yet, and {installer} is missing - cannot build it here.",
                f"  [yellow]⚠[/yellow] 尚未构建,而且找不到 {installer} - 无法在这里构建。",
            )
        )
        return False

    if not q.confirm(
        _t(
            "  Not built yet. Build it now? (a few minutes of downloads)",
            "  尚未构建。现在构建吗?(需要几分钟下载依赖)",
        ),
        default=True,
        qmark=_QMARK,
        style=RAVEN_STYLE,
    ).ask():
        console.print(
            _t(
                f"  [dim]Skipped. Run {installer} later, then `raven onboard` again.[/dim]",
                f"  [dim]已跳过。之后跑 {installer},再重新运行 `raven onboard`。[/dim]",
            )
        )
        return False

    console.print(_t("  Building...", "  正在构建..."))
    proc = subprocess.run(  # noqa: S603 - argv is built here
        ["bash", str(installer), folder.path.name],  # noqa: S607 - bash off PATH, as `register` does with the interpreter
        cwd=root,
        capture_output=True,
        text=True,
    )
    # Re-read the fact rather than trusting the exit status: the script builds and
    # scaffolds several things per folder, and "it returned 0" is not the same
    # claim as "this checkout now has a launcher raven can start".
    if venv_ready(folder.checkout):
        console.print(_t("  [green]Built.[/green]", "  [green]构建完成。[/green]"))
        return True
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    warnings.append(f"{folder.name}: build failed ({detail[-1] if detail else 'install.sh failed'})")
    console.print(
        _t(
            f"  [yellow]⚠[/yellow] Build failed; run {installer} by hand to see why.",
            f"  [yellow]⚠[/yellow] 构建失败;手动跑 {installer} 看原因。",
        )
    )
    return False


def configure_subagents(*, non_interactive: bool = False, warnings: Optional[list[str]] = None) -> int:
    """Set up each discovered folder: build its venv, choose whose LLM it runs on.

    Returns how many are ready after this run. **Registration is not part of it
    any more** -- ``vendored_agents`` materializes a row per folder on every table
    build, so the folder being there is what puts it on the table. This step used
    to end by running the folder's ``install.py`` to write a config row, and that
    row is now worse than nothing: it bakes in the folder's absolute path, it
    outranks the discovered row, and an upgrade that moves the tree turns it into
    a launcher that no longer exists.

    What is left is the part that needs a person. Both answers are things no
    default can supply: minutes of downloads, and whose credit the agent spends.

    Non-interactive skips the whole step, so an unattended install leaves every
    folder discovered-and-disabled rather than half-configured.
    """
    warnings = warnings if warnings is not None else []
    from raven.cli._styles import RAVEN_STYLE
    from raven.cli.onboard_commands import _QMARK, _require_questionary, _t, console

    root = subagents_root()
    if root is None:
        console.print(
            _t(
                "  [dim]No sub-agent tree in this installation. A release wheel carries one; "
                "reinstall from a release, or run from a source checkout.[/dim]",
                "  [dim]本次安装没有子代理目录。发布版 wheel 自带子代理;可重装发布版,或改用源码检出运行。[/dim]",
            )
        )
        return 0

    folders = discover(root)
    if not folders:
        console.print(_t("  [dim]No sub-agent folders found.[/dim]", "  [dim]没有找到子代理目录。[/dim]"))
        return 0

    if non_interactive:
        warnings.append(f"sub-agents: skipped (non-interactive; run {root}/install.sh, then raven onboard)")
        return 0

    # Offering an option the launcher cannot honour is the failure this step
    # already refuses for an unbuilt venv.
    can_inherit = host_can_lend_a_key()
    if not can_inherit:
        console.print(
            _t(
                "  [dim]This raven has no provider key to lend (an OAuth sign-in is not one),"
                " so each agent needs a key of its own.[/dim]",
                "  [dim]本机 raven 没有可借出的 provider key(OAuth 登录不算),所以每个 agent 都需要自己的 key。[/dim]",
            )
        )

    q = _require_questionary()
    _prune_shadowing_rows(folders, root, console, q, warnings)
    set_up = 0
    for folder in folders:
        console.print(f"\n[bold]{folder.name}[/bold] [dim]{folder.description[:100]}[/dim]")
        if not folder.venv_ready and not _offer_to_build(folder, root, q, warnings):
            continue

        # The recommended model goes first: it is what the folder was tuned for,
        # and it is only reachable through a key of its own -- with none set the
        # launcher inherits this raven's model too, not just its credentials.
        reuse = host_openrouter_key() if folder.on_openrouter else ""
        if reuse:
            recommended = _t(
                f"Recommended: {folder.recommended_model} via OpenRouter (reusing this raven's OpenRouter key)",
                f"推荐: {folder.recommended_model},经 OpenRouter(复用本机 raven 的 OpenRouter key)",
            )
        elif folder.on_openrouter:
            recommended = _t(
                f"Recommended: {folder.recommended_model} via OpenRouter (needs an OpenRouter key)",
                f"推荐: {folder.recommended_model},经 OpenRouter(需要一个 OpenRouter key)",
            )
        else:
            recommended = _t(
                f"Recommended: {folder.recommended_model} (needs its own key)",
                f"推荐: {folder.recommended_model}(需要它自己的 key)",
            )
        mine = host_model()
        inherit = _t(
            f"This raven's LLM{f' ({mine})' if mine else ''}",
            f"本机 raven 的 LLM{f'({mine})' if mine else ''}",
        )
        choices = [q.Choice(recommended, value="own")]
        if can_inherit:
            choices.append(q.Choice(inherit, value="inherit"))
        choices.append(q.Choice(_t("Skip", "跳过"), value="skip"))
        choice = q.select(
            _t(f"Set up {folder.name}?", f"设置 {folder.name}?"),
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
                console.print(
                    _t(
                        "  reused this raven's OpenRouter key",
                        "  已复用本机 raven 的 OpenRouter key",
                    )
                )
            elif not _take_key(folder, q):
                continue
        set_up += 1
        console.print(f"  [green]✓[/green] {_t('ready', '已就绪')}")

    if set_up:
        console.print(
            _t(
                f"\n  {set_up} sub-agent(s) ready.",
                f"\n  {set_up} 个子代理已就绪。",
            )
        )
    return set_up


def _take_key(folder: SubagentFolder, q: Any) -> bool:
    """Prompt for this folder's key, probe it, and write it. False = give up.

    A failed probe is offered as a choice rather than enforced: a base that
    blocks the metadata endpoint is not a bad key, and the user knows which of
    the two they are looking at.
    """
    from raven.cli._key_probe import probe_models
    from raven.cli._styles import RAVEN_STYLE
    from raven.cli.onboard_commands import _BACK, _QMARK, _prompt_api_key, _t, console

    while True:
        key = _prompt_api_key(
            folder.name,
            allow_back=True,
            back_label=_t("empty enter to skip this one", "留空回车跳过这个"),
        )
        if key is _BACK:
            return False
        result = probe_models(key, folder.api_base)
        if result["ok"]:
            break
        console.print(f"  [yellow]⚠[/yellow] {_t('Key validation failed', 'Key 验证失败')}: {result['status']}")
        action = q.select(
            _t("What now?", "怎么办?"),
            choices=[
                q.Choice(_t("Re-enter key", "重新输入 key"), value="retry"),
                q.Choice(_t("Save anyway", "仍然保存"), value="save"),
                *(
                    [q.Choice(_t("Use this raven's LLM instead", "改用本机 raven 的 LLM"), value="inherit")]
                    if host_can_lend_a_key()
                    else []
                ),
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
