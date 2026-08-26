"""The REPL's ask_user round trip: a prompt on the controlling terminal."""

import asyncio
import io
from contextlib import contextmanager

from rich.console import Console

from raven.cli._terminal_questions import TerminalQuestionBroker


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=80)


def _broker(answers: list[str], events: list[str] | None = None) -> TerminalQuestionBroker:
    events = events if events is not None else []

    @contextmanager
    def suspend():
        events.append("suspend")
        try:
            yield
        finally:
            events.append("resume")

    async def read_line(_prompt: str) -> str:
        events.append("read")
        return answers.pop(0)

    return TerminalQuestionBroker(_console(), suspend=suspend, read_line=read_line)


def _ask(broker: TerminalQuestionBroker, **kw) -> str:
    return asyncio.run(broker.await_question("cli:direct", **kw))


def test_a_number_selects_the_choice_and_text_is_verbatim() -> None:
    broker = _broker(["2", "the EU market", "0", "99"])
    choices = ["2023", "2024"]
    assert _ask(broker, prompt="which year?", choices=choices) == "2024"
    assert _ask(broker, prompt="scope?", choices=choices) == "the EU market"
    # Out-of-range numbers are answers, not selections.
    assert _ask(broker, prompt="q", choices=choices) == "0"
    assert _ask(broker, prompt="q", choices=choices) == "99"


def test_empty_input_falls_back_to_the_default() -> None:
    broker = _broker(["", "   "])
    assert _ask(broker, prompt="q", default="") == ""
    assert _ask(broker, prompt="q", default="proceed") == "proceed"


def test_the_spinner_is_suspended_around_the_prompt() -> None:
    events: list[str] = []
    broker = _broker(["ok"], events)
    assert _ask(broker, prompt="q") == "ok"
    assert events == ["suspend", "read", "resume"]


def test_interrupts_skip_the_question_not_the_turn() -> None:
    """Ctrl-C / Ctrl-D on the question answer the default: the tool then tells
    the model the user did not answer, and the turn continues."""
    for exc in (KeyboardInterrupt, EOFError, RuntimeError("prompt broke")):

        async def read_line(_prompt: str, _exc=exc) -> str:
            raise _exc if isinstance(_exc, Exception) else _exc()

        broker = TerminalQuestionBroker(_console(), read_line=read_line)
        assert _ask(broker, prompt="q", default="proceed") == "proceed"


def test_the_question_and_choices_are_rendered() -> None:
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=80)

    async def read_line(_prompt: str) -> str:
        return "1"

    broker = TerminalQuestionBroker(console, read_line=read_line)
    asyncio.run(broker.await_question("cli:direct", prompt="which year?", choices=["2023", "2024"]))
    rendered = out.getvalue()
    assert "which year?" in rendered
    assert "1. 2023" in rendered and "2. 2024" in rendered


def test_the_broker_surface_matches_what_the_tool_calls() -> None:
    """``AskUserTool.execute`` calls ``await_question(cid, prompt=..., choices=[...])``
    and the shutdown paths call ``cancel_all()``; both must exist here or the
    duck-typing breaks at runtime, not at import."""
    broker = _broker(["x"])
    assert asyncio.run(broker.await_question("cid", prompt="q", choices=[])) == "x"
    broker.cancel_all()
