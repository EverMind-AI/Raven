"""Interactive trajectory browser — the human face of ``raven trajectory``.

A bare ``raven trajectory`` on a TTY opens a three-screen questionary flow:
session list -> attempt list -> action menu (save / report / minimize /
verdict / pin|unpin / split); with two or more attempts, the M key opens the
multi-select merge. The machine face (id-taking subcommands) lives in
:mod:`raven.cli.trajectory_commands`;
this module never shows a session key, trace id, or attempt id in menus,
labels, or action messages — only artifact paths may carry ids.

Control flow contracts:

- Every ``.ask()`` goes through :func:`_ask`; a ``None`` answer (Ctrl+C/EOF)
  raises :class:`_CancelledError`, caught only at the top level for a clean exit.
- Every prompt gets an Escape binding injected after construction by merging a
  fresh key-binding registry (text/confirm expose a read-only
  ``_MergedKeyBindings``, so ``.add`` on the existing one is not an option).
  Esc on a list screen returns ``_BACK`` and navigates one level up; at the
  top-level session screen it stays put — quitting is Ctrl+C only, so a
  reflexive Esc cannot drop the browser. Esc inside a running action goes
  through
  :func:`_ask_action`, which raises :class:`_ActionCancelledError`, caught only at
  the action boundary — the sentinel never reaches a conversion or the data
  layer. A cancelled report keeps its pre-confirm side effects (bundle + pin),
  matching the declined-report contract.
- Prompts erase themselves once answered (``erase_when_done``); navigation is
  kept legible as breadcrumb lines whose dynamic text is markup-escaped
  (titles and previews are untrusted input).
- Session and attempt lists are fixed-width table rows laid out for the
  terminal width read at screen build time (a resize re-applies on the next
  rebuild; below a table's minimum width the tightest layout is kept and
  overlong lines are clipped at the terminal edge — prompt_toolkit does not
  wrap option rows). Every dynamic cell is collapsed to one plain line before
  any width math, and fixed column widths always fit their headers.
- Injected keys dispatch by key first: Space on an attempt row prints that
  attempt's per-turn previews, collected in the same snapshot scan and sorted
  by a fully-stringified key (ordering never follows log source order; the
  table PREVIEW cell derives from the same sorted collection); M (either
  case) opens the multi-select merge and is only bound — and only advertised —
  when two or more attempts exist. Anything else, and Space on a non-attempt
  row, is a cursor-keeping no-op. The preview's follow-up waiter maps any key
  or Esc to a non-None sentinel, so only Ctrl+C keeps the browser-wide cancel
  meaning.
- Once an action runs — normally, refused, cancelled, or failing controlled —
  the browser rescans everything (``_REFRESH``): actions like report bundle
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
import shutil
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
_REFRESH = object()
_UNSET = object()
_DONE = object()

_TITLE_LIMIT = 48
_PREVIEW_LIMIT = 32
_TURN_TEXT_LIMIT = 400
_TITLE_MIN = 12
_PREVIEW_MIN = 8
_COL_GAP = "  "
_ROW_INDENT = 3
_WIDTH_MARGIN = 1
_STALE_MESSAGE = "The action was rejected or the selected attempt is no longer available; the list was refreshed."

_HELP_SESSION = (
    "Sessions with recorded trajectories, most recent first.",
    "[↑↓] move · [Enter] open · [Ctrl+C] quit",
)
_HELP_ATTEMPT = (
    "Attempts in the selected session, oldest first.",
    "[↑↓] move · [Enter] actions · [Space] preview · [Esc] back",
)
_HELP_ATTEMPT_MERGE = (
    "Attempts in the selected session, oldest first.",
    "[↑↓] move · [Enter] actions · [Space] preview · [M] merge · [Esc] back",
)
_MERGE_HINT = "[Space] toggle · [Enter] confirm · [Esc] cancel"
_HELP_ACTION = (
    "Run one action on the selected attempt.",
    "[↑↓] move · [Enter] run · [Esc] back",
)
_VERDICT_HINT = "[↑↓] move · [Enter] record · [Esc] cancel"

# Flush a lone ESC after 50ms instead of prompt_toolkit's 0.5s: the default
# disambiguation wait (ESC prefixes every escape sequence) reads as lag on a
# human keypress. Local terminals deliver sequences atomically; the worst
# case on a slow remote is a split arrow-key sequence read as ESC.
_ESC_FLUSH_TIMEOUT = 0.05

_QUESTIONARY_INSTALL_HINT = (
    "[red]The interactive browser needs the questionary package.[/red]"
    " Install it with [cyan]uv add questionary[/cyan], or use the subcommands"
    " ([cyan]raven trajectory list[/cyan], ...)."
)


class _CancelledError(Exception):
    """A prompt was cancelled (Ctrl+C / EOF); unwinds to the browser top."""


class _ActionCancelledError(Exception):
    """An action-scoped prompt was dismissed with Esc; unwinds only to the
    action boundary (:func:`_run_action` / :func:`_merge_action`)."""


@dataclass(frozen=True)
class _KeyHit:
    """An injected extra key was pressed on a list screen; ``value`` carries
    the pointed row's choice value (None when no list control is present)."""

    key: str
    value: Any


