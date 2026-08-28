import io

import pytest
from rich.console import Console

from raven.cli._progress_line import (
    ProgressRenderer,
    TurnProgress,
    describe_start,
    looks_failed,
    resolve_mode,
    turn_language,
)
from raven.spine.events import ToolEvent, ToolPhase


def _start(name="web_search", tool_call_id="t1", arguments=None, iteration=0) -> ToolEvent:
    return ToolEvent(
        phase=ToolPhase.START,
        tool_call_id=tool_call_id,
        name=name,
        arguments=arguments or {},
        iteration=iteration,
    )


def _complete(tool_call_id="t1", preview="") -> ToolEvent:
    return ToolEvent(
        phase=ToolPhase.COMPLETE,
        tool_call_id=tool_call_id,
        result_preview=preview,
    )


# --- describe_start ---


def test_describe_start_search_shows_query():
    en = describe_start("web_search", {"query": "serper.dev pricing"})
    zh = describe_start("web_search", {"query": "serper.dev pricing"}, "zh")
    assert "Searching" in en and "serper.dev pricing" in en
    assert "搜索" in zh and "serper.dev pricing" in zh


def test_describe_start_search_clips_long_query():
    line = describe_start("web_search", {"query": "x" * 200})
    assert len(line) < 100 and line.endswith('…"')


def test_describe_start_fetch_shows_host_and_path():
    line = describe_start("web_fetch", {"url": "https://serper.dev/pricing/"})
    assert "Reading" in line and "serper.dev/pricing" in line
    assert "https://" not in line  # scheme dropped, host+path kept
    assert "阅读" in describe_start("web_fetch", {"url": "https://serper.dev/pricing/"}, "zh")


def test_describe_start_unknown_lang_falls_back_to_english():
    # A language the label table does not carry must not raise.
    assert "Searching" in describe_start("web_search", {"query": "q"}, "fr")


def test_describe_start_unknown_tool_falls_back_to_name():
    # DR only has the two web tools, but the same REPL runs non-DR turns
    # (read_file etc.) — the fallback is a required path, not decoration.
    assert describe_start("read_file", {"path": "x"}) == "· read_file"


def test_describe_start_tolerates_missing_arguments():
    assert "Searching" in describe_start("web_search", None)


# --- turn_language ---


@pytest.mark.parametrize(
    ("question", "lang"),
    [
        ("2026年第三季度，美联储可能加息吗，有什么信号？", "zh"),
        ("What are the Fed rate signals for Q3 2026?", "en"),
        ("OpenRouter 的 deepseek-v4-flash 价格是多少", "zh"),  # Latin product name, still zh
        ("What is 小红书's revenue", "en"),  # Latin prose quoting CJK, still en
        ("", "en"),
    ],
)
def test_turn_language_follows_the_question(question: str, lang: str):
    assert turn_language(question) == lang


# --- looks_failed ---


@pytest.mark.parametrize(
    ("preview", "failed"),
    [
        ('{"error": "not in corpus", "url": "u"}', True),
        ("Error: file not found", True),
        ('  {"error": "proxy"}', True),
        ('{"url": "u", "status": 200, "text": "..."}', False),
        ("plain tool output", False),
    ],
)
def test_looks_failed_is_a_prefix_check(preview: str, failed: bool):
    assert looks_failed(preview) is failed


# --- resolve_mode ---


@pytest.mark.parametrize(
    ("style", "is_terminal", "logs", "expected"),
    [
        ("off", True, False, "off"),
        ("lines", True, False, "lines"),
        ("lines", False, False, "off"),  # no TTY -> tool progress off entirely
        ("live", True, False, "live"),
        ("live", True, True, "lines"),  # loguru on stderr fights a rich Live
        ("live", False, False, "off"),
    ],
)
def test_resolve_mode(style, is_terminal, logs, expected):
    assert resolve_mode(style, is_terminal=is_terminal, logs=logs) == expected


# --- TurnProgress ---


def test_turn_progress_counts_by_tool_kind():
    p = TurnProgress()
    p.start(_start("web_search", "t1"))
    p.start(_start("web_fetch", "t2"))
    p.start(_start("web_fetch", "t3"))
    p.start(_start("read_file", "t4"))
    assert (p.searches, p.pages, p.other_tools) == (1, 2, 1)


