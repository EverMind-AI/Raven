"""Web tool credentials cluster of the onboard wizard (Step 5).

Two tools, each behind a vendor the user picks here, and the keys are not the
same kind of thing. A search key is what makes ``web_search`` exist at all --
the agent loop withholds the tool entirely without one -- while the page
reader defaults to Jina, which works unauthenticated at a lower rate limit, so
its key only raises the ceiling; the other readers refuse without one. The
screen says so, because presenting the keys as equal blanks invites skipping
the one that actually costs a capability. The search key is then spent on
one real query before it is kept: no search vendor has a free ``/v1/models``,
and a key rejected here would otherwise surface as the tool's first failure
inside a conversation. The page reader is not probed -- Jina, the default,
needs no key, and a keyed reader whose key does not resolve is replaced by
Jina at registration rather than left to fail.

Providers and keys land in ``config.json`` through ``update_tools``, keyed by
vendor (``tools.web.providers.<vendor>.apiKey``), and the keys are then mirrored
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


def _stored_provider(kind: str) -> str:
    """The vendor config currently selects for ``search`` or ``fetch``."""
    from raven.config.update_tools import get_web_fetch, get_web_search

    return get_web_search()["provider"] if kind == "search" else get_web_fetch()["provider"]


def _stored_key(vendor: str) -> str:
    """One vendor's key as config holds it, unredacted."""
    from raven.config.update_tools import get_web_provider_key

    return get_web_provider_key(vendor, redact=False)


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


def _pick_provider(kind: str, current: str) -> Optional[str]:
    """One vendor pick. Enter keeps ``current``; ``None`` means abort."""
    from raven.agent.tools.web import FETCH_PROVIDERS, SEARCH_PROVIDERS
    from raven.cli._styles import RAVEN_STYLE

    questionary = oc._require_questionary()
    specs = SEARCH_PROVIDERS if kind == "search" else FETCH_PROVIDERS
    choices = []
    for vendor, spec in specs.items():
        note = ""
        if kind == "fetch" and not spec.needs_key:
            note = t(" (no key needed)")
        elif kind == "fetch":
            note = t(" (key required)")
        choices.append(questionary.Choice(f"{spec.label}{note}", value=vendor))
    title = t("Search provider (web_search)") if kind == "search" else t("Page reader (web_fetch)")
    return questionary.select(
        f"{title}:",
        choices=choices,
        default=next((c for c in choices if c.value == current), None),
        style=RAVEN_STYLE,
        qmark=oc._QMARK,
    ).ask()


def _probe_search(vendor: str, api_key: str) -> tuple[bool, str]:
    """One real query through ``vendor``, sent the way ``web_search`` sends it.

    The proxy is the tool's own (``tools.web.proxy``): a key probed around the
    proxy the tool will use would pass here and fail in the conversation.
    """
    import asyncio

    from raven.agent.tools.web import WebSearchTool

    proxy = ((oc._load_raw_config().get("tools") or {}).get("web") or {}).get("proxy") or None
    return asyncio.run(WebSearchTool(api_key=api_key, provider=vendor, proxy=proxy).probe())


def _verify_search_key(vendor: str, api_key: str, *, non_interactive: bool) -> bool:
    """Spend one search on the key; ``False`` means the user wants to re-enter it."""
    from raven.agent.tools.web import SEARCH_PROVIDERS

    label = t("{label} API key", label=SEARCH_PROVIDERS[vendor].label)
    while True:
        oc.console.print(t("  [dim]⏳ Verifying {label}…[/dim]", label=label))
        ok, detail = _probe_search(vendor, api_key)
        if ok:
            oc.console.print(t("  [green]✓ {label} connected.[/green]", label=label))
            return True
        oc.console.print(t("  [yellow]✗ Couldn't verify {label}: {detail}[/yellow]", label=label, detail=detail))
        choice = oc._failure_choice(
            [
                (t("Re-enter"), "rekey"),
                (t("Retry"), "retry"),
                (t("Continue anyway"), "continue"),
            ],
            non_interactive=non_interactive,
        )
        if choice == "rekey":
            return False
        if choice == "continue":
            return True


