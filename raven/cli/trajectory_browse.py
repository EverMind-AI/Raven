"""Interactive trajectory browser — the human face of ``raven trajectory``.

A bare ``raven trajectory`` on a TTY opens a three-screen questionary flow:
session list -> attempt list -> action menu (save / report / minimize /
verdict / pin|unpin / split, plus a multi-select merge entry). The machine
face (id-taking subcommands) lives in :mod:`raven.cli.trajectory_commands`;
this module never shows a session key, trace id, or attempt id in menus,
labels, or action messages — only artifact paths may carry ids.

Control flow contracts:

- Every ``.ask()`` goes through :func:`_ask`; a ``None`` answer (Ctrl+C/EOF)
  raises :class:`_CancelledError`, caught only at the top level for a clean exit.
- Once an action runs — normally, refused, or failing controlled — the
  browser rescans everything (``_REFRESH``): actions like report bundle
  before confirming, so even an aborted one may have pinned the attempt.
- Action errors are presented as fixed, id-free messages; the original
  exception goes to the debug log (data-layer messages embed ids).
- Aggregation works on one snapshot per refresh: definitions, pins, and
  verdicts are each read once, and log records are deduplicated into logical
  spans keyed by ``(traceId, spanId)`` (a root turn's checkpoint and final
  record share both; the last write wins).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from raven.cli import trajectory_commands as tcmd
from raven.cli._theme import POINTER, QMARK
from raven.session.manager import SessionManager
from raven.tracing import config as tracing_config
from raven.trajectory import store as tstore
from raven.trajectory.bundle import _default_workspace, collect_bundle
from raven.trajectory.cassette import minimize_bundle
from raven.trajectory.verdict import VERDICT_STATUSES, read_verdicts, record_verdict

console = Console()
_log = logging.getLogger("raven.cli.trajectory_browse")

_BACK = object()
_MERGE = object()
_REFRESH = object()
_UNSET = object()

_TITLE_LIMIT = 48
_PREVIEW_LIMIT = 32
_STALE_MESSAGE = "The action was rejected or the selected attempt is no longer available; the list was refreshed."

_QUESTIONARY_INSTALL_HINT = (
    "[red]The interactive browser needs the questionary package.[/red]"
    " Install it with [cyan]uv add questionary[/cyan], or use the subcommands"
    " ([cyan]raven trajectory list[/cyan], ...)."
)


class _CancelledError(Exception):
    """A prompt was cancelled (Ctrl+C / EOF); unwinds to the browser top."""


def _require_questionary() -> Any:
    """Lazy-import :mod:`questionary` so missing-package errors stay scoped here."""
    try:
        import questionary
    except ModuleNotFoundError:
        console.print(_QUESTIONARY_INSTALL_HINT)
        raise typer.Exit(1)
    return questionary


def _ask(prompt: Any) -> Any:
    value = prompt.ask()
    if value is None:
        raise _CancelledError()
    return value


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _label_text(value: Any, limit: int) -> str | None:
    """Normalize an untrusted value into one plain menu-safe line.

    questionary renders plain text (no Rich markup, so no escaping), but the
    value may carry newlines or control characters that would break the
    one-item-one-line layout — collapse them, then truncate."""
    s = _str(value)
    if s is None:
        return None
    collapsed = " ".join("".join(ch if ch.isprintable() else " " for ch in s).split())
    if not collapsed:
        return None
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _fmt_ts(ts: str | None) -> str:
    # Timestamps are unvalidated record strings too: normalize the slice so a
    # newline or control character cannot break the one-line menu layout.
    return _label_text((ts or "")[5:16].replace("T", " "), 16) or "?"


@dataclass
class AttemptRow:
    key: str
    traces: tuple[str, ...]
    session_key: str | None
    start: str | None
    end: str | None
    spans: int
    turns: int
    verdict: str | None
    pinned: bool
    preview: str | None
    merged: bool


@dataclass
class SessionRow:
    key: str | None
    title: str
    attempts: list[AttemptRow]
    end: str | None


def _session_titles(workspace: Path) -> dict[str, str]:
    """session key -> human title; defective entries and read failures degrade
    to an empty mapping (every session falls back to a derived label)."""
    try:
        entries = SessionManager(workspace).list_sessions()
    except Exception:
        _log.debug("session title scan failed", exc_info=True)
        return {}
    titles: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = _str(entry.get("key"))
        meta = entry.get("metadata")
        title = _str(meta.get("title")) if isinstance(meta, dict) else None
        if key and title:
            titles[key] = title
    return titles


def _fallback_title(session_key: str | None, start: str | None, channel: str | None = None) -> str:
    if session_key is None:
        return "(no session)"
    # The span's own channel attribute is the primary source; only a
    # well-formed "<channel>:<chat>" key yields a prefix fallback — a
    # malformed key must not surface whole (menus never show session ids).
    label = _label_text(channel, 16)
    if label is None and ":" in session_key:
        label = _label_text(session_key.split(":", 1)[0], 16)
    stamp = _label_text((start or "")[:16].replace("T", " "), 16)
    head = f"{label} session" if label else "unknown session"
    return f"{head} · {stamp}" if stamp else head


def scan_sessions(workspace: Path, state_dir: Path | None = None) -> list[SessionRow]:
    """One snapshot of every addressable attempt, grouped by session.

    Single-read discipline: definitions, pins, and verdicts are read exactly
    once; grouping and alias sets are built locally from that snapshot (no
    per-row re-reads). Records first collapse into logical spans keyed by
    ``(traceId, spanId)`` with last-write-wins; records missing either key
    stay separate.
    """
    state = state_dir if state_dir is not None else tracing_config.state_dir()
    defs = tstore.definitions(state)
    registry = tstore.pins(state)
    verdict_rows = read_verdicts(state)
    owner_by_trace = {t: def_id for def_id, entry in defs.items() for t in entry["traces"]}

    logical: dict[Any, dict[str, Any]] = {}
    bogus = 0
    for span in tstore.iter_spans(state):
        trace_id, span_id = _str(span.get("traceId")), _str(span.get("spanId"))
        if trace_id and span_id:
            logical[(trace_id, span_id)] = span
        else:
            logical[("?", bogus)] = span
            bogus += 1

    groups: dict[str, dict[str, Any]] = {}
    for span in logical.values():
        attrs = span.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        trace_id = _str(span.get("traceId"))
        aid = owner_by_trace.get(trace_id) or _str(attrs.get("attempt.id")) or trace_id
        if not aid:
            continue
        g = groups.setdefault(
            aid,
            {
                "traces": [],
                "session": None,
                "channel": None,
                "start": None,
                "end": None,
                "spans": 0,
                "turns": 0,
                "preview": None,
            },
        )
        g["spans"] += 1
        if trace_id and trace_id not in g["traces"]:
            g["traces"].append(trace_id)
        if g["session"] is None:
            g["session"] = _str(attrs.get("session.key"))
        if g["channel"] is None:
            g["channel"] = _str(attrs.get("channel"))
        start, end = _str(span.get("startTime")), _str(span.get("endTime"))
        if start and (g["start"] is None or start < g["start"]):
            g["start"] = start
        if end and (g["end"] is None or end > g["end"]):
            g["end"] = end
        if span.get("name") == "session.turn":
            g["turns"] += 1
            if g["preview"] is None:
                g["preview"] = _label_text(attrs.get("turn.input_preview"), _PREVIEW_LIMIT)

    latest: dict[str, tuple[int, str]] = {}
    for idx, v in enumerate(verdict_rows):
        latest[v.attempt_id] = (idx, v.status)

    def _verdict_of(aid: str) -> str | None:
        entry = defs.get(aid)
        ids = (aid, *(entry.get("aliases") or []), *entry["traces"]) if entry else (aid,)
        hits = [latest[x] for x in ids if x in latest]
        return max(hits)[1] if hits else None

    def _pinned(aid: str, traces: list[str]) -> bool:
        if aid in registry:
            return True
        entry = defs.get(aid)
        if entry and any(alias in registry for alias in entry.get("aliases") or []):
            return True
        return any(t in registry for t in traces)

    by_session: dict[str | None, list[AttemptRow]] = {}
    channel_by_session: dict[str | None, str] = {}
    for aid, g in groups.items():
        row = AttemptRow(
            key=aid,
            traces=tuple(g["traces"]),
            session_key=g["session"],
            start=g["start"],
            end=g["end"],
            spans=g["spans"],
            turns=g["turns"],
            verdict=_verdict_of(aid),
            pinned=_pinned(aid, g["traces"]),
            preview=g["preview"],
            merged=aid in defs,
        )
        by_session.setdefault(row.session_key, []).append(row)
        if g["channel"] and row.session_key not in channel_by_session:
            channel_by_session[row.session_key] = g["channel"]

    titles = _session_titles(workspace)
    sessions: list[SessionRow] = []
    for key, rows in by_session.items():
        rows.sort(key=lambda r: r.start or "")
        title = _label_text(titles.get(key) if key else None, _TITLE_LIMIT) or _fallback_title(
            key, rows[0].start, channel_by_session.get(key)
        )
        sessions.append(SessionRow(key=key, title=title, attempts=rows, end=max((r.end or "" for r in rows))))
    sessions.sort(key=lambda s: s.end or "", reverse=True)
    return sessions


def session_label(row: SessionRow) -> str:
    return f"{row.title} · {len(row.attempts)} attempt(s)"


def attempt_label(index: int, row: AttemptRow) -> str:
    parts = [f"#{index}", _fmt_ts(row.start), f"{row.turns} turn(s)", f"{row.spans} span(s)"]
    if row.verdict:
        parts.append(_label_text(row.verdict, 12) or "?")
    if row.pinned:
        parts.append("pinned")
    if row.merged:
        parts.append("merged")
    if row.preview:
        parts.append(f'"{row.preview}"')
    return " · ".join(parts)


def _action_error(exc: Exception) -> None:
    _log.debug("browser action failed", exc_info=exc)
    console.print(f"[red]{_STALE_MESSAGE}[/red]")


def _run_action(action: str, row: AttemptRow, workspace: Path, questionary: Any, style: Any) -> None:
    """Run one action against the data layer; every outcome is id-free.

    Success wording is gated on the data layer's sentinel protocol: split
    returns None and unpin returns False for already-gone state (a normal
    concurrent race, not an exception) — those must not read as success."""

    def _confirm(message: str) -> bool:
        return bool(_ask(questionary.confirm(message, style=style, qmark=QMARK)))

    try:
        if action == "save":
            with console.status("Packing the bundle...", spinner="dots"):
                bundle_dir = collect_bundle(row.key, workspace=workspace)
            console.print(f"[green]✓[/green] Bundled to [cyan]{escape(str(bundle_dir))}[/cyan]")
        elif action == "report":
            tcmd._report_attempt(
                row.key,
                out=None,
                yes=False,
                workspace=workspace,
                config=None,
                confirm=_confirm,
                on_bundled=lambda _aid: console.print(
                    "Bundled the selected attempt (re-packed to pick up the latest data)"
                ),
            )
        elif action == "minimize":
            with console.status("Packing and minimizing...", spinner="dots"):
                bundle_dir = collect_bundle(row.key, workspace=workspace)
                report = minimize_bundle(bundle_dir, tcmd._default_cassette_dir(bundle_dir.name), config_path=None)
            console.print(f"[green]✓[/green] Cassette written to [cyan]{escape(str(report.cassette_dir))}[/cyan]")
            console.print(f"  spans kept: {report.span_count}/{report.source_span_count}")
        elif action == "verdict":
            status = _ask(
                questionary.select(
                    "Verdict:", choices=list(VERDICT_STATUSES), style=style, qmark=QMARK, pointer=POINTER
                )
            )
            why = _ask(questionary.text("Why (optional):", style=style, qmark=QMARK))
            notes = _ask(questionary.text("Notes (optional):", style=style, qmark=QMARK))
            record_verdict(row.key, status, source="user", why=why or None, notes=notes or None)
            console.print(f"[green]✓[/green] Recorded verdict [cyan]{escape(status)}[/cyan]")
        elif action == "pin":
            reason = _ask(questionary.text("Reason (optional):", style=style, qmark=QMARK))
            tstore.pin_attempt(row.key, reason=reason)
            console.print("[green]✓[/green] Pinned the selected attempt")
        elif action == "unpin":
            if _confirm("Remove protection (member-level pins are cleared too)?"):
                if tstore.unpin_attempt(row.key):
                    console.print("[green]✓[/green] Unpinned the selected attempt")
                else:
                    console.print("Nothing was pinned; the list was refreshed.")
        elif action == "split":
            if _confirm(
                "Split this merged attempt (pins migrate to members, merged verdicts do not"
                " transfer, legacy members revert to their original grouping)?"
            ):
                members = tstore.split_attempt(row.key)
                if members is None:
                    console.print("The attempt is no longer merged; the list was refreshed.")
                else:
                    console.print(f"[green]✓[/green] Split into {len(members)} member trace(s)")
    except (ValueError, LookupError) as exc:
        _action_error(exc)
    except typer.Exit:
        pass


def _merge_action(session_row: SessionRow, questionary: Any, style: Any) -> None:
    choices = [
        questionary.Choice(attempt_label(i, row), value=row.key) for i, row in enumerate(session_row.attempts, start=1)
    ]
    picked = _ask(
        questionary.checkbox(
            "Select 2+ attempts to merge:",
            choices=choices,
            style=style,
            qmark=QMARK,
            pointer=POINTER,
            validate=lambda picked: len(picked) >= 2 or "Select at least two attempts",
        )
    )
    try:
        tstore.merge_attempts(list(picked))
        console.print(f"[green]✓[/green] Merged {len(picked)} attempts into one")
    except (ValueError, LookupError) as exc:
        _action_error(exc)
    except typer.Exit:
        pass


def _attempt_screen(session_row: SessionRow, workspace: Path, questionary: Any, style: Any) -> object:
    """Pick an attempt and run one action. Returns _BACK or _REFRESH."""
    while True:
        choices = [
            questionary.Choice(attempt_label(i, row), value=row) for i, row in enumerate(session_row.attempts, start=1)
        ]
        if len(session_row.attempts) >= 2:
            choices.append(questionary.Choice("Merge attempts…", value=_MERGE))
        choices.append(questionary.Choice("Back", value=_BACK))
        picked = _ask(questionary.select("Attempt:", choices=choices, style=style, qmark=QMARK, pointer=POINTER))
        if picked is _BACK:
            return _BACK
        if picked is _MERGE:
            _merge_action(session_row, questionary, style)
            return _REFRESH

        row = picked
        actions = [
            questionary.Choice("Save (bundle)", value="save"),
            questionary.Choice("Report (redact + tarball)", value="report"),
            questionary.Choice("Minimize (cassette)", value="minimize"),
            questionary.Choice("Verdict", value="verdict"),
            questionary.Choice("Unpin" if row.pinned else "Pin", value="unpin" if row.pinned else "pin"),
        ]
        if row.merged:
            actions.append(questionary.Choice("Split", value="split"))
        actions.append(questionary.Choice("Back", value=_BACK))
        action = _ask(questionary.select("Action:", choices=actions, style=style, qmark=QMARK, pointer=POINTER))
        if action is _BACK:
            continue
        _run_action(action, row, workspace, questionary, style)
        return _REFRESH


def browse_trajectories(workspace: Path | None = None) -> None:
    """Run the interactive browser until the user exits."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    ws = workspace or _default_workspace()
    current_key: Any = _UNSET
    try:
        while True:
            with console.status("Scanning trajectories...", spinner="dots"):
                sessions = scan_sessions(ws)
            if not sessions:
                console.print("[dim]No trajectories found.[/dim]")
                return
            selected = next((s for s in sessions if current_key is not _UNSET and s.key == current_key), None)
            if selected is None:
                current_key = _UNSET
                choices = [questionary.Choice(session_label(s), value=s) for s in sessions]
                choices.append(questionary.Choice("Exit", value=_BACK))
                picked = _ask(
                    questionary.select("Session:", choices=choices, style=RAVEN_STYLE, qmark=QMARK, pointer=POINTER)
                )
                if picked is _BACK:
                    return
                selected = picked
                current_key = selected.key
            outcome = _attempt_screen(selected, ws, questionary, RAVEN_STYLE)
            if outcome is _BACK:
                current_key = _UNSET
    except _CancelledError:
        console.print("[dim]Cancelled.[/dim]")


__all__ = ["AttemptRow", "SessionRow", "attempt_label", "browse_trajectories", "scan_sessions", "session_label"]
