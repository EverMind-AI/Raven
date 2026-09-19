"""Interactive full-screen viewer for the trajectory conversation preview.

Replaces the external ``less`` pager: help, global collapse, and label
filtering all need in-application state and custom key bindings that an
external pager cannot carry. Built on prompt_toolkit (already the browser's
prompt engine), run as a sequential standalone Application like every other
browser screen.

Contracts:

- ``make_lines(collapse, label_filter, width)`` is the single content source;
  the viewer passes the *actual* content-area width on every render (results
  cached per ``(collapse, filter, width)``), so a terminal resize re-wraps and
  re-judges collapsing instead of showing a stale layout, and a height change
  re-clamps the scroll offset.
- Rich ``Text`` lines convert to prompt_toolkit fragments through Rich's own
  segment rendering (whole-line ``Text.style`` and span styles are already
  merged there); prompt_toolkit has no dim attribute, so dim styles map to
  muted truecolor approximations.
- Ctrl+C is the global cancel in all three states (viewing, help, filter
  input): it is registered last so it wins over the help page's any-key
  return and is never treated as filter input. The caller receives the
  browser-wide cancel result; q stays a plain character while typing a filter
  and merely leaves the help page when it is open.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from rich.console import Console
from rich.text import Text

_STATE_VIEW = "view"
_STATE_HELP = "help"
_STATE_FILTER = "filter"

_RESULT_DONE = "done"
_RESULT_CANCELLED = "cancelled"

# prompt_toolkit styles for the rich styles the preview renderer emits.
# prompt_toolkit has no dim attribute; dim variants use muted colors.
_PT_STYLES = {
    "green": "fg:ansigreen",
    "cyan": "fg:ansicyan",
    "yellow": "fg:ansiyellow",
    "magenta": "fg:ansimagenta",
    "blue": "fg:ansiblue",
    "red": "fg:ansired",
    "dim": "fg:#808080",
    "dim yellow": "fg:#8a8a00",
    "dim cyan": "fg:#008787",
}

_HELP_LINES = (
    "Preview keys",
    "",
    "  Up/Down, j/k     scroll one line",
    "  Space/f, b       page down / page up",
    "  PgDn / PgUp      page down / page up",
    "  g / Home         jump to the top",
    "  G / End          jump to the bottom",
    "  s                collapse / expand long contents",
    "  %                filter by label (empty clears)",
    "  h                this help",
    "  q / Esc          back to the attempt list",
    "  Ctrl+C           quit the browser",
    "",
    "scroll keys page this help; any other key goes back",
)

_NO_MATCH_LINE = "(no items match the filter)"
_HELP_HINT = "h for help"


def _cwidth(text: str) -> int:
    from prompt_toolkit.utils import get_cwidth

    return get_cwidth(text)


def _clip_cells(text: str, cells: int) -> str:
    """Truncate by display width (an ellipsis replaces the overflow)."""
    if cells <= 0:
        return ""
    if _cwidth(text) <= cells:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        w = _cwidth(ch)
        if used + w > cells - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _wrap_cells(text: str, width: int) -> list[str]:
    """Hard-wrap one line by display width (help text is short and plain)."""
    lines: list[str] = []
    current: list[str] = []
    used = 0
    for ch in text:
        w = _cwidth(ch)
        if current and used + w > width:
            lines.append("".join(current))
            current, used = [], 0
        current.append(ch)
        used += w
    lines.append("".join(current))
    return lines


class LinesProvider(Protocol):
    def __call__(self, collapse: bool, label_filter: str | None, width: int) -> list[Text]: ...


_STYLE_CONSOLE = Console(color_system="truecolor", force_terminal=True)


def _pt_style(style: Any) -> str:
    if not style:
        return ""
    return _PT_STYLES.get(str(style), "")


def _line_fragments(line: Text) -> list[tuple[str, str]]:
    """One rich Text line as prompt_toolkit fragments.

    Rich's segment rendering covers span styles but leaves the whole-line
    ``Text.style`` to the enclosing console render, so it is combined here
    explicitly — many preview lines carry only the line style (a red error
    line, a dim-yellow degradation note) and iterating spans alone would
    drop exactly those."""
    from rich.style import Style

    base = _STYLE_CONSOLE.get_style(line.style) if line.style else None
    fragments: list[tuple[str, str]] = []
    for segment in line.render(_STYLE_CONSOLE, end=""):
        if not segment.text:
            continue
        style = segment.style
        if base is not None:
            style = base + (style or Style())
        fragments.append((_pt_style(style), segment.text))
    return fragments


class _PreviewViewer:
    """State and bindings for one viewer run; ``run`` drives the Application."""

    def __init__(self, make_lines: LinesProvider, title: str) -> None:
        self._make_lines = make_lines
        self._title = title
        self._collapse = False
        self._filter: str | None = None
        self._offset = 0
        self._help_offset = 0
        self._state = _STATE_VIEW
        self._cache: dict[tuple[bool, str | None, int], list[Text]] = {}
        self._app: Any = None
        self._filter_buffer: Any = None

    # -- geometry ------------------------------------------------------

    def _size(self) -> tuple[int, int]:
        size = self._app.output.get_size()
        return size.columns, max(size.rows - 1, 1)

    def _lines(self, width: int) -> list[Text]:
        key = (self._collapse, self._filter, width)
        cached = self._cache.get(key)
        if cached is None:
            try:
                cached = self._make_lines(self._collapse, self._filter, width)
            except Exception:  # noqa: BLE001 — a render bug must not wedge the terminal
                cached = [Text("(preview rendering failed)", style="red")]
            if not cached:
                cached = [Text(_NO_MATCH_LINE, style="dim")]
            self._cache[key] = cached
        return cached

    def _help_lines(self, width: int) -> list[str]:
        wrapped: list[str] = []
        for line in _HELP_LINES:
            wrapped.extend(_wrap_cells(line, max(width, 2)))
        return wrapped

    def _in_help(self) -> bool:
        return self._state == _STATE_HELP

    def _total(self, width: int) -> int:
        return len(self._help_lines(width)) if self._in_help() else len(self._lines(width))

    def _get_offset(self) -> int:
        return self._help_offset if self._in_help() else self._offset

    def _set_offset(self, value: int) -> None:
        if self._in_help():
            self._help_offset = value
        else:
            self._offset = value

    def _clamp_active(self) -> None:
        width, height = self._size()
        total = self._total(width)
        self._set_offset(max(0, min(self._get_offset(), total - height)))

    # -- content -------------------------------------------------------

    def _content_fragments(self) -> list[tuple[str, str]]:
        width, height = self._size()
        self._clamp_active()
        offset = self._get_offset()
        if self._in_help():
            page = self._help_lines(width)[offset : offset + height]
            return [("", "\n".join(page))]
        lines = self._lines(width)
        fragments: list[tuple[str, str]] = []
        for line in lines[offset : offset + height]:
            fragments.extend(_line_fragments(line))
            fragments.append(("", "\n"))
        if fragments:
            fragments.pop()
        return fragments

    def _status_fragments(self) -> list[tuple[str, str]]:
        """The bottom bar, laid out by display width for the real terminal.

        The scroll range and the help hint always survive; the variable
        fields (title, filter, collapsed) shrink first — the first one that
        does not fit whole is clipped and the rest are dropped. On a terminal
        too narrow even for the hint, the hint compacts to ``h`` and finally
        the whole bar clips rather than overflowing."""
        width, height = self._size()
        self._clamp_active()
        total = self._total(width)
        offset = self._get_offset()
        first = min(offset + 1, total)
        last = min(offset + height, total)
        right = f"{first}-{last}/{total}"
        if self._in_help():
            variable = [self._title, "help"]
        else:
            variable = [self._title]
            if self._filter:
                variable.append(f"filter: {self._filter}")
            if self._collapse:
                variable.append("collapsed")
        available = width - _cwidth(right) - 1
        hint = _HELP_HINT if _cwidth(_HELP_HINT) <= available else ("h" if available >= 1 else "")
        budget = available - _cwidth(hint) - (3 if hint else 0)
        kept: list[str] = []
        for part in variable:
            need = _cwidth(part) + (3 if kept else 0)
            if need <= budget:
                kept.append(part)
                budget -= need
                continue
            clipped = _clip_cells(part, budget - (3 if kept else 0))
            if clipped:
                kept.append(clipped)
            break
        left_text = " · ".join([*kept, hint] if hint else kept)
        gap = max(width - _cwidth(left_text) - _cwidth(right), 1)
        bar = left_text + " " * gap + right
        return [("fg:#808080", _clip_cells(bar, width) if _cwidth(bar) > width else bar)]

    # -- state changes -------------------------------------------------

    def _move(self, lines: int) -> None:
        self._set_offset(self._get_offset() + lines)
        self._clamp_active()

    def _page(self, direction: int) -> None:
        _width, height = self._size()
        self._set_offset(self._get_offset() + direction * max(height - 1, 1))
        self._clamp_active()

    def _to_end(self) -> None:
        width, height = self._size()
        self._set_offset(max(0, self._total(width) - height))

    def _toggle_collapse(self) -> None:
        self._collapse = not self._collapse
        self._clamp_active()

    def _apply_filter(self, needle: str) -> None:
        # Typed (or pasted) input is untrusted like everything else on this
        # screen: strip non-printable characters before it reaches the status
        # bar or the renderer.
        cleaned = "".join(ch for ch in needle if ch.isprintable()).strip()
        self._filter = cleaned or None
        self._offset = 0
        self._state = _STATE_VIEW

    # -- application ---------------------------------------------------

    def _build_app(self, input: Any = None, output: Any = None) -> Any:
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

        self._filter_buffer = Buffer(multiline=False)
        in_view = Condition(lambda: self._state == _STATE_VIEW)
        in_help = Condition(lambda: self._state == _STATE_HELP)
        in_filter = Condition(lambda: self._state == _STATE_FILTER)

        # focusable keeps the layout valid (something must hold focus outside
        # filter input); show_cursor=False stops that focus from painting a
        # cursor over the first content character in view and help alike —
        # only the filter input line shows a caret, where typing happens.
        content_control = FormattedTextControl(self._content_fragments, focusable=True, show_cursor=False)
        content = Window(content_control, wrap_lines=False)
        status = ConditionalContainer(
            Window(FormattedTextControl(self._status_fragments), height=1),
            filter=~in_filter,
        )
        filter_control = BufferControl(buffer=self._filter_buffer)
        filter_row = ConditionalContainer(
            Window(
                filter_control,
                height=1,
                get_line_prefix=lambda _line, _wrap: [("fg:#808080", "%")],
            ),
            filter=in_filter,
        )

        kb = KeyBindings()

        def _bind(condition: Any, *keys: Any):
            def decorate(handler):
                for key in keys:
                    kb.add(key, filter=condition)(handler)
                return handler

            return decorate

        @_bind(in_view, "q", "escape")
        def _quit(event: Any) -> None:
            event.app.exit(result=_RESULT_DONE)

        @_bind(in_view, "h")
        def _help(event: Any) -> None:
            self._help_offset = 0
            self._state = _STATE_HELP

        @_bind(in_view, "s")
        def _collapse(event: Any) -> None:
            self._toggle_collapse()

        @_bind(in_view, "%")
        def _filter(event: Any) -> None:
            # The input starts empty: pressing Enter with nothing typed is
            # the documented way to clear the current filter.
            self._filter_buffer.text = ""
            self._state = _STATE_FILTER
            event.app.layout.focus(filter_control)

        # Any-key-returns is registered before the scroll keys: a later
        # registration wins in prompt_toolkit, so in help the scroll keys
        # page the help text while every other key drops back to the preview
        # (its own scroll position untouched).
        @kb.add(Keys.Any, filter=in_help)
        def _leave_help(event: Any) -> None:
            self._state = _STATE_VIEW

        @kb.add("escape", filter=in_help, eager=True)
        def _leave_help_esc(event: Any) -> None:
            self._state = _STATE_VIEW

        scrolling = in_view | in_help

        @_bind(scrolling, "up", "k")
        def _up(event: Any) -> None:
            self._move(-1)

        @_bind(scrolling, "down", "j")
        def _down(event: Any) -> None:
            self._move(1)

        @_bind(scrolling, "pageup", "b")
        def _pageup(event: Any) -> None:
            self._page(-1)

        @_bind(scrolling, "pagedown", " ", "f")
        def _pagedown(event: Any) -> None:
            self._page(1)

        @_bind(scrolling, "g", "home")
        def _top(event: Any) -> None:
            self._set_offset(0)

        @_bind(scrolling, "G", "end")
        def _bottom(event: Any) -> None:
            self._to_end()

        @kb.add("enter", filter=in_filter)
        def _apply(event: Any) -> None:
            self._apply_filter(self._filter_buffer.text)
            event.app.layout.focus(content_control)

        @kb.add("escape", filter=in_filter, eager=True)
        def _cancel_filter(event: Any) -> None:
            self._state = _STATE_VIEW
            event.app.layout.focus(content_control)

        # Registered last so it wins in every state: the browser-wide cancel
        # must beat the help page's any-key return and the filter buffer's
        # input handling.
        @kb.add("c-c", eager=True)
        def _cancel(event: Any) -> None:
            event.app.exit(result=_RESULT_CANCELLED)

        app = Application(
            layout=Layout(HSplit([content, status, filter_row])),
            key_bindings=kb,
            full_screen=True,
            input=input,
            output=output,
        )
        app.ttimeoutlen = 0.05
        return app

    def run(self, input: Any = None, output: Any = None) -> str:
        self._app = self._build_app(input=input, output=output)
        result = self._app.run()
        return result if result in (_RESULT_DONE, _RESULT_CANCELLED) else _RESULT_DONE


def view_lines(
    make_lines: LinesProvider | Callable[..., list[Text]],
    *,
    title: str,
    input: Any = None,
    output: Any = None,
) -> bool:
    """Run the viewer until the user leaves; True means Ctrl+C cancelled.

    The caller translates a True result into the browser-wide cancel
    (:class:`trajectory_browse._CancelledError`); a normal q/Esc exit
    returns False."""
    viewer = _PreviewViewer(make_lines, title)
    return viewer.run(input=input, output=output) == _RESULT_CANCELLED


__all__ = ["view_lines"]
