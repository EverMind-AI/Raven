"""Tests for the interactive preview viewer (`raven.cli._preview_viewer`)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from rich.text import Text

from raven.cli import _preview_viewer as pviewer

pytest.importorskip("prompt_toolkit")

from prompt_toolkit.data_structures import Size  # noqa: E402
from prompt_toolkit.input.defaults import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402


class _SizedOutput(DummyOutput):
    def __init__(self, columns=40, rows=10):
        self.columns = columns
        self.rows = rows

    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)


def _static_lines(count=20, text="line"):
    def make_lines(collapse, label_filter, width):
        return [Text(f"{text} {i}") for i in range(count)]

    return make_lines


def _run(keys, make_lines, output=None):
    viewer = pviewer._PreviewViewer(make_lines, "Preview #1")
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        result = viewer.run(input=pipe, output=output or _SizedOutput())
    return viewer, result


def _spy(lines_by_call=None, count=8):
    calls = []

    def make_lines(collapse, label_filter, width):
        calls.append((collapse, label_filter, width))
        if lines_by_call is not None:
            return lines_by_call(collapse, label_filter, width)
        return [Text(f"line {i}") for i in range(count)]

    return make_lines, calls


# ── exit and cancel ───────────────────────────────────────────────────


@pytest.mark.parametrize("keys", ["q", "\x1b"], ids=["q", "esc"])
def test_quit_keys_exit_normally(keys):
    _viewer, result = _run(keys, _static_lines())
    assert result == pviewer._RESULT_DONE


@pytest.mark.parametrize(
    "keys",
    ["\x03", "h\x03", "%\x03"],
    ids=["viewing", "help", "filter-input"],
)
def test_ctrl_c_cancels_in_all_three_states(keys):
    _viewer, result = _run(keys, _static_lines())
    assert result == pviewer._RESULT_CANCELLED


def test_view_lines_reports_cancellation_to_caller():
    with create_pipe_input() as pipe:
        pipe.send_text("\x03")
        assert pviewer.view_lines(_static_lines(), title="t", input=pipe, output=_SizedOutput()) is True
    with create_pipe_input() as pipe:
        pipe.send_text("q")
        assert pviewer.view_lines(_static_lines(), title="t", input=pipe, output=_SizedOutput()) is False


# ── help page ─────────────────────────────────────────────────────────


def test_help_swallows_keys_until_any_key_returns():
    # In help, "s" is the any-key return, not the collapse toggle; the
    # control case proves "s" toggles when help is not open.
    viewer, result = _run("hsq", _static_lines())
    assert result == pviewer._RESULT_DONE
    assert viewer._collapse is False
    assert viewer._state == pviewer._STATE_VIEW

    control, _result = _run("sq", _static_lines())
    assert control._collapse is True


def test_help_content_lists_new_keys_in_aligned_columns():
    viewer = pviewer._PreviewViewer(_static_lines(), "Preview #1")
    viewer._app = SimpleNamespace(output=_SizedOutput(columns=70, rows=20))
    viewer._state = pviewer._STATE_HELP
    text = "".join(fragment for _style, fragment in viewer._content_fragments())
    for token in ("collapse / expand", "filter by label", "this help", "back to the attempt list", "Ctrl+C"):
        assert token in text
    rows = [line for line in pviewer._HELP_LINES if line.startswith("  ")]
    assert rows
    # Two aligned columns: every description starts at the same offset, with
    # a padded key column before it.
    assert all(line[18] == " " and line[19] != " " for line in rows)


# ── collapse and filter ───────────────────────────────────────────────


def test_collapse_toggles_and_passes_flag():
    make_lines, calls = _spy()
    viewer, _result = _run("ssq", make_lines)
    assert viewer._collapse is False
    assert {c[0] for c in calls} == {False, True}


def test_filter_input_applies_and_clears():
    make_lines, calls = _spy()
    viewer, _result = _run("%llm\rq", make_lines)
    assert viewer._filter == "llm"
    assert any(c[1] == "llm" for c in calls)
    assert viewer._offset == 0

    cleared, _result = _run("%llm\r%\rq", make_lines)
    assert cleared._filter is None


def test_q_is_a_plain_character_while_typing_a_filter():
    viewer, result = _run("%q\rq", _static_lines())
    assert result == pviewer._RESULT_DONE
    assert viewer._filter == "q"


def test_escape_cancels_filter_input():
    viewer, _result = _run("%llm\x1bq", _static_lines())
    assert viewer._filter is None
    assert viewer._state == pviewer._STATE_VIEW


def test_empty_result_shows_no_match_placeholder():
    def make_lines(collapse, label_filter, width):
        return []

    viewer = pviewer._PreviewViewer(make_lines, "Preview #1")
    viewer._app = SimpleNamespace(output=_SizedOutput())
    text = "".join(fragment for _style, fragment in viewer._content_fragments())
    assert pviewer._NO_MATCH_LINE in text


# ── scrolling ─────────────────────────────────────────────────────────


def test_scroll_keys_and_clamping():
    output = _SizedOutput(rows=6)  # content height 5
    viewer, _result = _run("Gq", _static_lines(20), output=output)
    assert viewer._offset == 15
    viewer, _result = _run("Ggq", _static_lines(20), output=output)
    assert viewer._offset == 0
    viewer, _result = _run("jjkq", _static_lines(20), output=output)
    assert viewer._offset == 1
    viewer, _result = _run("Gjjq", _static_lines(20), output=output)
    assert viewer._offset == 15  # clamped at the bottom
    viewer, _result = _run(" q", _static_lines(20), output=output)
    assert viewer._offset == 4  # page = height - 1


# ── resize ────────────────────────────────────────────────────────────


def test_resize_recomputes_width_and_clamps_offset():
    output = _SizedOutput(columns=40, rows=10)
    calls = []

    def make_lines(collapse, label_filter, width):
        calls.append(width)
        if len(calls) == 1:
            output.columns = 20
            output.rows = 4
        return [Text(f"line {i}") for i in range(8)]

    viewer, _result = _run("Gq", make_lines, output=output)
    assert 40 in calls and 20 in calls  # the actual content width reaches make_lines
    assert viewer._offset <= 8 - (4 - 1)  # clamped against the shrunken height


# ── status bar ────────────────────────────────────────────────────────


def test_status_bar_states():
    viewer = pviewer._PreviewViewer(_static_lines(20), "Preview #1")
    viewer._app = SimpleNamespace(output=_SizedOutput(columns=100, rows=6))

    def status():
        return "".join(fragment for _style, fragment in viewer._status_fragments())

    base = status()
    assert "h for help" in base and "Preview #1" in base
    assert "1-5/20" in base
    assert "collapsed" not in base and "filter:" not in base

    viewer._collapse = True
    viewer._filter = "llm"
    styled = viewer._status_fragments()
    assert all(style == "fg:#808080" for style, _text in styled)  # muted bar
    text = status()
    assert "collapsed" in text and "filter: llm" in text


def test_status_bar_narrow_keeps_hint_and_range():
    viewer = pviewer._PreviewViewer(_static_lines(30), "Preview #1")
    viewer._app = SimpleNamespace(output=_SizedOutput(columns=40, rows=10))
    viewer._collapse = True
    viewer._filter = "llm"
    text = "".join(fragment for _style, fragment in viewer._status_fragments())
    assert pviewer._cwidth(text) <= 40
    assert "h for help" in text
    assert "1-9/30" in text
    assert "…" in text  # a variable field was clipped, not the essentials

    # Wide CJK filter: budgeting is by display width, not len().
    viewer._filter = "中文过滤中文过滤中文过滤"
    text = "".join(fragment for _style, fragment in viewer._status_fragments())
    assert pviewer._cwidth(text) <= 40
    assert "h for help" in text and "1-9/30" in text

    # Degenerate width: the bar never overflows, the range survives longest.
    viewer._app = SimpleNamespace(output=_SizedOutput(columns=12, rows=10))
    text = "".join(fragment for _style, fragment in viewer._status_fragments())
    assert pviewer._cwidth(text) <= 12
    assert "1-9/30" in text


# ── rich -> prompt_toolkit fragments ──────────────────────────────────


def test_line_fragments_cover_line_styles_and_spans():
    whole = pviewer._line_fragments(Text("boom", style="red"))
    assert whole == [("fg:ansired", "boom")]

    spans = Text()
    spans.append("Tool output: ", style="yellow")
    spans.append("body")
    assert pviewer._line_fragments(spans) == [("fg:ansiyellow", "Tool output: "), ("", "body")]

    layered = Text("note", style="dim yellow")
    assert pviewer._line_fragments(layered) == [("fg:#8a8a00", "note")]

    assert pviewer._line_fragments(Text("")) == []
    unknown = pviewer._line_fragments(Text("x", style="bold underline"))
    assert unknown == [("", "x")]

    joined = "".join(t for _s, t in pviewer._line_fragments(spans))
    assert joined == "Tool output: body"  # no characters lost or repeated


# ── real rendered screen ──────────────────────────────────────────────


def _screen_rows(screen):
    # Screen.width stays 0 under DummyOutput; read each row's real extent.
    rows = []
    height = max(screen.height, max(screen.data_buffer.keys(), default=-1) + 1)
    for y in range(height):
        row = screen.data_buffer[y]
        row_width = max(row.keys(), default=-1) + 1
        rows.append("".join(row[x].char for x in range(row_width)).rstrip())
    return rows


def _last_frame(frames):
    for frame in reversed(frames):
        if any(row for row in frame):
            return frame
    raise AssertionError("no rendered frame with content")


def _run_capture(keys, make_lines, output):
    """Run the viewer collecting every really-rendered frame (row lists)."""
    viewer = pviewer._PreviewViewer(make_lines, "Preview #1")
    frames = []
    with create_pipe_input() as pipe:
        app = viewer._build_app(input=pipe, output=output)
        viewer._app = app

        def _snap(rendering_app):
            screen = rendering_app.renderer.last_rendered_screen
            if screen is not None:
                frames.append(_screen_rows(screen))

        app.after_render += _snap
        pipe.send_text(keys)
        result = app.run()
    return viewer, result, frames


def test_rendered_status_bar_survives_collapse_and_filter_on_narrow_screen():
    output = _SizedOutput(columns=40, rows=10)
    viewer, result, frames = _run_capture("s%llm\r\x03", _static_lines(30, text="LLM line"), output)
    assert result == pviewer._RESULT_CANCELLED
    assert viewer._collapse and viewer._filter == "llm"
    bars = [frame[-1] for frame in frames]
    assert all(pviewer._cwidth(bar) <= 40 for bar in bars)
    final = _last_frame(frames)[-1]
    assert "h for help" in final
    assert "/30" in final and "1-9" in final


def test_rendered_status_bar_with_long_and_cjk_filters():
    long_needle = "a-very-long-filter-string-typed-by-hand-that-keeps-going"
    viewer, _result, frames = _run_capture(
        f"%{long_needle}\r\x03", _static_lines(30), _SizedOutput(columns=80, rows=10)
    )
    bar = _last_frame(frames)[-1]
    assert pviewer._cwidth(bar) <= 80
    assert "h for help" in bar and "/30" in bar

    viewer, _result, frames = _run_capture("%中文过滤\r\x03", _static_lines(30), _SizedOutput(columns=40, rows=10))
    bar = _last_frame(frames)[-1]
    assert pviewer._cwidth(bar) <= 40
    assert "h for help" in bar and "/30" in bar
    assert viewer._filter == "中文过滤"


def _render_frame(viewer, output, state=None, help_offset=0):
    """One synchronous real render (layout -> screen) of the given state."""
    from prompt_toolkit.application.current import set_app

    with create_pipe_input() as pipe:
        app = viewer._build_app(input=pipe, output=output)
        viewer._app = app
        if state is not None:
            viewer._state = state
            viewer._help_offset = help_offset
        with set_app(app):
            app.renderer.render(app, app.layout)
        return _screen_rows(app.renderer.last_rendered_screen)


def test_rendered_help_is_fully_reachable_on_small_screens():
    output = _SizedOutput(columns=40, rows=10)
    viewer = pviewer._PreviewViewer(_static_lines(30), "Preview #1")
    first_page = "\n".join(_render_frame(viewer, output, state=pviewer._STATE_HELP))
    assert "Preview keys" in first_page
    assert "Ctrl+C" not in first_page  # the tail starts off-screen ...

    bottom = "\n".join(_render_frame(viewer, output, state=pviewer._STATE_HELP, help_offset=999))
    assert "Ctrl+C" in bottom  # ... and scrolling reaches it
    assert all(pviewer._cwidth(row) <= 40 for row in bottom.split("\n"))

    # The scroll keys really move the help page (states drive the render above).
    scrolled, _result = _run("h  \x03", _static_lines(30), output=_SizedOutput(columns=40, rows=10))
    assert scrolled._state == pviewer._STATE_HELP
    assert scrolled._help_offset > 0

    # Long descriptions wrap at narrow widths instead of clipping: the full
    # sentence is reachable across the wrapped help lines.
    joined = "".join(viewer._help_lines(40))
    assert "collapse / expand long contents" in joined


def test_help_scrolling_preserves_preview_position():
    output = _SizedOutput(columns=40, rows=10)
    viewer, result = _run("jjh  xq", _static_lines(30), output=output)
    assert result == pviewer._RESULT_DONE
    assert viewer._offset == 2  # the preview position survived the help visit
    assert viewer._state == pviewer._STATE_VIEW


def test_cursor_hidden_in_view_and_help_but_visible_while_filtering():
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.layout.controls import BufferControl

    output = _SizedOutput(columns=40, rows=10)
    for state in (pviewer._STATE_VIEW, pviewer._STATE_HELP):
        viewer = pviewer._PreviewViewer(_static_lines(30), "Preview #1")
        with create_pipe_input() as pipe:
            app = viewer._build_app(input=pipe, output=output)
            viewer._app = app
            viewer._state = state
            with set_app(app):
                app.renderer.render(app, app.layout)
            assert app.renderer.last_rendered_screen.show_cursor is False

    viewer = pviewer._PreviewViewer(_static_lines(30), "Preview #1")
    with create_pipe_input() as pipe:
        app = viewer._build_app(input=pipe, output=output)
        viewer._app = app
        viewer._state = pviewer._STATE_FILTER
        buffer_control = next(c for c in app.layout.find_all_controls() if isinstance(c, BufferControl))
        app.layout.focus(buffer_control)

        async def _render():
            # Rendering a focused BufferControl schedules background tasks
            # and needs a running loop; let them settle before asserting.
            with set_app(app):
                app.renderer.render(app, app.layout)
            await asyncio.sleep(0)

        asyncio.run(_render())
        assert app.renderer.last_rendered_screen.show_cursor is True
