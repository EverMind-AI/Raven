"""Web tool credentials cluster of the onboard wizard (Step 5).

Two keys, and they are not the same kind of thing. Serper is what makes
``web_search`` exist at all -- the agent loop withholds the tool entirely
without one -- while Jina only raises the ceiling on ``web_fetch``, which reads
pages unauthenticated at a lower rate limit either way. The screen says so,
because presenting them as two equal blanks invites skipping the one that
actually costs a capability.

Both land in ``config.json`` through ``update_tools``, and are then mirrored
into ``~/.raven/env`` as ``export`` lines. The mirror is what reaches consumers
that never read raven's config: the user's own shell, and -- the reason it is
here rather than in a docs page -- every ``cli`` and ``acp`` sub-agent, whose
environment is a capture of ``$SHELL -lic`` (see
``raven.agent.subagent.backends.env``) rather than anything raven passes down.
Which files that capture actually reads is the subtle part, and ``rc_targets_for``
carries it.
A ``kind: openai`` sub-agent is an HTTP call with no subprocess, so no
environment variable can reach it; the step does not pretend otherwise.

The mirror itself lives in ``raven.config.env_file``, because all three writers
of these keys have to refresh it for "config is the single source of truth" to
hold. This module owns the screen and the shell wiring; the rc only ever gains a
guarded ``source`` line, never a credential.

Shared wizard UI state (``console``, ``_QMARK``, ...) lives in
``onboard_commands`` and is reached through the ``oc`` module reference, as in
``onboard_channels`` -- so a test monkeypatching an attribute there still takes
effect here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer

from raven.cli import _onboard_shared as oc
from raven.config.env_file import write_env_file
from raven.i18n import t

#: Trailing comment on the line appended to the rc, and the marker that makes a
#: second ``raven onboard`` run leave the file alone.
RC_MARKER = "# raven-onboard-env"

#: The rc line itself. Guarded on existence so deleting ``~/.raven/env`` later
#: degrades to a no-op rather than an error on every new shell.
SOURCE_LINE = f'[ -f "$HOME/.raven/env" ] && . "$HOME/.raven/env"  {RC_MARKER}'

#: Which of these bash reads is decided by existence, first match winning; when
#: none exist, ``~/.profile`` is the one a login shell reads by convention.
_BASH_LOGIN_CANDIDATES = (".bash_profile", ".bash_login", ".profile")

_SERPER_SIGNUP = "https://serper.dev"
_JINA_SIGNUP = "https://jina.ai/reader"


def _stored_keys() -> tuple[str, str]:
    """The Serper and Jina keys as config holds them, unredacted."""
    from raven.config.update_tools import get_jina_api_key, get_serper_api_key

    return get_serper_api_key(redact=False), get_jina_api_key(redact=False)


def rc_targets_for(shell_name: str) -> list[Path]:
    """Every startup file this shell needs the source line in.

    bash needs two, and getting this wrong is silent. ``bash -lic`` -- what
    ``login_shell_env`` runs, and therefore what every cli/acp sub-agent's
    environment is built from -- reads the login profile chain and never opens
    ``~/.bashrc``. A line placed only in the rc reaches a sub-agent purely
    because some distros ship a ``~/.profile`` that happens to source it, and
    reaches nothing at all on a home holding a ``~/.bash_profile``, since bash
    stops at the first candidate it finds. The rc is written too, for the
    interactive non-login terminals that read only it.

    zsh needs one: ``~/.zshenv`` is read by every zsh invocation, login or not.
    """
    home = Path.home()
    if shell_name == "zsh":
        return [home / ".zshenv"]
    if shell_name != "bash":
        return []
    login = next(
        (home / name for name in _BASH_LOGIN_CANDIDATES if (home / name).exists()),
        home / ".profile",
    )
    return [login, home / ".bashrc"]


def rc_targets() -> list[Path]:
    """``rc_targets_for`` against ``$SHELL``; empty for a shell we cannot drive."""
    shell = os.environ.get("SHELL", "").strip()
    return rc_targets_for(os.path.basename(shell)) if shell else []


def ensure_rc_source_line(rc: Path) -> bool:
    """Append the source line to ``rc``. ``False`` when it was already there."""
    body = rc.read_text(encoding="utf-8") if rc.exists() else ""
    if RC_MARKER in body:
        return False

    lead = ""
    if body:
        lead = "\n" if body.endswith("\n") else "\n\n"
    with rc.open("a", encoding="utf-8") as handle:
        handle.write(f"{lead}{SOURCE_LINE}\n")
    return True


def _prompt_key(*, label: str, obtain_from: str, current: str, optional_note: str) -> Optional[str]:
    """One key prompt. ``''`` means keep what is stored; ``None`` means abort."""
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    state = t("configured, Enter keeps it") if current else t("Enter skips")
    return questionary.password(
        f"{label} ({obtain_from}) [{state}]:",
        style=RAVEN_STYLE,
        qmark=oc._QMARK,
        instruction=optional_note,
    ).ask()


def _confirm_rc(targets: list[Path]) -> bool:
    """Ask before touching the user's shell files, having shown the exact line."""
    listed = "\n".join(f"    [dim]{path}[/dim]" for path in targets)
    oc.console.print()
    oc.console.print(t("  [dim]To hand these to new shells and to cli/acp sub-agents, this line goes in:[/dim]"))
    oc.console.print(listed, highlight=False)
    oc.console.print(f"    [accent]{SOURCE_LINE}[/accent]", highlight=False)
    return typer.confirm(
        t("  Append it to {a0} file(s)?", a0=len(targets)),
        default=False,
    )


