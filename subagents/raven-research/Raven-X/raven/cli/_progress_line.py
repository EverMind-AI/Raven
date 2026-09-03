"""Tool-progress rendering for the CLI: pure line builders plus the renderer
the CliOutlet callbacks land on.

Display layer only — nothing here is read by the model (AGENTS.md §0.2), so
wording and format are free to change without a drFlow version bump.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from rich.console import Console
from rich.text import Text

from raven.spine.events import ToolEvent, ToolPhase

_ARG_CLIP = 60
_NOTICE_CLIP = 120

# One label set per language; the turn's language is picked from the user's own
# question, so these lines read in the same language as the answer they precede.
# Count entries are (singular, plural) - the two coincide in Chinese.
_LABELS: dict[str, dict[str, Any]] = {
    "en": {
        "search": '🔍 Searching "{}"',
        "fetch": "📄 Reading {}",
        "failed_inline": "— failed",
        "failed_block": "└ failed",
        "thinking": "Raven is thinking",
        "thinking_idle": "Raven is thinking...",
        "round": "round {}/{}",
        "searches": ("{} search", "{} searches"),
        "pages": ("{} page", "{} pages"),
        "tools": ("{} tool", "{} tools"),
    },
    "zh": {
        "search": '🔍 搜索 "{}"',
        "fetch": "📄 阅读 {}",
        "failed_inline": "— 失败",
        "failed_block": "└ 失败",
        "thinking": "Raven 思考中",
        "thinking_idle": "Raven 思考中...",
        "round": "第 {}/{} 轮",
        "searches": ("{} 搜索", "{} 搜索"),
        "pages": ("{} 页", "{} 页"),
        "tools": ("{} 工具", "{} 工具"),
    },
}


def _label(lang: str, key: str) -> Any:
    return _LABELS.get(lang, _LABELS["en"])[key]


def _count(lang: str, key: str, n: int) -> str:
    singular, plural = _label(lang, key)
    return (singular if n == 1 else plural).format(n)


def turn_language(question: str) -> str:
    """``"zh"`` or ``"en"`` for the turn, from the user's own question.

    Delegates to the flow's ``scaffold_language`` rather than re-deriving the
    rule: it counts CJK ideographs against Latin WORDS (a ratio misreads a
    Chinese question quoting a Latin product name), and that tuning was paid
    for once already. Imported lazily so cli keeps booting without the flow.
    """
    try:
        from raven.agent.flow.ask_user import scaffold_language

        return scaffold_language(question or "")
    except Exception:
        return "en"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _host_path(url: str) -> str:
    parts = urlsplit(url)
    if not parts.netloc:
        return _clip(url, _ARG_CLIP)
    return _clip(parts.netloc + parts.path.rstrip("/"), _ARG_CLIP)


def describe_start(name: str, arguments: dict[str, Any] | None, lang: str = "en") -> str:
    args = arguments or {}
    if name == "web_search":
        return _label(lang, "search").format(_clip(str(args.get("query", "")), _ARG_CLIP))
    if name == "web_fetch":
        return _label(lang, "fetch").format(_host_path(str(args.get("url", ""))))
    return f"· {name}"


def looks_failed(preview: str) -> bool:
    # Prefix check, not JSON parsing: every web.py error branch serializes
    # with "error" as the first key, and shell-style failures start with
    # "Error". A miss only renders a failed call as succeeded.
    p = preview.lstrip()
    return p.startswith('{"error"') or p.startswith("Error")


def resolve_mode(style: str, *, is_terminal: bool, logs: bool) -> str:
    """Collapse the configured style and the environment to the effective mode.

    No TTY (redirected stdout, rollout subprocesses) turns tool progress off
    entirely; "live" additionally needs quiet stderr (loguru lines fight a
    rich Live) and degrades to per-line printing under --logs.
    """
    if style == "off" or not is_terminal:
        return "off"
    if style == "live" and not logs:
        return "live"
    return "lines"


@dataclass
class TurnProgress:
    """Counters one CLI turn accumulates from ToolEvents.

    reset() every turn: the REPL reuses one renderer across turns and outlets
    never see TurnStarted/TurnEnded (the hub routes deliverables only). It also
    carries the turn's language, so a pending START line and the status line
    that follows it cannot end up in two languages.
    """

    max_iterations: int = 0
    lang: str = "en"
    iteration: int = 0
    searches: int = 0
    pages: int = 0
    other_tools: int = 0
    _pending: dict[str, str] = field(default_factory=dict)

    def reset(self, lang: str = "en") -> None:
        self.lang = lang
        self.iteration = 0
        self.searches = 0
        self.pages = 0
        self.other_tools = 0
        self._pending.clear()

    def start(self, ev: ToolEvent) -> str:
        if ev.iteration:
            self.iteration = max(self.iteration, ev.iteration)
        if ev.name == "web_search":
            self.searches += 1
        elif ev.name == "web_fetch":
            self.pages += 1
        else:
            self.other_tools += 1
        text = describe_start(ev.name, ev.arguments, self.lang)
        self._pending[ev.tool_call_id] = text
        return text

    def complete(self, ev: ToolEvent) -> tuple[str, bool] | None:
        """(start_text, ok) for a finished call, or None for an unpaired
        complete (e.g. the TUI runner's synthetic message-tool event, which
        never had a START)."""
        started = self._pending.pop(ev.tool_call_id, None)
        if started is None:
            return None
        return started, not looks_failed(ev.result_preview)

    def status_line(self) -> str:
        if not (self.iteration or self.searches or self.pages or self.other_tools):
            return _label(self.lang, "thinking_idle")
        parts = [_label(self.lang, "thinking")]
        if self.iteration and self.max_iterations:
            parts.append(_label(self.lang, "round").format(self.iteration, self.max_iterations))
        counts = [
            _count(self.lang, key, n)
            for n, key in ((self.searches, "searches"), (self.pages, "pages"), (self.other_tools, "tools"))
            if n
        ]
        if counts:
            parts.append(" ".join(counts))
        return " · ".join(parts)


class ProgressRenderer:
    """Terminal renderer behind the CliOutlet's render callbacks.

    Every entry point swallows its own exceptions: a raising deliver() would
    put the hub worker into its retry-with-backoff path (delivery.py) for a
    purely cosmetic line. Callbacks must also never block — the hub queue is
    bounded and backpressure reaches the agent loop's emit.
    """

    def __init__(
        self,
        console: Console,
        *,
        mode: str,
        spinner: bool,
        max_iterations: int = 0,
    ) -> None:
        self._console = console
        self.mode = mode
        self._spinner = spinner
        self.progress = TurnProgress(max_iterations=max_iterations)
        self._status: Any = None
        # The active spinner regardless of mode, tracked for suspended() only.
        # Kept apart from ``_status``: on_tool branches on ``_status is None``
        # to decide between updating the live line and printing plain lines,
        # and the plain-lines mode must keep printing.
        self._active: Any = None

    def thinking_ctx(self, question: str = ""):
        """Per-turn context manager. Entering is the turn-boundary signal that
        resets the counters (outlets get no lifecycle events) and fixes the
        turn's display language; in live mode it also owns the status line."""
        self.progress.reset(turn_language(question))
        if not self._spinner:
            return nullcontext()
        if self.mode == "live":
            return self._live_status()
        return self._plain_status()

    @contextmanager
    def _live_status(self):
        status = self._console.status(Text(self.progress.status_line(), style="dim"), spinner="dots")
        with status:
            self._status = status
            self._active = status
            try:
                yield
            finally:
                self._status = None
                self._active = None

    @contextmanager
    def _plain_status(self):
        status = self._console.status(Text(self.progress.status_line(), style="dim"), spinner="dots")
        with status:
            self._active = status
            try:
                yield
            finally:
                self._active = None

    @contextmanager
    def suspended(self):
        """Stop the spinner while something else owns the terminal.

        Exists for the ask_user prompt: a rich status animates on the same fd
        prompt_toolkit is about to draw on, and the two interleave into a
        garbled line. Exception-swallowing like every other entry point here --
        a broken spinner must not take the question down with it.
        """
        status = self._active
        if status is not None:
            try:
                status.stop()
            except Exception:
                status = None
        try:
            yield
        finally:
            if status is not None:
                try:
                    status.start()
                except Exception:
                    pass

    def on_notice(self, text: str) -> None:
        """Model narration / tool hints. DR models write paragraphs between
        tool calls; clip to the first line so they don't drown the progress."""
        try:
            stripped = text.strip()
            if not stripped:
                return
            self._print(f"↳ {_clip(stripped.splitlines()[0], _NOTICE_CLIP)}")
        except Exception:
            pass

    def on_tool(self, ev: ToolEvent) -> None:
        if self.mode == "off":
            return
        try:
            if ev.phase is ToolPhase.START:
                started = self.progress.start(ev)
                if self._status is not None:
                    self._status.update(Text(f"{started} · {self.progress.status_line()}", style="dim"))
                else:
                    self._print(f"↳ {started}")
            else:
                # A successful call prints nothing beyond its START line;
                # only failure gets a completion echo.
                pair = self.progress.complete(ev)
                if pair is None:
                    return
                started, ok = pair
                lang = self.progress.lang
                if self._status is not None:
                    self._print(f"↳ {started}" if ok else f"↳ {started} {_label(lang, 'failed_inline')}")
                    self._status.update(Text(self.progress.status_line(), style="dim"))
                elif not ok:
                    self._print(f"    {_label(lang, 'failed_block')}")
        except Exception:
            pass

    def _print(self, line: str) -> None:
        self._console.print(Text(f"  {line}", style="dim"))
