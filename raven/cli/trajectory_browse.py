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
- Injected keys dispatch by key first: Space on an attempt row renders that
  attempt's full conversation — the causally ordered record stream rebuilt by
  :mod:`raven.trajectory.conversation` (every field sanitized, labels colored
  by kind, bodies hanging-indented). On a terminal it opens the full-screen
  viewer (:mod:`raven.cli._preview_viewer`: scrolling, h help, s collapse,
  % label filter; q/Esc returns, Ctrl+C is the browser-wide cancel), which
  needs no follow-up waiter; without a terminal every line prints directly
  and the waiter runs. A rebuild failure falls back to the scan-time per-turn
  previews; the table PREVIEW cell still derives from that sorted preview
  collection. M (either case) opens the multi-select merge and is only bound —
  and only advertised — when two or more attempts exist. Anything else, and
  Space on a non-attempt row, is a cursor-keeping no-op. The direct-print
  preview's follow-up waiter maps any key or Esc to a non-None sentinel, so
  only Ctrl+C keeps the browser-wide cancel meaning.
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
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from raven.cli import _preview_viewer as pviewer
from raven.cli import trajectory_commands as tcmd
from raven.cli._theme import POINTER, QMARK
from raven.session.manager import SessionManager
from raven.tracing import config as tracing_config
from raven.trajectory import bugreport as breport
from raven.trajectory import conversation as tconversation
from raven.trajectory import review as treview
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
_HELP_BUG_REPORTS = (
    "Bug reports filed from this attempt, newest first.",
    "[↑↓] move · [Enter] open · [Esc] back",
)
_PII_NOTE = (
    "  Note: this check looks for credentials only — names, business data, and\n  customer content are NOT anonymized."
)
_CANCELLED_MESSAGE = "Cancelled — no bug report was created."

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


_PREVIEW_BODY_MIN = 8
_PREVIEW_STACK_INDENT_MIN = 10
_PREVIEW_KIND_STYLES = {
    "user": "green",
    "llm": "cyan",
    "tool": "yellow",
    "skill": "magenta",
    "subagent": "blue",
}
_PREVIEW_DIM_LABELS = ("LLM thinking",)
_COLLAPSE_LINES = 5


def _sanitize_multiline(value: str) -> str:
    """Multi-line variant of the sanitization gate: newlines survive, every
    other non-printable character becomes a space (ANSI escapes, ``\\r``, and
    ``\\t`` — a tab has no fixed display width, which would break alignment).
    Nothing is collapsed or truncated; length is the pager's problem."""
    return "".join(ch if ch == "\n" or ch.isprintable() else " " for ch in value)


def _wrap_display(text: str, width: int) -> list[str]:
    """Split multi-line text into display lines at most ``width`` cells wide.

    CJK-aware (a wide character is never split) and word-aware: an overflowing
    line breaks after its last space when one exists — only an unbroken
    overlong run is hard-wrapped mid-word. The single space at a break point
    is consumed, like any wrapping terminal; all other whitespace survives."""
    lines: list[str] = []
    for raw in text.split("\n"):
        current: list[str] = []
        used = 0
        last_space = -1
        for ch in raw:
            cell = _cell_width(ch)
            if current and used + cell > width:
                # Break after the last space only when something precedes it;
                # a leading-space break would emit an empty line.
                if last_space > 0:
                    lines.append("".join(current[:last_space]))
                    current = current[last_space + 1 :]
                else:
                    lines.append("".join(current))
                    current = []
                used = sum(_cell_width(c) for c in current)
                last_space = max((i for i, c in enumerate(current) if c == " "), default=-1)
            if ch == " ":
                last_space = len(current)
            current.append(ch)
            used += cell
        lines.append("".join(current))
    return lines


def _fold_body(body_lines: list[str], collapse: bool) -> tuple[list[str], int]:
    """(kept lines, folded count): more than ``_COLLAPSE_LINES`` display lines
    fold to the first ``_COLLAPSE_LINES`` when collapsing is on."""
    if not collapse or len(body_lines) <= _COLLAPSE_LINES:
        return body_lines, 0
    return body_lines[:_COLLAPSE_LINES], len(body_lines) - _COLLAPSE_LINES


def _is_turn_marker(record: Any) -> bool:
    """A bare Turn marker carries a turn's identity and start time only; it
    contributes the separator but never renders a body row. A same-labeled
    record with content, a degradation, or an error renders normally."""
    return record.label == "Turn" and not record.text and not record.degraded and not record.error


