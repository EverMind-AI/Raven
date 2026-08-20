"""Onboard's sub-agent step: register the agents that ship in this checkout.

``subagents/`` holds one folder per third-party agent - its own raven checkout,
a ``config.json`` pinning the LLM it is tuned for, a ``subagent.json`` manifest,
and an ``install.py`` that turns that manifest into an entry in the host roster.
``subagents/install.sh`` builds the checkouts' venvs at install time; this is
where they enter the roster.

Registration lives here rather than in the installer because it needs a
configured host raven, and the installer runs before one exists: on a first
install ``~/.raven/config.json`` is written by this very wizard, minutes after
the installer has finished.

Nothing has to be restarted afterwards, which is the other reason this belongs
in the wizard: the startup gate runs it before ``_build_agent_loop`` reads
``config.subagents.third_party``, so a first run registers and then serves the
agents in one process.

Only a source checkout has the tree at all - the wheel and the sdist ship
``raven/`` alone - so a wheel install finds nothing and the step says so.

The choice offered per folder is not "working or not". An agent with no key of
its own still runs: its launcher copies the host's provider block whenever the
folder's own key is unset. What that costs is the model - inheritance brings the
host's `agents.defaults.model` along with its credentials, so the folder stops
running the one it was tuned for. A key of its own is the only way to keep it,
which is why the recommended model leads the menu.

All three folders are tuned for models served through OpenRouter, so a host that
already has an OpenRouter key needs no second copy of it: the step reuses that
one and asks nothing. The reuse is keyed on `providers.openrouter` specifically -
a key sitting in `custom` belongs to whatever private gateway that section points
at, and spending it against openrouter.ai would read as a bad credential rather
than as the configuration mistake it is.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple, Optional

import typer


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

        Executability, not existence: ``subagents/install.sh`` classifies the
        same folder with ``[ -x ]``, and two readers of one fact that disagree on
        a present-but-unexecutable file would have the installer call a folder
        unbuilt while this offered it.
        """
        if not self.checkout:
            return False
        launcher = self.checkout / ".venv" / "bin" / "raven"
        return os.access(launcher, os.X_OK)


def subagents_root() -> Optional[Path]:
    """The ``subagents/`` tree of the checkout this raven runs from, if any.

    An editable install leaves ``raven/__init__.py`` inside the clone, so the
    tree is two levels up. A wheel install leaves it in site-packages, where
    there is none - which is the whole gate, and needs no separate flag.
    """
    import raven

    candidate = Path(raven.__file__).resolve().parent.parent / "subagents"
    return candidate if candidate.is_dir() else None


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


def host_can_lend_a_key() -> bool:
    """Whether `inherit_llm` in the launchers would find anything to inherit.

    Mirrors that function's own test rather than asking `providers.auth`, and
    the difference is the whole point. The launchers are standard-library-only
    scripts outside this package: they cannot import auth, and they accept
    exactly one shape -- a literal `apiKey` on some provider section. A host
    signed in through OAuth is configured by auth's rule and has nothing to lend
    by the launcher's, because those credentials live in files under
    `~/.raven/oauth/`. Asking auth here would offer an option that registers
    cleanly and then dies at the first dispatch.

    So this is not a second opinion on whether a provider is set up. It is the
    question "will `inherit_llm` return non-empty", which only `inherit_llm`'s
    own rule can answer.
    """
    from raven.cli.onboard_commands import _load_raw_config

    try:
        providers = (_load_raw_config().get("providers") or {}).values()
    except Exception:
        return False
    return any(isinstance(p, dict) and p.get("apiKey") for p in providers)


def host_model() -> str:
    """The model this raven answers with, for naming the inherit option."""
    from raven.cli.onboard_commands import _load_raw_config

    try:
        return str(((_load_raw_config().get("agents") or {}).get("defaults") or {}).get("model") or "")
    except Exception:
        return ""


def _checkout_of(folder: Path) -> Optional[Path]:
    """The folder's raven checkout: its one subdirectory that is a python project.

    Discovered rather than named - the three folders spell it differently and a
    fourth is free to spell it a fourth way. ``subagents/install.sh`` finds it
    the same way, and disagreeing with it would mean two answers to one question.
    """
    found = [project.parent for project in folder.glob("*/pyproject.toml")]
    return found[0] if len(found) == 1 else None


def _env_var(folder_name: str) -> str:
    """``CODE_API_KEY`` for ``raven-code``: the folder name without its
    ``raven-`` prefix, upper-cased. Mirrors ``prefix_of`` in
    ``subagents/install.sh`` and the ``REQUIRED_SECRETS`` each launcher reads."""
    stem = folder_name[len("raven-") :] if folder_name.startswith("raven-") else folder_name
    return stem.upper().replace("-", "_") + "_API_KEY"


def discover(root: Path) -> list[SubagentFolder]:
    """Every folder shipping both a manifest and an installer, name-sorted.

    Discovery rather than a hard-coded three, so adding a folder is adding a
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


def register(folder: SubagentFolder, warnings: list[str]) -> bool:
    """Write the roster entry by running the folder's own ``install.py``.

    Through the script rather than ``update_subagents`` directly: only it knows
    how to resolve ``{SUBAGENT_DIR}`` against the folder's real location and how
    to back the previous list up. This module owns the UX layer, not the
    manifest's assembly rules.

    ``--raven-python`` is passed explicitly because the script's default is to
    read the shebang of ``raven`` on PATH, and the interpreter running this is
    already the one that has raven importable - no PATH lookup can be more
    reliable than that.
    """
    proc = subprocess.run(
        [sys.executable, "install.py", "--raven-python", sys.executable],
        cwd=folder.path,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    warnings.append(f"{folder.name}: not registered ({detail[-1] if detail else 'install.py failed'})")
    return False


def configure_subagents(*, non_interactive: bool = False, warnings: Optional[list[str]] = None) -> int:
    """Ask about each discovered folder and register the ones taken up.

    Returns how many were registered this run. Non-interactive skips the whole
    step: an unattended install should not put three agents in a roster nobody
    asked for.
    """
    warnings = warnings if warnings is not None else []
    from raven.cli._styles import RAVEN_STYLE
    from raven.cli.onboard_commands import _QMARK, _require_questionary, _t, console

    root = subagents_root()
    if root is None:
        console.print(
            _t(
                "  [dim]No sub-agent tree in this installation (source checkouts only).[/dim]",
                "  [dim]本次安装没有子代理目录(仅源码检出才有)。[/dim]",
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
    registered = 0
    for folder in folders:
        console.print(f"\n[bold]{folder.name}[/bold] [dim]{folder.description[:100]}[/dim]")
        if not folder.venv_ready:
            console.print(
                _t(
                    f"  [yellow]⚠[/yellow] Not built yet - run {root}/install.sh, then `raven onboard` again.",
                    f"  [yellow]⚠[/yellow] 尚未构建 - 先跑 {root}/install.sh,再重新运行 `raven onboard`。",
                )
            )
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
        if register(folder, warnings):
            registered += 1
            console.print(f"  [green]✓[/green] {_t('registered', '已注册')}")

    if registered:
        console.print(
            _t(
                f"\n  {registered} sub-agent(s) registered.",
                f"\n  已注册 {registered} 个子代理。",
            )
        )
    return registered


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
