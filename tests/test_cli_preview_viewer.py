"""Tests for the interactive preview viewer (`raven.cli._preview_viewer`)."""

from __future__ import annotations

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
    viewer._app = SimpleNamespace(output=_SizedOutput())
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
    viewer._app = SimpleNamespace(output=_SizedOutput(rows=6))

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