def _record_lines(
    record: Any, label_col: int, width: int, show_meta: bool = True, collapse: bool = False
) -> list[Text]:
    """One record as pre-wrapped display lines (label + hanging body + notes).

    Every dynamic field is untrusted: the label and the note fields pass the
    single-line collapse gate, the body the multi-line one, and everything is
    appended as plain text — markup is never interpreted. Each returned Text
    is exactly one display line no wider than ``width``, so callers can count
    real lines. Below ``_PREVIEW_BODY_MIN`` of body room the layout stacks
    (label on its own line, body wrapped from the left edge) instead of
    clipping — narrow terminals must not lose characters.

    ``collapse`` folds a body of more than ``_COLLAPSE_LINES`` *display* lines
    (the threshold is judged after wrapping, not on source lines) down to the
    first ``_COLLAPSE_LINES`` plus a dim ellipsis line. Note lines — degraded,
    error, meta — are never folded: loss evidence must stay visible."""
    label = _collapse_text(record.label) or "?"
    style = _PREVIEW_KIND_STYLES.get(record.kind, "dim")
    dim_all = record.label in _PREVIEW_DIM_LABELS
    if dim_all:
        style = f"dim {style}"
    body_style = "dim" if dim_all else None
    body = _sanitize_multiline(record.text) if record.text else ""
    lines: list[Text] = []
    if width - label_col >= _PREVIEW_BODY_MIN:
        indent = label_col
        body_width = width - label_col
        body_lines = _wrap_display(body, body_width) if body else []
        body_lines, folded = _fold_body(body_lines, collapse)
        head = Text()
        if body_lines:
            head.append(_cell_pad(f"{label}:", label_col), style=style)
            head.append(body_lines[0], style=body_style)
            rest = body_lines[1:]
        else:
            head.append(f"{label}:", style=style)
            rest = []
        lines.append(head)
    else:
        indent = 2 if width >= _PREVIEW_STACK_INDENT_MIN else 0
        body_width = max(width - indent, 2)
        for part in _wrap_display(f"{label}:", max(width, 2)):
            lines.append(Text(part, style=style))
        rest, folded = _fold_body(_wrap_display(body, body_width) if body else [], collapse)
    pad = " " * indent
    for part in rest:
        lines.append(Text(pad + part, style=body_style))
    if folded:
        for part in _wrap_display(f"… (+{folded} more lines, s to expand)", body_width):
            lines.append(Text(pad + part, style="dim"))
    notes: list[tuple[str, str]] = []
    if record.degraded:
        notes.append((f"(! {_collapse_text(record.degraded) or '?'})", "dim yellow"))
    if record.error:
        notes.append((f"[ERROR] {_collapse_text(record.error) or '?'}", "red"))
    if record.meta and show_meta:
        meta = _collapse_text(record.meta)
        if meta:
            notes.append((f"({meta})", "dim"))
    for note, note_style in notes:
        for part in _wrap_display(note, body_width):
            lines.append(Text(pad + part, style=note_style))
    return lines


def _filter_records(records: list[Any], label_filter: str | None) -> list[Any]:
    """Keep records whose label contains the filter (case-insensitive).

    Turn markers never match by themselves: one survives only when its group
    keeps at least one visible record, so a filtered-out group produces no
    separator downstream. An empty or all-control filter keeps everything."""
    needle = (_collapse_text(label_filter) or "").lower() if label_filter else ""
    if not needle:
        return records
    matching = {id(r) for r in records if not _is_turn_marker(r) and needle in (_collapse_text(r.label) or "?").lower()}
    live_groups = {(r.trace_id, r.turn_span_id) for r in records if id(r) in matching}
    return [
        r for r in records if id(r) in matching or (_is_turn_marker(r) and (r.trace_id, r.turn_span_id) in live_groups)
    ]