def _require_questionary() -> Any:
    """Lazy-import :mod:`questionary` so missing-package errors stay scoped here."""
    try:
        import questionary
    except ModuleNotFoundError:
        console.print(_QUESTIONARY_INSTALL_HINT)
        raise typer.Exit(1)
    return questionary


def _ask(prompt: Any) -> Any:
    # unsafe_ask + our own except: questionary's safe ask() prints its own
    # "Cancelled by user" line, which would double the browser's exit notice
    # now that Ctrl+C is the standard way out.
    ask = getattr(prompt, "unsafe_ask", None) or prompt.ask
    try:
        value = ask()
    except KeyboardInterrupt:
        value = None
    if value is None:
        raise _CancelledError()
    return value


def _find_inquirer_control(app: Any) -> Any:
    from questionary.prompts.common import InquirerControl

    for control in app.layout.find_all_controls():
        if isinstance(control, InquirerControl):
            return control
    return None


def _restyle_list_rows(control: Any) -> None:
    """Two render-time fixes questionary cannot express on its own.

    The pointed row gains a bold fragment without a foreground color, so a
    cell's own semantic color (e.g. a green check) survives — questionary only
    highlights plain-string titles, and ``class:highlighted`` would override
    the color. Separator lines (help text, table headers) move from the
    near-invisible ``separator`` class to the readable ``help`` class.
    """
    original = control.text

    def _restyled() -> list:
        tokens = []
        pointed = False
        for token in original():
            if token[0] == "[SetCursorPosition]":
                pointed = True
            elif pointed:
                token = (f"{token[0]} bold noreverse", *token[1:])
                if "\n" in token[1]:
                    pointed = False
            elif "class:separator" in token[0]:
                token = (token[0].replace("class:separator", "class:help"), *token[1:])
            tokens.append(token)
        return tokens

    control.text = _restyled


def _inject_bindings(question: Any, extra_keys: tuple[str, ...] = ()) -> None:
    """Wire Esc (and optional extra keys) into a questionary prompt.

    Bindings go through a fresh registry merged over the existing one, never
    ``.add`` on it: text/confirm prompts expose a read-only
    ``_MergedKeyBindings``. A prompt without a real application (a test fake)
    is left untouched.
    """
    app = getattr(question, "application", None)
    if app is None:
        return
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings

    control = _find_inquirer_control(app)
    if control is not None:
        _restyle_list_rows(control)

    injected = KeyBindings()

    @injected.add("escape", eager=True)
    def _escape(event: Any) -> None:
        event.app.exit(result=_BACK)

    for key in extra_keys:

        def _hit(event: Any, _key: str = key) -> None:
            pointed = control.get_pointed_at() if control is not None else None
            event.app.exit(result=_KeyHit(_key, getattr(pointed, "value", None)))

        injected.add(key)(_hit)

    app.key_bindings = merge_key_bindings([app.key_bindings, injected]) if app.key_bindings else injected
    app.erase_when_done = True
    app.ttimeoutlen = _ESC_FLUSH_TIMEOUT


def _ask_action(prompt: Any) -> Any:
    """Ask a prompt that belongs to a running action; Esc cancels the action."""
    _inject_bindings(prompt)
    value = _ask(prompt)
    if value is _BACK:
        raise _ActionCancelledError()
    return value


