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
    """Whether this tool result should be shown to the viewer as a failure.

    ★ 20260828 (Framework, product-surface audit). This used to re-spell the
    predicate instead of borrowing it, and dropped two of the three prefixes
    ``WebSearchTool._failed`` enumerates one file over: a zero-hit search
    (``No results for: ...``) and a proxy fault (``Proxy error: ...``) both
    rendered as a normal, successful ``🔍 搜索 "..."`` line. During a live run
    that is the difference between "the agent is making progress" and "the
    agent has been getting nothing back for two minutes" — and the viewer had
    no way to tell. The saturation refusal (``[search closed ...]``) was a
    third miss: the harness had switched search off and the screen said the
    search succeeded.

    So the search verdict is now *borrowed*, not restated. Lazy import with a
    fallback, the same shape ``turn_language`` above uses and for the same
    reason: this module must keep rendering if the agent package cannot be
    imported. The fallback is the union of the three prefixes rather than the
    old two, so even the degraded path no longer under-reports.

    ``_failed`` is documented as behaviour-bearing (it decides what enters the
    replay cache). Borrowing it here couples display to that decision on
    purpose: "the model got nothing usable back" and "there is nothing worth
    replaying" are the same question, and this repo's most-repeated failure is
    two spellings of one predicate drifting apart with no runtime symptom.

    The registry-wide convention stays in front of it: every failed tool's
    model-facing text starts with ``Error`` (``registry.py``'s ``Error
    executing <tool>: ...``, the file and shell tools' ``Error running ...``),
    and ``_failed`` is a search-result classifier that only knows ``Error:``
    with a colon. Borrowing it alone rendered those as successful calls.
    """
    p = preview.lstrip()
    if p.startswith(('{"error"', "Error")):
        # web_fetch's envelope, and the registry-wide failure prefix; neither
        # is a search string, so the search predicate below never sees them.
        return True
    try:
        from raven.agent.harness_text import SEARCH_CLOSED_PREFIX
        from raven.agent.tools.web import WebSearchTool

        return p.startswith(SEARCH_CLOSED_PREFIX) or bool(WebSearchTool._failed(p))
    except Exception:
        # Hardcoded, not the imported name: this branch runs precisely when that
        # import failed, so the name may not be bound here.
        return p.startswith(("Proxy error:", "No results for:", "Search is closed for this task:"))


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
        # The active spinner, for suspended() only. It used to be tracked apart
        # from ``_status`` because ``on_tool`` branched on ``_status is None`` to
        # choose between updating the live line and printing plain lines, so the
        # plain-lines mode could not hold a handle without switching styles. That
        # coupling is gone (20260828): the print style is keyed on the MODE, and
        # both modes now store the handle, so these two are set and cleared
        # together by ``_status_ctx``. Kept as a separate name because suspend is
        # a different question from "is there a line to refresh" - a future mode
        # may hold one without the other - and because the two entry points below
        # read them for different reasons.
        self._active: Any = None

    def thinking_ctx(self, question: str = ""):
        """Per-turn context manager. Entering is the turn-boundary signal that
        resets the counters (outlets get no lifecycle events) and fixes the
        turn's display language, and it owns the status line.

        ★ 20260828 (Framework, product-surface audit). Both modes now keep the
        handle. They always both CREATED a ``console.status`` — the Live was
        never the difference between them — but only the live branch stored it,
        so ``self._status`` was ``None`` under the default ``lines`` mode and
        every ``.update()`` below was dead code. The consequence was that
        ``status_line()`` got evaluated exactly once, at entry, with every
        counter still at zero: the round/search/page tally built in
        ``TurnProgress.status_line`` was never rendered at all, and the spinner
        read a frozen "Raven is thinking..." for the whole run, including the
        final generation + review stretch where no tool event fires and the
        line is the only thing moving.

        What stays keyed on the MODE, not on the handle, is the *print* style:
        ``lines`` announces a call when it starts, ``live`` announces it when it
        finishes so it can mark failure on the same line. Those were entangled
        with handle-presence, which is why storing the handle used to be
        impossible without silently switching modes.
        """
        self.progress.reset(turn_language(question))
        if not self._spinner:
            return nullcontext()
        return self._status_ctx()

    @contextmanager
    def _status_ctx(self):
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

    def _update_status(self, text: str) -> None:
        """Refresh the spinner line. No-op when no status is live (``--no-spinner``,
        or a caller that never entered ``thinking_ctx``)."""
        if self._status is not None:
            self._status.update(Text(text, style="dim"))

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
            # Both conditions, and the second is not redundant. ToolEvents can
            # trail the thinking context (the render barrier is ``wait_idle``),
            # and outside it there is no status line to print onto - live mode
            # must then degrade to the lines behaviour or the call is never
            # shown at all. The pre-20260828 code got this for free by keying
            # the print style on ``_status`` alone; splitting the two jobs
            # apart means saying so.
            live = self.mode == "live" and self._status is not None
            if ev.phase is ToolPhase.START:
                started = self.progress.start(ev)
                if live:
                    self._update_status(f"{started} · {self.progress.status_line()}")
                else:
                    self._print(f"↳ {started}")
                    self._update_status(self.progress.status_line())
            else:
                # A successful call prints nothing beyond its START line;
                # only failure gets a completion echo.
                pair = self.progress.complete(ev)
                if pair is None:
                    return
                started, ok = pair
                lang = self.progress.lang
                if live:
                    self._print(f"↳ {started}" if ok else f"↳ {started} {_label(lang, 'failed_inline')}")
                elif not ok:
                    self._print(f"    {_label(lang, 'failed_block')}")
                self._update_status(self.progress.status_line())
        except Exception:
            pass

    def _print(self, line: str) -> None:
        self._console.print(Text(f"  {line}", style="dim"))