def _conversation_lines(
    records: list[Any], width: int, *, collapse: bool = False, label_filter: str | None = None
) -> list[Text]:
    """The full conversation as display lines, in the data layer's order.

    A single pass over the already-sorted stream: a separator line is inserted
    wherever the ``(trace_id, turn_span_id)`` attribution changes (numbered on
    first appearance, ``(continued)`` on re-entry), records are never regrouped
    — interleaved traces render exactly as ordered.

    ``label_filter`` narrows the stream to matching labels before anything is
    laid out (the label column shrinks to the visible set); ``collapse`` folds
    long bodies per record (see :func:`_record_lines`).

    Every produced Text — separators included — is one display line within the
    real terminal width, so ``len()`` of the result is the true screen usage.
    The only floor is 2 cells: a wide character cannot be split, so a
    degenerate narrower terminal renders at 2 and may overflow."""
    width = max(width, 2)
    records = _filter_records(records, label_filter)
    shown = [r for r in records if not _is_turn_marker(r)]
    label_col = max((_cell_width((_collapse_text(r.label) or "?") + ":") for r in shown), default=2) + 1
    groups = {(r.trace_id, r.turn_span_id) for r in records}
    lines: list[Text] = []
    seen: dict[tuple[str, str | None], int] = {}
    current: Any = _UNSET
    for index, record in enumerate(records):
        key = (record.trace_id, record.turn_span_id)
        if key != current:
            current = key
            if len(groups) > 1:
                number = seen.get(key)
                if number is None:
                    number = len(seen) + 1
                    seen[key] = number
                    separator = f"── Turn {number} · {_fmt_ts_full(record.event_time)} ──"
                else:
                    separator = f"── Turn {number} (continued) ──"
                for part in _wrap_display(separator, width):
                    lines.append(Text(part, style="dim"))
        if _is_turn_marker(record):
            continue
        # One span's records repeat the same meta (an LLM call stamps its
        # model/tokens on input, thinking, and output alike); show it once,
        # on the span's last consecutive record. Span identity needs the
        # trace too: span ids are only unique within one trace, so a merged
        # attempt may interleave equal ids from different traces.
        following = records[index + 1] if index + 1 < len(records) else None
        show_meta = not (
            following is not None
            and following.trace_id == record.trace_id
            and following.span_id == record.span_id
            and following.meta == record.meta
        )
        lines.extend(_record_lines(record, label_col, width, show_meta, collapse))
    return lines


def _viewer_available() -> bool:
    """The interactive viewer needs both stdio ends on a real terminal."""
    return sys.stdout.isatty() and sys.stdin.isatty()


def _preview_screen(index: int, row: AttemptRow, state_dir: Path | None = None) -> bool:
    """Render one attempt's full conversation; returns whether the caller
    still owes the press-any-key wait (False after the interactive viewer
    ran — it contains its own interaction and exits straight to the list).

    On a terminal the preview opens the full-screen viewer (scrolling, h
    help, s collapse, % label filter); without one it degrades to printing
    every line. The record stream comes from ``attempt_conversation``; a
    rebuild failure falls back to the legacy per-turn preview so old or
    damaged stores keep a working Space key. A viewer cancelled with Ctrl+C
    raises the browser-wide :class:`_CancelledError`."""
    console.print(f"[dim]Preview ❯[/dim] #{index}", highlight=False)
    try:
        records = tconversation.attempt_conversation(row.traces, state_dir)
    except Exception:  # noqa: BLE001 — a broken store must not break browsing
        _log.debug("conversation rebuild failed; using turn previews", exc_info=True)
        _preview_screen_legacy(row)
        return True
    if not records:
        console.print("[dim](no turns recorded)[/dim]", highlight=False)
        return True
    width = shutil.get_terminal_size((80, 24)).columns
    if not _conversation_lines(records, width):
        console.print("[dim](no preview recorded)[/dim]", highlight=False)
        return True
    if _viewer_available():

        def _make_lines(collapse: bool, label_filter: str | None, view_width: int) -> list[Text]:
            return _conversation_lines(records, view_width, collapse=collapse, label_filter=label_filter)

        if pviewer.view_lines(_make_lines, title=f"Preview #{index}"):
            raise _CancelledError()
        return False
    for line in _conversation_lines(records, width):
        console.print(line, highlight=False)
    return True


def _preview_screen_legacy(row: AttemptRow) -> None:
    """The scan-time per-turn preview, kept as the fallback when the
    conversation rebuild itself fails.

    All text is untrusted log content: collapsed at scan time, markup-escaped
    here, auto-highlighter off (the same boundary as breadcrumbs)."""
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
        ("RPT", 3),
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
            ("class:success", "✓") if r.reported else ("class:text", ""),
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
    reported: bool = False
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
    # Bug report records, read once for the whole snapshot (this read also
    # performs the persisted-draft crash recovery). Matching is by the frozen
    # association: the recorded attempt id, or any member-trace overlap.
    reported_keys: set[str] = set()
    reported_traces: set[str] = set()
    for _record_dir, record in breport.list_reports(state):
        attempt = record.get("attempt") or {}
        if attempt.get("attempt_id"):
            reported_keys.add(attempt["attempt_id"])
        reported_traces.update(attempt.get("member_traces") or [])

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
            reported=aid in reported_keys or bool(reported_traces.intersection(g["traces"])),
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