def _select_screen(
    questionary: Any,
    style: Any,
    message: str,
    help_lines: tuple[str, ...],
    choices: list,
    *,
    header: str | None = None,
    extra_keys: tuple[str, ...] = (),
    default: Any = None,
) -> Any:
    """One list screen: help separators under the title (then an optional
    table header line), Esc returning ``_BACK``, extra keys returning a
    :class:`_KeyHit`."""
    items: list[Any] = [questionary.Separator(line) for line in help_lines]
    if header is not None:
        items.append(questionary.Separator(header))
    items.extend(choices)
    # A single-space instruction suppresses questionary's default
    # "(Use arrow keys)" hint, which would duplicate the help separator.
    question = questionary.select(
        message, choices=items, style=style, qmark=QMARK, pointer=POINTER, default=default, instruction=" "
    )
    _inject_bindings(question, extra_keys)
    return _ask(question)


def _preview_screen(index: int, row: AttemptRow) -> None:
    """Print one attempt's conversation previews (input/output per turn).

    All text is untrusted log content: collapsed at scan time, markup-escaped
    here, auto-highlighter off (the same boundary as breadcrumbs)."""
    console.print(f"[dim]Preview ❯[/dim] #{index}", highlight=False)
    if not row.turn_previews:
        console.print("[dim](no turns recorded)[/dim]", highlight=False)
        return
    # Old or damaged logs may yield turns whose previews are all empty; the
    # emptiness check must look at displayable content, not tuple length.
    if not any(t.input or t.output for t in row.turn_previews):
        console.print("[dim](no preview recorded)[/dim]", highlight=False)
        return
    for turn in row.turn_previews:
        if turn.input:
            console.print(f"[dim]❯[/dim] {escape(turn.input)}", highlight=False)
        if turn.output:
            console.print(f"[dim]←[/dim] {escape(turn.output)}", highlight=False)


def _wait_question(questionary: Any, style: Any, **kwargs: Any) -> Any:
    """Build the post-preview waiter.

    ``press_any_key_to_continue`` maps every key — Ctrl+C included — to a
    None result, which :func:`_ask` defines as the browser-wide cancel. Its
    bindings are therefore replaced outright: any key or Esc exits with the
    non-None ``_DONE``, and only Ctrl+C keeps the cancel meaning."""
    question = questionary.press_any_key_to_continue("Press any key to go back…", style=style, **kwargs)
    app = getattr(question, "application", None)
    if app is None:
        return question
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    kb = KeyBindings()

    # Everything must be eager: the key processor still merges the default
    # registry, whose exact non-eager bindings (Up, Backspace, ...) would
    # otherwise win over a lazy wildcard and leave the waiter stuck.
    @kb.add("escape", eager=True)
    @kb.add(Keys.Any, eager=True)
    def _done(event: Any) -> None:
        event.app.exit(result=_DONE)

    @kb.add("c-c", eager=True)
    def _cancel(event: Any) -> None:
        event.app.exit(result=None)

    # The eager wildcard would also swallow the terminal's CPR reply (an
    # internal event, not a keypress) and finish the waiter on its own;
    # mirror prompt_toolkit's default CPR handling instead.
    @kb.add(Keys.CPRResponse, eager=True)
    def _cpr(event: Any) -> None:
        row, _col = map(int, event.data[2:-1].split(";"))
        event.app.renderer.report_absolute_cursor_row(row)

    app.key_bindings = kb
    app.erase_when_done = True
    app.ttimeoutlen = _ESC_FLUSH_TIMEOUT
    return question


def _preview_wait(questionary: Any, style: Any) -> None:
    _ask(_wait_question(questionary, style))


def _crumb(label: str, text: str) -> None:
    """Echo one erased screen as a breadcrumb. ``text`` is untrusted: it is
    collapsed to one plain line here (newlines/control characters would break
    the single-line record), then markup-escaped, with the auto-highlighter
    disabled."""
    line = _label_text(text, _TITLE_LIMIT) or "?"
    console.print(f"[dim]{label} ❯[/dim] {escape(line)}", highlight=False)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _collapse_text(value: Any) -> str | None:
    """One plain line from an untrusted value: non-printable characters become
    spaces and whitespace runs collapse. The single sanitization gate every
    dynamic menu/table/breadcrumb text must pass before layout math."""
    s = _str(value)
    if s is None:
        return None
    collapsed = " ".join("".join(ch if ch.isprintable() else " " for ch in s).split())
    return collapsed or None


