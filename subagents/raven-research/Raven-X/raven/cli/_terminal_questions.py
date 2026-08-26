"""ask_user on the interactive REPL: the prompt itself is the round trip.

The gateway and the TUI need a :class:`QuestionBroker` because their answer
arrives on another transport and must be routed back by conversation_id; a
terminal has the user on the same fd, so ``await_question`` just renders the
question, suspends the turn spinner, and reads a line. Duck-typed to the
broker's surface (``await_question`` / ``cancel_all``) -- ``AskUserTool`` and
the DR gate's ``round_trip_ready`` neither know nor care that nothing is ever
pending across frames here.

Interactive REPL only. The ``-m`` one-shot never wires this: headless is a
design constraint on that entry point (it is the batch/measurement launcher,
and a batch must not block on a human), so the DR gate falls back to the
handoff there.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import Any

from loguru import logger
from rich.text import Text


async def _prompt_toolkit_read(prompt: str) -> str:
    # A dedicated session, not the REPL's global one: the main prompt carries
    # multiline/paste handling and file history, and an answer to a mid-turn
    # question belongs in neither. patch_stdout(raw=True) for the same reason
    # the main prompt uses it -- background renderers keep writing.
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    with patch_stdout(raw=True):
        return await PromptSession().prompt_async(prompt)


class TerminalQuestionBroker:
    """Answers ``await_question`` by prompting on the controlling terminal."""

    def __init__(
        self,
        console: Any,
        *,
        suspend: Callable[[], Any] | None = None,
        read_line: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._console = console
        self._suspend = suspend
        self._read_line = read_line or _prompt_toolkit_read

    async def await_question(
        self,
        conversation_id: str,
        *,
        prompt: str,
        choices: list[str] | None = None,
        default: str = "",
        timeout_s: float = 600.0,
        **_: Any,
    ) -> str:
        """Render the question, read the answer. Returns ``default``, never raises.

        ``timeout_s`` is accepted for signature parity and deliberately unused:
        a REPL is attended, and a prompt that resolves itself mid-typing is
        worse than one that waits.

        A bare number selects the matching choice; anything else is the answer
        verbatim; empty input falls back to ``default`` -- the tool then tells
        the model the user did not answer and to proceed on its own judgment.
        Ctrl-C / Ctrl-D on the question skip the QUESTION, not the turn.
        """
        options = [str(c) for c in (choices or [])]
        try:
            with (self._suspend() if self._suspend is not None else nullcontext()):
                self._render(prompt, options)
                raw = (await self._read_line("  > ")).strip()
        except (EOFError, KeyboardInterrupt):
            return default
        except Exception:
            logger.exception("terminal ask_user prompt failed; answering the default")
            return default
        if not raw:
            return default
        if options and raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        return raw

    def _render(self, prompt: str, options: list[str]) -> None:
        self._console.print()
        self._console.print(Text(f"? {prompt}", style="bold"))
        for i, option in enumerate(options, start=1):
            self._console.print(Text(f"  {i}. {option}", style="dim"))

    def cancel_all(self) -> None:
        """Broker-surface parity; nothing is ever pending across frames."""


__all__ = ["TerminalQuestionBroker"]