def _print_stale_refresh() -> None:
    console.print("The attempt changed while the report was being prepared — nothing was created.")
    console.print("The list was refreshed; pick the attempt again.")


def _print_report_ready(record: dict[str, Any]) -> None:
    console.print(f"[green]✓[/green] Bug report {escape(record['report_id'])} ready (local_ready)", highlight=False)
    console.print(f"  Package: [cyan]{escape(record['package']['path'])}[/cyan]", highlight=False)
    console.print("  Not uploaded — hand the package file to a developer yourself.")
    console.print('  Reopen this attempt\'s actions to view it under "Bug reports".')


def _print_packaging_failure(report_id: str, reason: str, *, retryable: bool) -> None:
    console.print(f"[red]✗ Bug report {escape(report_id)} failed: {escape(reason)}[/red]", highlight=False)
    if retryable:
        console.print('  The collected snapshot is kept. Retry from this attempt\'s "Bug reports"')
        console.print("  entry — retrying will not re-collect the trajectory.")


# Reasons whose content the review screen already presents as items (the
# remaining reasons are the warning lines the screen must still show).
_REVIEW_ITEM_REASON_PREFIXES = ("residual scan flagged", "the original trajectory contained")

_REVIEW_CONTEXT_WINDOW = 200
_REVIEW_WHOLE_LINE_LIMIT = 300
_REVIEW_CONTEXT_GROUP_LIMIT = 5
_REVIEW_PATHS_NOTE = "Paths are relative to the trajectory snapshot inside the package."


def _review_warnings(reasons: list[str]) -> list[str]:
    return [reason for reason in reasons if not reason.startswith(_REVIEW_ITEM_REASON_PREFIXES)]


def _occurrence_display(occurrence: dict[str, Any]) -> tuple[str, list[tuple[int, int]]]:
    """(rendered context, highlight spans) for one occurrence.

    Short lines render whole with every hit of the value highlighted (two
    hits on one line must not lose their positions to display-text dedup);
    long lines get a window centered on this occurrence, so hits at other
    positions naturally render as distinct texts.
    """
    line, start, end = occurrence["line"], occurrence["start"], occurrence["end"]
    token = line[start:end]
    stripped = line.strip()
    if len(stripped) <= _REVIEW_WHOLE_LINE_LIMIT:
        offset = line.find(stripped) if stripped else 0
        spans = [(m.start(), m.end()) for m in re.finditer(re.escape(token), stripped)] if token else []
        return stripped, spans or [(max(0, start - offset), max(0, end - offset))]
    lo = max(0, start - _REVIEW_CONTEXT_WINDOW)
    hi = min(len(line), end + _REVIEW_CONTEXT_WINDOW)
    prefix = "..." if lo else ""
    suffix = "..." if hi < len(line) else ""
    text = prefix + line[lo:hi] + suffix
    return text, [(len(prefix) + start - lo, len(prefix) + end - lo)]


def _grouped_occurrences(item: Any) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    by_text: dict[str, dict[str, Any]] = {}
    for occurrence in item.occurrences:
        text, spans = _occurrence_display(occurrence)
        group = by_text.get(text)
        if group is None:
            group = {"text": text, "spans": [], "occurrences": []}
            by_text[text] = group
            groups.append(group)
        # Two windows on a long line can render the same text from different
        # hit positions; every occurrence's spans must survive the merge.
        for span in spans:
            if span not in group["spans"]:
                group["spans"].append(span)
        group["occurrences"].append(occurrence)
    return groups