def _write_keys(serper: str, jina: str) -> Optional[Path]:
    """Persist whichever key is non-empty; return the refreshed mirror's path."""
    from raven.config.update_tools import set_jina_api_key, set_web_search

    if serper:
        set_web_search({"api_key": serper})
    if jina:
        set_jina_api_key(jina)
    return write_env_file()


def _report(path: Optional[Path]) -> None:
    if path is None:
        return
    oc.console.print(t("  [green]✓ Keys written to config and mirrored to {path} (owner-only).[/green]", path=path))
    # Both are surprising enough to be worth a line each. The capture is cached
    # per process, so a gateway already running keeps the environment it started
    # with; and a Debian ~/.bashrc returns early for non-interactive shells, so
    # a plain `bash script.sh` never reaches an appended line.
    oc.console.print(t("  [dim]A running gateway / TUI picks these up on its next restart.[/dim]"))


def _step5_web(
    *,
    skip: bool,
    non_interactive: bool,
    yes: bool = False,
    serper_api_key: Optional[str] = None,
    jina_api_key: Optional[str] = None,
) -> object:
    """Step 5 -- Serper / Jina keys, optional, forward-only.

    The flags are honoured even when the screen itself is skipped: a
    non-interactive run auto-skips, and dropping a key the caller passed
    explicitly would be the wrong reading of ``--non-interactive``.
    """
    oc._step_header(5, t("Web access"))

    if serper_api_key or jina_api_key:
        _report(_write_keys(serper_api_key or "", jina_api_key or ""))
        return None

    if skip or non_interactive:
        oc.console.print(t("  [dim]Skipping the web tool keys (set them up later: raven onboard).[/dim]"))
        return None

    oc.console.print(
        t(
            "  [dim]web_search[/dim]  Search the web — needs a Serper key; without one the\n"
            "              tool is not offered to the model at all.\n"
            "  [dim]web_fetch [/dim]  Read a page — works with no key at a lower rate limit;\n"
            "              a Jina key raises it."
        ),
        highlight=False,
    )
    oc.console.print()

    stored_serper, stored_jina = _stored_keys()
    serper = _prompt_key(
        label=t("Serper API key"),
        obtain_from=_SERPER_SIGNUP,
        current=stored_serper,
        optional_note=t(" enables web_search"),
    )
    if serper is None:
        raise typer.Exit(1)
    jina = _prompt_key(
        label=t("Jina API key"),
        obtain_from=_JINA_SIGNUP,
        current=stored_jina,
        optional_note=t(" optional"),
    )
    if jina is None:
        raise typer.Exit(1)

    path = _write_keys(serper.strip(), jina.strip())
    _report(path)

    if path is None:
        return None
    targets = rc_targets()
    if not targets:
        oc.console.print(
            t(
                "  [dim]$SHELL is not bash or zsh; add this line yourself:[/dim]\n    {SOURCE_LINE}",
                SOURCE_LINE=SOURCE_LINE,
            ),
            highlight=False,
        )
        return None
    # `--yes` is documented as skipping every confirm prompt, and skipping one
    # means answering it: a scripted run that stopped here would be blocked on a
    # prompt nobody is present to answer. The line is idempotent and guarded, so
    # answering it for the user costs a re-runnable edit, not a surprise.
    if not yes and not _confirm_rc(targets):
        return None
    touched = [target for target in targets if ensure_rc_source_line(target)]
    for target in touched:
        oc.console.print(t("  [green]✓ Added to {target}[/green]", target=target))
    if touched:
        oc.console.print(t("  [dim]New shells pick it up; sub-agents on the next raven restart.[/dim]"))
    return None


__all__ = [
    "RC_MARKER",
    "SOURCE_LINE",
    "ensure_rc_source_line",
    "rc_targets",
    "rc_targets_for",
]