def _label_text(value: Any, limit: int) -> str | None:
    """Normalize an untrusted value into one plain menu-safe line.

    questionary renders plain text (no Rich markup, so no escaping), but the
    value may carry newlines or control characters that would break the
    one-item-one-line layout — collapse them, then truncate."""
    collapsed = _collapse_text(value)
    if collapsed is None:
        return None
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _fmt_ts(ts: str | None) -> str:
    # Timestamps are unvalidated record strings too: normalize the slice so a
    # newline or control character cannot break the one-line menu layout.
    return _label_text((ts or "")[5:16].replace("T", " "), 16) or "?"


def _fmt_ts_full(ts: str | None) -> str:
    # Table time cells keep the year: the short menu stamp is ambiguous across
    # years, and the column header names what the value means.
    return _label_text((ts or "")[:16].replace("T", " "), 16) or "?"


def _cell_width(text: str) -> int:
    from prompt_toolkit.utils import get_cwidth

    return get_cwidth(text)


def _cell_truncate(text: str, width: int) -> str:
    """Truncate by terminal display width (CJK cells are 2 wide; ``len()``
    would misalign every following column)."""
    if _cell_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        w = _cell_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _cell_pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _cell_width(text))


def _content_budget(width: int, ncols: int) -> int:
    """Cells available for column content: the terminal width minus the
    option-row indent, a 1-cell safety margin (the qmark sits on the message
    line, not on option rows), and the inter-column gaps."""
    return width - _ROW_INDENT - _WIDTH_MARGIN - (ncols - 1) * _cell_width(_COL_GAP)


def _table_min_width(columns: list[tuple[str, int]]) -> int:
    """Terminal width needed to fit these columns; below it the layout stops
    deforming and the renderer clips overlong lines at the terminal edge
    (the declared floor behavior — option rows never wrap)."""
    return _ROW_INDENT + _WIDTH_MARGIN + sum(w for _, w in columns) + (len(columns) - 1) * _cell_width(_COL_GAP)


def _session_layout(width: int) -> list[tuple[str, int]]:
    avail = _content_budget(width, 3) - 8 - 16
    return [("TITLE", max(_TITLE_MIN, min(_TITLE_LIMIT, avail))), ("ATTEMPTS", 8), ("LAST ACTIVITY", 16)]


def _attempt_fixed(count: int) -> list[tuple[str, int]]:
    # Fixed widths are max(header, widest legal value): VERDICT is its 7-cell
    # header (statuses reach 5), so headers never need truncation.
    return [
        ("#", 1 + len(str(count))),
        ("STARTED", 16),
        ("TURNS", 5),
        ("SPANS", 5),
        ("VERDICT", 7),
        ("PIN", 3),
        ("MERGED", 6),
    ]


def _attempt_layout(width: int, count: int) -> list[tuple[str, int]]:
    fixed = _attempt_fixed(count)
    avail = _content_budget(width, len(fixed) + 1) - sum(w for _, w in fixed)
    if avail >= _PREVIEW_MIN:
        return [*fixed, ("PREVIEW", min(_PREVIEW_LIMIT, avail))]
    return fixed


def _header_line(layout: list[tuple[str, int]]) -> str:
    cells = [h if i == len(layout) - 1 else _cell_pad(h, w) for i, (h, w) in enumerate(layout)]
    return _COL_GAP.join(cells)


def _row_tokens(cells: list[tuple[str, str]], layout: list[tuple[str, int]]) -> list[tuple[str, str]]:
    """One table row as styled tokens; the last column is never padded so the
    row ends at its content."""
    tokens: list[tuple[str, str]] = []
    for i, ((style, text), (_header, w)) in enumerate(zip(cells, layout)):
        text = _cell_truncate(text, w)
        pad = "" if i == len(layout) - 1 else " " * (w - _cell_width(text)) + _COL_GAP
        if style == "class:text":
            if text + pad:
                tokens.append((style, text + pad))
        else:
            if text:
                tokens.append((style, text))
            if pad:
                tokens.append(("class:text", pad))
    return tokens


def _session_table(sessions: list[SessionRow], width: int) -> tuple[str, list[list[tuple[str, str]]]]:
    """Header line + one styled token row per session, fitted to ``width``."""
    layout = _session_layout(width)
    rows = []
    for s in sessions:
        cells = [
            ("class:text", _collapse_text(s.title) or "?"),
            ("class:text", str(len(s.attempts))),
            ("class:text", _fmt_ts_full(s.end)),
        ]
        rows.append(_row_tokens(cells, layout))
    return _header_line(layout), rows