def test_turn_progress_tracks_iteration_high_water_mark():
    p = TurnProgress(max_iterations=40)
    p.start(_start(iteration=3))
    p.start(_start(tool_call_id="t2", iteration=0))  # emitter without the field
    assert p.iteration == 3


def test_turn_progress_complete_pairs_by_tool_call_id():
    p = TurnProgress()
    p.start(_start("web_fetch", "t1", {"url": "https://a.io/x"}))
    started, ok = p.complete(_complete("t1", preview='{"url": "a"}'))
    assert "a.io/x" in started
    assert ok is True


def test_turn_progress_complete_marks_failure_from_preview():
    p = TurnProgress()
    p.start(_start("web_fetch", "t1"))
    _, ok = p.complete(_complete("t1", preview='{"error": "boom"}'))
    assert ok is False


def test_turn_progress_unpaired_complete_returns_none():
    # The TUI runner's synthetic message-tool complete never had a START.
    assert TurnProgress().complete(_complete("msg-1")) is None


def test_turn_progress_reset_clears_counters_and_pending():
    p = TurnProgress(max_iterations=40)
    p.start(_start(iteration=5))
    p.reset()
    assert (p.iteration, p.searches, p.pages, p.other_tools) == (0, 0, 0, 0)
    assert p.complete(_complete("t1")) is None  # pending cleared too
    assert p.max_iterations == 40  # the loop constant survives reset


def test_status_line_english_pluralizes():
    p = TurnProgress(max_iterations=40)
    assert p.status_line() == "Raven is thinking..."  # nothing counted yet
    p.start(_start("web_search", "t1", iteration=4))
    p.start(_start("web_fetch", "t2", iteration=4))
    assert p.status_line() == "Raven is thinking · round 4/40 · 1 search 1 page"
    p.start(_start("web_fetch", "t3", iteration=4))
    assert "2 pages" in p.status_line()


def test_status_line_chinese():
    p = TurnProgress(max_iterations=40)
    p.reset("zh")
    assert p.status_line() == "Raven 思考中..."
    p.start(_start("web_search", "t1", iteration=4))
    p.start(_start("web_fetch", "t2", iteration=4))
    assert p.status_line() == "Raven 思考中 · 第 4/40 轮 · 1 搜索 1 页"


def test_status_line_omits_round_without_max_iterations():
    # Never "4/0".
    q = TurnProgress()
    q.start(_start("web_search", "t1", iteration=4))
    assert "round" not in q.status_line()


# --- ProgressRenderer ---


def _renderer(mode: str, *, spinner: bool = False, force_terminal: bool = False):
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=force_terminal, width=120)
    return buf, ProgressRenderer(console, mode=mode, spinner=spinner, max_iterations=40)


def test_renderer_lines_mode_prints_start_only_on_success():
    buf, r = _renderer("lines")
    r.on_tool(_start("web_search", "t1", {"query": "q"}))
    start_only = buf.getvalue()
    assert "Searching" in start_only
    r.on_tool(_complete("t1", preview="ok"))
    # A successful completion adds nothing — no size/duration echo.
    assert buf.getvalue() == start_only


def test_renderer_lines_mode_echoes_failure():
    buf, r = _renderer("lines")
    r.on_tool(_start("web_fetch", "t1", {"url": "https://a.io/p"}))
    r.on_tool(_complete("t1", preview='{"error": "boom"}'))
    assert "└ failed" in buf.getvalue()


def test_renderer_off_mode_prints_nothing():
    buf, r = _renderer("off")
    r.on_tool(_start())
    r.on_tool(_complete())
    assert buf.getvalue() == ""


def test_renderer_notice_clips_to_first_line():
    buf, r = _renderer("lines")
    r.on_notice("first line " + "x" * 300 + "\nsecond line")
    out = buf.getvalue()
    assert "second line" not in out
    assert "…" in out


def test_renderer_notice_ignores_empty_text():
    buf, r = _renderer("lines")
    r.on_notice("   \n  ")
    assert buf.getvalue() == ""