def _print_context_text(group: dict[str, Any]) -> None:
    text = group["text"]
    merged: list[list[int]] = []
    for start, end in sorted(group["spans"]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    rendered = Text("    ")
    pos = 0
    for start, end in merged:
        rendered.append(text[pos:start])
        rendered.append(text[start:end], style="bold red")
        pos = end
    rendered.append(text[pos:])
    console.print(rendered)


def _occurrence_location(occurrence: dict[str, Any]) -> str:
    return f"{occurrence['label']} — {occurrence['file']}:{occurrence['line_no']}"


def _print_review_item(item: Any, index: int, total: int, index_by_id: dict[str, int]) -> None:
    console.print()
    title = (
        "Confirmed sensitive: private key block (already replaced)"
        if item.kind == treview.KIND_CONFIRMED
        else f"Suspected: {item.category} token"
    )
    console.print(escape(f"[{index}/{total}] {title}"), highlight=False)
    places = len(item.occurrences)
    if item.kind == treview.KIND_CONFIRMED:
        console.print(f"  Token: {escape(item.masked_sample)} — already replaced, {places} place(s)", highlight=False)
    else:
        console.print(f"  Token: {escape(item.token)} — the same value in {places} place(s)", highlight=False)
    if item.linked:
        linked = ", ".join(f"#{index_by_id[other]}" for other in item.linked)
        console.print(f"  Linked with item {linked} (overlapping values) — decisions must match.", highlight=False)
    groups = _grouped_occurrences(item)
    shown = groups[:_REVIEW_CONTEXT_GROUP_LIMIT]
    hidden = groups[_REVIEW_CONTEXT_GROUP_LIMIT:]
    for position, group in enumerate(shown, 1):
        header = f"  Context {position} of {len(groups)}" if len(groups) > 1 else "  Context"
        if len(group["occurrences"]) > 1:
            header += f" (identical in {len(group['occurrences'])} place(s))"
        console.print(header + ":", highlight=False)
        _print_context_text(group)
        console.print("  Seen at:", highlight=False)
        for occurrence in group["occurrences"]:
            console.print(f"    - {escape(_occurrence_location(occurrence))}", highlight=False)
    if hidden:
        # Context text is the only thing capped: every occurrence's semantic
        # source and file:line stay visible no matter how many groups exist.
        console.print(f"  ... {len(hidden)} more distinct context(s), locations listed below:", highlight=False)
        for group in hidden:
            for occurrence in group["occurrences"]:
                console.print(f"    - {escape(_occurrence_location(occurrence))} (context omitted)", highlight=False)


def _ask_review_item(
    item: Any, index: int, total: int, index_by_id: dict[str, int], questionary: Any, style: Any
) -> str:
    _print_review_item(item, index, total, index_by_id)
    if item.kind == treview.KIND_CONFIRMED:
        while True:
            answer = (
                _ask_action(
                    questionary.text(
                        "Decision — [Enter] acknowledge and continue / [c] cancel:", style=style, qmark=QMARK
                    )
                )
                .strip()
                .lower()
            )
            if answer == "":
                return treview.ACTION_ACKNOWLEDGED
            if answer == "c":
                raise treview.ReviewCancelledError("the report was cancelled from the review screen")
            console.print("  Press Enter to acknowledge, or c to cancel.", highlight=False)
    while True:
        answer = (
            _ask_action(
                questionary.text(
                    "Decision — [k] keep / [r] replace with [REDACTED:user-confirmed] / [c] cancel:",
                    style=style,
                    qmark=QMARK,
                )
            )
            .strip()
            .lower()
        )
        if answer == "k":
            return treview.ACTION_KEPT
        if answer == "r":
            return treview.ACTION_REDACTED
        if answer == "c":
            raise treview.ReviewCancelledError("the report was cancelled from the review screen")
        console.print("  Choose k, r, or c — this item has no default.", highlight=False)


def _conflicted_review_items(items: list[Any], actions: dict[str, str]) -> list[Any]:
    by_id = {item.id: item for item in items}
    conflicted: list[Any] = []
    seen: set[str] = set()
    for item in items:
        for other_id in item.linked:
            if actions[item.id] != actions[other_id]:
                for item_id in (item.id, other_id):
                    if item_id not in seen:
                        seen.add(item_id)
                        conflicted.append(by_id[item_id])
    return conflicted


def make_review_decider(questionary: Any, style: Any) -> Any:
    """The interactive ``decide`` callback for :func:`breport.freeze_export`.

    Validates before returning and re-asks only the conflicting linked group,
    so the pipeline never sees an inconsistent decision set (and nothing is
    applied until the set is legal). Shared by the browser and the CLI's TTY
    path.
    """

    def _decide(items: list[Any], reasons: list[str]) -> list[Any]:
        console.print()
        console.print(f"Redaction review — {len(items)} item(s) need your decision", highlight=False)
        console.print(f"  {_REVIEW_PATHS_NOTE}", highlight=False)
        for warning in _review_warnings(reasons):
            console.print(f"  [yellow]! {escape(warning)}[/yellow]", highlight=False)
        index_by_id = {item.id: index for index, item in enumerate(items, 1)}
        actions: dict[str, str] = {}
        for index, item in enumerate(items, 1):
            actions[item.id] = _ask_review_item(item, index, len(items), index_by_id, questionary, style)
        while True:
            decisions = [treview.ReviewDecision(item.id, actions[item.id]) for item in items]
            try:
                treview.validate_review_decisions(items, decisions)
                return decisions
            except treview.ReviewConflictError as exc:
                console.print(f"[red]{escape(str(exc))}[/red]", highlight=False)
                for item in _conflicted_review_items(items, actions):
                    actions[item.id] = _ask_review_item(
                        item, index_by_id[item.id], len(items), index_by_id, questionary, style
                    )

    return _decide


def _ask_problem_fields(questionary: Any, style: Any) -> dict[str, str]:
    """The required description plus the optional detail fields (design 1.2/1.3)."""
    while True:
        # Trimmed before the emptiness check: a spaces-only entry must reprompt
        # rather than land a report whose required description looks blank.
        description = _ask_action(
            questionary.text("Describe the problem (required):", style=style, qmark=QMARK)
        ).strip()
        if description:
            break
        console.print("A problem description is required to file a bug report.")
    fields = {"description": description, "expected": "", "actual": "", "severity": "", "steps": "", "reporter": ""}
    add_more = _ask_action(
        questionary.confirm(
            "Add more detail (expected, actual, severity, steps)?", default=False, style=style, qmark=QMARK
        )
    )
    if not add_more:
        return fields
    fields["expected"] = _ask_action(questionary.text("Expected result (Enter to skip):", style=style, qmark=QMARK))
    fields["actual"] = _ask_action(questionary.text("Actual result (Enter to skip):", style=style, qmark=QMARK))
    severity = _ask_action(
        questionary.select(
            "Severity:",
            choices=[questionary.Choice(label, value=label) for label in ("(skip)", *breport.SEVERITIES)],
            style=style,
            qmark=QMARK,
            pointer=POINTER,
            instruction=" ",
        )
    )
    fields["severity"] = "" if severity == "(skip)" else severity
    fields["steps"] = _ask_action(questionary.text("Steps to reproduce (Enter to skip):", style=style, qmark=QMARK))
    fields["reporter"] = _ask_action(
        questionary.text(
            "Your name or handle (Enter to skip; it will be included in the package):", style=style, qmark=QMARK
        )
    )
    return fields


def _decision_tally(decisions: list[dict[str, Any]]) -> str:
    """`N decision(s) complete — X kept, Y redacted, Z acknowledged` (hit kinds only)."""
    tally = Counter(entry.get("action") for entry in decisions)
    breakdown = ", ".join(
        f"{tally[action]} {action}" for action in ("kept", "redacted", "acknowledged") if tally[action]
    )
    return f"{len(decisions)} decision(s) complete — {breakdown}"


def _decision_line(entry: dict[str, Any]) -> str:
    """One decision row: action, short masked token, first source (+N more)."""
    token_label = entry.get("masked_token") or entry.get("masked_sample") or ""
    sources = entry.get("sources") or []
    first = sources[0]["source"] if sources else ""
    more = f" (+{len(sources) - 1} more sources)" if len(sources) > 1 else ""
    return _collapse_text(f"{entry.get('action', ''):<12} {token_label} — {first}{more}") or ""


def _summary_line(label: str, value: str) -> None:
    console.print(f"  {label + ':':<14}{value}", highlight=False)


def _print_bug_summary(session_row: SessionRow, index: int, row: AttemptRow, prep: Any) -> None:
    """The final confirmation block, rendered from the frozen canonical
    metadata (the text the user approves is the text that ships)."""
    meta = prep.package_metadata
    manifest = prep.manifest
    console.print("Bug report summary", highlight=False)
    title = _collapse_text(session_row.title) or "?"
    _summary_line("Session", f"{escape(title)}          last activity {_fmt_ts_full(session_row.end)}")
    _summary_line("Attempt", f"#{index} · {row.turns} turn(s) · started {_fmt_ts_full(row.start)}")
    _summary_line("Problem", escape(_collapse_text(meta["problem"]["description"]) or ""))
    for label, key in (("Expected", "expected"), ("Actual", "actual"), ("Severity", "severity"), ("Steps", "steps")):
        if meta["problem"].get(key):
            _summary_line(label, escape(_collapse_text(meta["problem"][key]) or ""))
    if meta.get("reporter"):
        _summary_line("Reporter", f"{escape(_collapse_text(meta['reporter']) or '')} (included in the package)")
    session_note = "included" if manifest.get("session_included") else "missing"
    missing = len(manifest.get("missing_artifacts") or [])
    _summary_line(
        "Trajectory",
        f"{manifest.get('span_count')} span(s) · session record {session_note}"
        f" · {manifest.get('artifact_count')} artifact(s), {missing} missing",
    )
    completeness = meta["completeness"]
    _summary_line("Completeness", escape(completeness["status"]))
    for reason in completeness["reasons"]:
        console.print(f"    - {escape(reason)}", highlight=False)
    redaction = meta["redaction"]
    exact = sum(redaction["exact_replacements"].values())
    patterns = sum(redaction["pattern_replacements"].values())
    counts = f"{exact} known-value + {patterns} pattern replacement(s)"
    if prep.classification == breport.CLASSIFICATION_NEEDS_REVIEW:
        _summary_line("Redaction", f"{counts} · NEEDS REVIEW")
        for notice in redaction.get("security_notices") or []:
            console.print(f"    [yellow]! {escape(notice)}[/yellow]", highlight=False)
        for reason in prep.reasons:
            console.print(f"    - {escape(reason)}", highlight=False)
        decisions = redaction.get("user_decisions") or []
        if decisions:
            _summary_line("Reviewed", _decision_tally(decisions))
            for entry in decisions:
                console.print(f"    - {escape(_decision_line(entry))}", highlight=False)
    else:
        _summary_line("Redaction", f"{counts} · residual scan: clean")
    console.print(_PII_NOTE, highlight=False)
    _summary_line("Will produce", f"bug report package {prep.report_id}.tar.gz (kept locally)")
    console.print("  Nothing will be uploaded — the package stays on this machine.")


def _confirm_create(questionary: Any, style: Any, prep: Any) -> bool:
    """One confirmation: the review variant carries the risk authorization.

    The per-item adjudication already happened during the freeze, so a review
    report needs exactly one risk-worded consent (listing was printed by the
    summary) instead of the old create-then-review double prompt.
    """
    if prep.classification == breport.CLASSIFICATION_NEEDS_REVIEW:
        if prep.user_decisions:
            console.print("Review decisions are complete. Confirm the report contents shown above.", highlight=False)
        return bool(
            _ask_action(
                questionary.confirm(
                    "Ship the package with the risks listed above?", default=False, style=style, qmark=QMARK
                )
            )
        )
    return bool(_ask_action(questionary.confirm("Create the bug report?", default=True, style=style, qmark=QMARK)))


def _bug_report_action(
    session_row: SessionRow, index: int, row: AttemptRow, workspace: Path, questionary: Any, style: Any
) -> None:
    """The Report-a-bug flow: freeze first, confirm the frozen bytes, then pack.

    Any exit before the record lands deletes the staging directory; the pin
    from bundling stays (it cannot be told apart from a user's own pin, and
    the disclosure line covers it)."""
    status = console.status("Collecting the trajectory snapshot...", spinner="dots")

    def _collected() -> None:
        console.print("Snapshot collected; the attempt was pinned so cleanup won't remove it.")
        status.update("Redacting a copy...")

    try:
        with status:
            prep = breport.prepare_trajectory(
                row.key, expected_traces=row.traces, workspace=workspace, on_collected=_collected
            )
    except breport.StaleAttemptError:
        _print_stale_refresh()
        return
    except (ValueError, LookupError, breport.PreparationError) as exc:
        console.print(f"[red]✗ Could not prepare the trajectory snapshot: {escape(str(exc))}[/red]", highlight=False)
        return

    try:
        fields = _ask_problem_fields(questionary, style)
        try:
            prep = breport.freeze_export(prep, **fields, decide=make_review_decider(questionary, style))
        except breport.PreparationError as exc:
            console.print(
                f"[red]✗ Could not prepare the trajectory snapshot: {escape(str(exc))}[/red]", highlight=False
            )
            return
        _print_bug_summary(session_row, index, row, prep)
        if not _confirm_create(questionary, style, prep):
            console.print(_CANCELLED_MESSAGE)
            return
        try:
            _record_dir, record = breport.confirm_and_package(prep)
        except breport.StaleAttemptError:
            _print_stale_refresh()
            return
        except breport.PreparationError as exc:
            console.print(
                f"[red]✗ Could not prepare the trajectory snapshot: {escape(str(exc))}[/red]", highlight=False
            )
            return
        except breport.PackagingError as exc:
            _print_packaging_failure(prep.report_id, str(exc), retryable=exc.retryable)
            return
        _print_report_ready(record)
    except (_ActionCancelledError, treview.ReviewCancelledError):
        console.print(_CANCELLED_MESSAGE)
    finally:
        # A no-op after the record landed (the staging directory was renamed
        # into place); everywhere else it removes the pre-confirmation state.
        prep.cleanup()


def _print_report_details(record: dict[str, Any]) -> None:
    console.print(f"Bug report {escape(record['report_id'])} ({escape(record['status'])})", highlight=False)
    console.print(f"  Created:  {_fmt_ts_full(record.get('created_at'))}", highlight=False)
    description = _collapse_text((record.get("problem") or {}).get("description")) or ""
    console.print(f"  Problem:  {escape(description)}", highlight=False)
    completeness = record.get("completeness") or {}
    status_text = str(completeness.get("status") or "unknown")
    reasons = completeness.get("reasons") or []
    if reasons:
        status_text += f" ({len(reasons)} reason(s))"
    console.print(f"  Completeness: {escape(status_text)}", highlight=False)
    for notice in (record.get("redaction") or {}).get("security_notices") or []:
        console.print(f"  [yellow]! {escape(notice)}[/yellow]", highlight=False)
    if record["status"] == breport.STATUS_LOCAL_READY:
        console.print(f"  Package:  [cyan]{escape(record['package']['path'])}[/cyan]", highlight=False)
        console.print("  Not uploaded — hand the package file to a developer yourself.")
    elif record["status"] == breport.STATUS_FAILED:
        console.print(f"  Failure:  {escape(record['failure'].get('reason') or '')}", highlight=False)


def _bug_reports_screen(row: AttemptRow, questionary: Any, style: Any) -> None:
    """List, inspect, and retry this attempt's bug reports (design 1.8)."""
    reports = breport.reports_for_attempt(row.key, row.traces)
    while reports:
        choices = []
        for record_dir, record in reports:
            description = _label_text((record.get("problem") or {}).get("description"), 40) or ""
            label = (
                f"{record['report_id']}  {record['status']:<11}"
                f"  {_fmt_ts_full(record.get('created_at'))}  {description}"
            )
            choices.append(questionary.Choice(label, value=(record_dir, record)))
        picked = _select_screen(questionary, style, "Bug report:", _HELP_BUG_REPORTS, choices)
        if picked is _BACK:
            return
        record_dir, record = picked
        _print_report_details(record)
        if record["status"] == breport.STATUS_FAILED and record["failure"].get("retryable"):
            action = _select_screen(
                questionary, style, "Action:", _HELP_ACTION, [questionary.Choice("Retry packaging", value="retry")]
            )
            if action is _BACK:
                reports = breport.reports_for_attempt(row.key, row.traces)
                continue
            try:
                with console.status("Packing the report...", spinner="dots"):
                    retried = breport.retry_packaging(record_dir)
            except breport.PackagingError as exc:
                _print_packaging_failure(record["report_id"], str(exc), retryable=exc.retryable)
            else:
                _print_report_ready(retried)
        reports = breport.reports_for_attempt(row.key, row.traces)
    console.print("[dim]No bug reports for this attempt.[/dim]")


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
                if _preview_screen(index, row):
                    _preview_wait(questionary, style)
            continue
        index, row = picked
        _crumb("Attempt", f"#{index}")

        actions = [
            ("Report a bug", "bug"),
            ("Save (bundle)", "save"),
            ("Report (redact + tarball)", "report"),
            ("Minimize (cassette)", "minimize"),
            ("Verdict", "verdict"),
            ("Unpin", "unpin") if row.pinned else ("Pin", "pin"),
        ]
        if row.merged:
            actions.append(("Split", "split"))
        report_count = len(breport.reports_for_attempt(row.key, row.traces))
        if report_count:
            actions.append((f"Bug reports ({report_count})", "bug_list"))
        action = _select_screen(
            questionary, style, "Action:", _HELP_ACTION, [questionary.Choice(t, value=v) for t, v in actions]
        )
        if action is _BACK:
            continue
        _crumb("Action", {v: t for t, v in actions}[action])
        if action == "bug":
            _bug_report_action(session_row, index, row, workspace, questionary, style)
        elif action == "bug_list":
            _bug_reports_screen(row, questionary, style)
        else:
            _run_action(action, row, workspace, questionary, style)
        return _REFRESH


def browse_trajectories(workspace: Path | None = None) -> None:
    """Run the interactive browser until the user exits."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    ws = workspace or _default_workspace()
    try:
        breport.cleanup_stale_staging()
    except Exception:
        _log.debug("stale bug report staging cleanup failed", exc_info=True)
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