def _attempt_table(rows: list[AttemptRow], width: int) -> tuple[str, list[list[tuple[str, str]]]]:
    """Header line + one styled token row per attempt.

    Dynamic cells pass the collapse gate before any width math: verdicts come
    from a sidecar that only guarantees a non-empty string, and ``get_cwidth``
    does not neutralize newlines or control characters."""
    layout = _attempt_layout(width, len(rows))
    out = []
    for i, r in enumerate(rows, start=1):
        cells = [
            ("class:text", f"#{i}"),
            ("class:text", _fmt_ts_full(r.start)),
            ("class:text", str(r.turns)),
            ("class:text", str(r.spans)),
            ("class:text", _collapse_text(r.verdict) or ""),
            ("class:success", "✓") if r.pinned else ("class:text", ""),
            ("class:success", "✓") if r.merged else ("class:text", ""),
        ]
        if len(layout) > len(cells):
            cells.append(("class:text", _collapse_text(r.preview) or ""))
        out.append(_row_tokens(cells, layout))
    return _header_line(layout), out


@dataclass(frozen=True, order=True)
class _TurnPreview:
    """One turn's preview texts plus its sort identity. Field order is the
    sort key: every component is an already-collapsed string (missing -> ""),
    so ordering can never raise on None and never follows log source order."""

    start: str
    span_id: str
    trace_id: str
    input: str
    output: str


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
    turn_previews: tuple[_TurnPreview, ...] = ()


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