def test_renderer_swallows_render_exceptions():
    # A raising render callback would put the hub worker into its
    # retry-with-backoff path (spine/delivery.py) for a cosmetic line.
    _, r = _renderer("lines")
    r._print = lambda line: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    r.on_tool(_start())  # must not raise
    r.on_notice("text")  # must not raise


def test_thinking_ctx_resets_counters_each_turn():
    _, r = _renderer("lines", spinner=False)
    r.on_tool(_start("web_search", "t1", iteration=7))
    with r.thinking_ctx():
        pass
    assert (r.progress.iteration, r.progress.searches) == (0, 0)


def test_thinking_ctx_sets_the_turn_language_from_the_question():
    buf, r = _renderer("lines", spinner=False)
    with r.thinking_ctx("美联储 2026 年会加息吗"):
        r.on_tool(_start("web_search", "t1", {"query": "fed 2026"}))
        r.on_tool(_complete("t1", preview='{"error": "boom"}'))
    out = buf.getvalue()
    assert "搜索" in out and "失败" in out
    assert "Searching" not in out  # one language per turn, no mixing


def test_thinking_ctx_relanguages_each_turn():
    buf, r = _renderer("lines", spinner=False)
    with r.thinking_ctx("美联储会加息吗"):
        r.on_tool(_start("web_search", "t1", {"query": "q"}))
    with r.thinking_ctx("Will the Fed hike?"):
        r.on_tool(_start("web_search", "t2", {"query": "q"}))
    out = buf.getvalue()
    assert "搜索" in out and "Searching" in out  # each turn follows its own question


def test_thinking_ctx_without_spinner_is_a_noop_context():
    _, r = _renderer("lines", spinner=False)
    with r.thinking_ctx():
        pass  # nullcontext: nothing rendered, nothing to update


def test_live_mode_routes_start_to_status_and_done_to_history():
    buf, r = _renderer("live", spinner=True, force_terminal=True)
    with r.thinking_ctx():
        r.on_tool(_start("web_fetch", "t1", {"url": "https://a.io/p"}))
        r.on_tool(_complete("t1", preview='{"url": "a"}'))
    out = buf.getvalue()
    # The completed line lands in scrolling history above the status line,
    # without a size/duration echo.
    assert "a.io/p" in out and "字符" not in out


def test_live_mode_marks_failed_history_line():
    buf, r = _renderer("live", spinner=True, force_terminal=True)
    with r.thinking_ctx():
        r.on_tool(_start("web_fetch", "t1", {"url": "https://a.io/p"}))
        r.on_tool(_complete("t1", preview='{"error": "boom"}'))
    assert "failed" in buf.getvalue()


def test_live_mode_outside_turn_falls_back_to_lines():
    # ToolEvents can trail the thinking context (render barrier is wait_idle);
    # with no live status they print as plain lines instead of being lost.
    buf, r = _renderer("live", spinner=True, force_terminal=True)
    r.on_tool(_start("web_search", "t1", {"query": "q"}))
    assert "Searching" in buf.getvalue()


def test_suspended_stops_and_restarts_the_active_spinner() -> None:
    """The ask_user prompt owns the terminal while it reads; a status animating
    on the same fd garbles the line. Works in both spinner modes, because
    ``_active`` is tracked apart from the live-mode-only ``_status``."""
    for mode in ("lines", "live"):
        renderer = ProgressRenderer(
            Console(file=io.StringIO(), force_terminal=True, width=80),
            mode=mode,
            spinner=True,
        )
        with renderer.thinking_ctx("q"):
            status = renderer._active
            assert status is not None
            with renderer.suspended():
                assert renderer._active is status  # tracked, merely stopped
            # Restarted: rich raises on a second start of a running Live, so
            # entering again proves suspended() really restarted it.
            with renderer.suspended():
                pass
        assert renderer._active is None


def test_suspended_is_a_noop_without_a_spinner() -> None:
    renderer = ProgressRenderer(
        Console(file=io.StringIO(), force_terminal=False, width=80),
        mode="off",
        spinner=False,
    )
    with renderer.thinking_ctx("q"):
        with renderer.suspended():
            pass  # nothing active, nothing to stop — and nothing raises