def _write(
    *,
    search_provider: Optional[str] = None,
    fetch_provider: Optional[str] = None,
    keys: Optional[dict[str, str]] = None,
) -> Optional[Path]:
    """Persist what was chosen; return the refreshed mirror's path.

    A provider is written only when one was picked, a key only when one was
    typed: an empty answer keeps whatever config already holds, so re-running
    the wizard never blanks a credential.
    """
    from raven.config.update_tools import set_web_fetch, set_web_provider_key, set_web_search

    if search_provider:
        set_web_search({"provider": search_provider})
    if fetch_provider:
        set_web_fetch({"provider": fetch_provider})
    for vendor, key in (keys or {}).items():
        if key:
            set_web_provider_key(vendor, key)
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
    search_provider: Optional[str] = None,
    fetch_provider: Optional[str] = None,
    search_api_key: Optional[str] = None,
    fetch_api_key: Optional[str] = None,
) -> object:
    """Step 5 -- pick a search vendor and a page reader, and give each its key.

    The flags are honoured even when the screen itself is skipped: a
    non-interactive run auto-skips, and dropping a value the caller passed
    explicitly would be the wrong reading of ``--non-interactive``. The two
    legacy flags name their vendor; the four newer ones name a role, and a key
    given for a role lands under whichever vendor that role selects.
    """
    from raven.agent.tools.web import FETCH_PROVIDERS, SEARCH_PROVIDERS

    oc._step_header(5, t("Web access"))

    for flag, value, table in (
        ("--search-provider", search_provider, SEARCH_PROVIDERS),
        ("--fetch-provider", fetch_provider, FETCH_PROVIDERS),
    ):
        if value and value not in table:
            raise typer.BadParameter(f"{value!r} is not a web vendor; one of: {', '.join(table)}", param_hint=flag)

    if any((serper_api_key, jina_api_key, search_provider, fetch_provider, search_api_key, fetch_api_key)):
        keys: dict[str, str] = {}
        if serper_api_key:
            keys["serper"] = serper_api_key
        if jina_api_key:
            keys["jina"] = jina_api_key
        if search_api_key:
            keys[search_provider or _stored_provider("search")] = search_api_key
        if fetch_api_key:
            keys[fetch_provider or _stored_provider("fetch")] = fetch_api_key
        _report(_write(search_provider=search_provider, fetch_provider=fetch_provider, keys=keys))
        # Written first, then probed: a flag is an explicit instruction and a
        # scripted run has nobody to re-enter the key, so a failed probe is a
        # warning on the way out, not a reason to drop what was asked for.
        vendor = search_provider or _stored_provider("search")
        if keys.get(vendor):
            _verify_search_key(vendor, keys[vendor], non_interactive=True)
        return None

    if skip or non_interactive:
        oc.console.print(t("  [dim]Skipping the web tool keys (set them up later: raven onboard).[/dim]"))
        return None

    oc.console.print(
        t(
            "  [dim]web_search[/dim]  Search the web — needs the chosen vendor's key; without\n"
            "              one the tool is not offered to the model at all.\n"
            "  [dim]web_fetch [/dim]  Read a page — Jina works with no key at a lower rate limit;\n"
            "              every other reader needs its key."
        ),
        highlight=False,
    )
    oc.console.print()

    search = _pick_provider("search", _stored_provider("search"))
    if search is None:
        raise typer.Exit(1)
    spec = SEARCH_PROVIDERS[search]
    keys = {}
    while True:
        search_key = _prompt_key(
            label=t("{label} API key", label=spec.label),
            obtain_from=spec.signup,
            current=_stored_key(search),
            optional_note=t(" enables web_search"),
        )
        if search_key is None:
            raise typer.Exit(1)
        keys[search] = search_key.strip()
        # A stored key that Enter kept is probed too: it may have been revoked
        # since it was typed. No key at all means web_search stays withheld,
        # which is a choice, not a failure.
        live = keys[search] or _stored_key(search)
        if not live or _verify_search_key(search, live, non_interactive=non_interactive):
            break

    fetch = _pick_provider("fetch", _stored_provider("fetch"))
    if fetch is None:
        raise typer.Exit(1)
    fetch_spec = FETCH_PROVIDERS[fetch]
    # One account serves both tools for the dual-use vendors, so a key just
    # typed (or already stored) for the search side is not asked for twice.
    if not (fetch == search and (keys[search] or _stored_key(search))):
        fetch_key = _prompt_key(
            label=t("{label} API key", label=fetch_spec.label),
            obtain_from=fetch_spec.signup,
            current=_stored_key(fetch),
            optional_note=t(" required for web_fetch") if fetch_spec.needs_key else t(" optional"),
        )
        if fetch_key is None:
            raise typer.Exit(1)
        keys[fetch] = fetch_key.strip()

    path = _write(search_provider=search, fetch_provider=fetch, keys=keys)
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