def _fallback_title(session_key: str | None, channel: str | None = None) -> str:
    if session_key is None:
        return "(no session)"
    # The span's own channel attribute is the primary source; only a
    # well-formed "<channel>:<chat>" key yields a prefix fallback — a
    # malformed key must not surface whole (menus never show session ids).
    # No timestamp here: the table's LAST ACTIVITY column carries the time,
    # so equally-named fallbacks stay distinguishable there.
    label = _label_text(channel, 16)
    if label is None and ":" in session_key:
        label = _label_text(session_key.split(":", 1)[0], 16)
    return f"{label} session" if label else "unknown session"


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
                "turn_previews": [],
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
            g["turn_previews"].append(
                _TurnPreview(
                    start=_label_text(span.get("startTime"), _TURN_TEXT_LIMIT) or "",
                    span_id=_label_text(span.get("spanId"), _TURN_TEXT_LIMIT) or "",
                    trace_id=_label_text(span.get("traceId"), _TURN_TEXT_LIMIT) or "",
                    input=_label_text(attrs.get("turn.input_preview"), _TURN_TEXT_LIMIT) or "",
                    output=_label_text(attrs.get("turn.output_preview"), _TURN_TEXT_LIMIT) or "",
                )
            )

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
        # The table PREVIEW cell derives from the same sorted collection the
        # preview page shows — a first-seen pick would follow log source order.
        turns = tuple(sorted(g["turn_previews"]))
        first_input = next((t.input for t in turns if t.input), None)
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
            preview=_label_text(first_input, _PREVIEW_LIMIT),
            merged=aid in defs,
            turn_previews=turns,
        )
        by_session.setdefault(row.session_key, []).append(row)
        if g["channel"] and row.session_key not in channel_by_session:
            channel_by_session[row.session_key] = g["channel"]

    titles = _session_titles(workspace)
    sessions: list[SessionRow] = []
    for key, rows in by_session.items():
        rows.sort(key=lambda r: r.start or "")
        title = _label_text(titles.get(key) if key else None, _TITLE_LIMIT) or _fallback_title(
            key, channel_by_session.get(key)
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
        return bool(_ask_action(questionary.confirm(message, style=style, qmark=QMARK)))

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
            status = _ask_action(
                questionary.select(
                    "Verdict:",
                    choices=[questionary.Separator(_VERDICT_HINT), *VERDICT_STATUSES],
                    style=style,
                    qmark=QMARK,
                    pointer=POINTER,
                    instruction=" ",
                )
            )
            why = _ask_action(questionary.text("Why (optional):", style=style, qmark=QMARK))
            notes = _ask_action(questionary.text("Notes (optional):", style=style, qmark=QMARK))
            record_verdict(row.key, status, source="user", why=why or None, notes=notes or None)
            console.print(f"[green]✓[/green] Recorded verdict [cyan]{escape(status)}[/cyan]")
        elif action == "pin":
            reason = _ask_action(questionary.text("Reason (optional):", style=style, qmark=QMARK))
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
    except _ActionCancelledError:
        pass
    except (ValueError, LookupError) as exc:
        _action_error(exc)
    except typer.Exit:
        pass


def _merge_action(session_row: SessionRow, questionary: Any, style: Any) -> None:
    choices: list[Any] = [questionary.Separator(_MERGE_HINT)]
    choices.extend(
        questionary.Choice(attempt_label(i, row), value=row.key) for i, row in enumerate(session_row.attempts, start=1)
    )
    try:
        picked = _ask_action(
            questionary.checkbox(
                "Select 2+ attempts to merge:",
                choices=choices,
                style=style,
                qmark=QMARK,
                pointer=POINTER,
                instruction=" ",
                validate=lambda picked: len(picked) >= 2 or "Select at least two attempts",
            )
        )
        tstore.merge_attempts(list(picked))
        console.print(f"[green]✓[/green] Merged {len(picked)} attempts into one")
    except _ActionCancelledError:
        pass
    except (ValueError, LookupError) as exc:
        _action_error(exc)
    except typer.Exit:
        pass


def _attempt_screen(session_row: SessionRow, workspace: Path, questionary: Any, style: Any) -> object:
    """Pick an attempt and run one action. Returns _BACK or _REFRESH."""
    default: Any = None
    while True:
        width = shutil.get_terminal_size((80, 24)).columns
        header, row_tokens = _attempt_table(session_row.attempts, width)
        choices = [
            questionary.Choice(tokens, value=(i, row))
            for i, (tokens, row) in enumerate(zip(row_tokens, session_row.attempts), start=1)
        ]
        can_merge = len(session_row.attempts) >= 2
        picked = _select_screen(
            questionary,
            style,
            "Attempt:",
            _HELP_ATTEMPT_MERGE if can_merge else _HELP_ATTEMPT,
            choices,
            header=header,
            extra_keys=(" ", "m", "M") if can_merge else (" ",),
            default=default,
        )
        default = None
        if picked is _BACK:
            return _BACK
        if isinstance(picked, _KeyHit):
            # Dispatch by key, then by row: M is a screen-level action, Space
            # previews the pointed attempt row only. Anything else, and Space
            # on a non-attempt row (no pointed value), is a cursor-keeping
            # no-op — the value must never be unpacked blindly.
            if picked.key in ("m", "M"):
                _merge_action(session_row, questionary, style)
                return _REFRESH
            default = picked.value
            if picked.key == " " and isinstance(picked.value, tuple):
                index, row = picked.value
                _preview_screen(index, row)
                _preview_wait(questionary, style)
            continue
        index, row = picked
        _crumb("Attempt", f"#{index}")

        actions = [
            ("Save (bundle)", "save"),
            ("Report (redact + tarball)", "report"),
            ("Minimize (cassette)", "minimize"),
            ("Verdict", "verdict"),
            ("Unpin", "unpin") if row.pinned else ("Pin", "pin"),
        ]
        if row.merged:
            actions.append(("Split", "split"))
        action = _select_screen(
            questionary, style, "Action:", _HELP_ACTION, [questionary.Choice(t, value=v) for t, v in actions]
        )
        if action is _BACK:
            continue
        _crumb("Action", {v: t for t, v in actions}[action])
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
                width = shutil.get_terminal_size((80, 24)).columns
                header, row_tokens = _session_table(sessions, width)
                choices = [questionary.Choice(tokens, value=s) for tokens, s in zip(row_tokens, sessions)]
                picked = _BACK
                while picked is _BACK:
                    # Esc at the top level stays put: quitting is Ctrl+C
                    # only, so a reflexive Esc cannot drop the browser.
                    picked = _select_screen(questionary, RAVEN_STYLE, "Session:", _HELP_SESSION, choices, header=header)
                selected = picked
                current_key = selected.key
                _crumb("Session", selected.title)
            outcome = _attempt_screen(selected, ws, questionary, RAVEN_STYLE)
            if outcome is _BACK:
                current_key = _UNSET
    except _CancelledError:
        console.print("[dim]Cancelled.[/dim]")


__all__ = ["AttemptRow", "SessionRow", "attempt_label", "browse_trajectories", "scan_sessions", "session_label"]
