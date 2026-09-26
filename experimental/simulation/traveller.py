"""The traveller: a model plays one persona, reads the conversation so far, then says the next thing or leaves.

In a cultivation round each persona is one of the agency's drill cards, played as the customer it describes.
"""

import asyncio
import random
import re
from pathlib import Path

from ..iteration.exchange import ExchangeError, messages
from .channel import said
from .files import read_delivered
from .scenario import Persona

LEAVE = "LEAVE"
TRAILING = re.compile(r"\s+\W*LEAVE\W*$")
OPENED_LIMIT = 8_000
_PROMPT = Path(__file__).resolve().parent / "prompts" / "traveller.md"


def transcript(exchanges, speaker: str = "traveller") -> list[dict]:
    """What the speaker saw: each message, and the names of any files handed over with it."""
    rows = []
    for exchange in exchanges:
        row = {speaker: exchange.user, "assistant": said(exchange)}
        if exchange.execution.deliverables:
            row["delivered"] = [Path(path).name for path in exchange.execution.deliverables]
        rows.append(row)
    return rows


def opened(exchanges) -> dict[str, str]:
    """What a person sees on opening each file handed over so far, latest version of each."""
    files = {}
    for exchange in exchanges:
        for path in exchange.execution.deliverables:
            if Path(path).is_file():
                files[Path(path).name] = read_delivered(path)[:OPENED_LIMIT]
    return files


def parting(text: str) -> tuple[str | None, bool]:
    """(message, leaving): the word LEAVE on a line of its own, or closing the last line, marks leaving and is never
    sent; what else was written is the parting message, or None when nothing was."""
    lines = (text or "").strip().splitlines()
    kept = [line for line in lines if line.strip(" .!*_\"'").upper() != LEAVE]
    closing = bool(kept) and bool(TRAILING.search(kept[-1]))
    if closing:
        kept[-1] = TRAILING.sub("", kept[-1])
    message = "\n".join(kept).strip() or None
    return message, closing or len(kept) < len(lines) or message is None


async def say(provider, prompt: Path, packet, *, label: str, model=None, effort=None, timeout=120) -> str:
    """One plain-text reply from a role-playing model, as written (see `parting` for how it says it is done).

    `effort` is the reasoning effort of this call; without one the provider's own setting applies.
    """
    extra = {"reasoning_effort": effort} if effort else {}
    try:
        async with asyncio.timeout(timeout):
            response = await provider.chat_with_retry(
                messages=messages(prompt.read_text(), packet), model=model, **extra
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ExchangeError(f"{label}: model call failed: {exc!r}") from exc
    if response.finish_reason == "error":
        raise ExchangeError(f"{label}: provider error: {response.content}")
    return (response.content or "").strip()


class Traveller:
    """Speaks in plain text, the way a person types into a chat; the word LEAVE on a line of its own ends the
    conversation, after anything else written with it has been sent as a parting message.

    Each conversation plays the card afresh: its values are drawn from `seed`, the traveller's name and how many
    conversations it has had, so every round meets new dates, party, budget and phone, and two runs with one seed
    meet the same ones. `card` is the playing of the current conversation.
    """

    def __init__(self, persona: Persona, provider, *, name=None, model=None, effort=None, timeout=120, seed=0):
        self.persona, self.provider, self.model, self.timeout = persona, provider, model, timeout
        self.effort, self.seed, self.played, self.card = effort, seed, 0, None
        self.leaving = False
        self.name = name or persona.name

    async def speak(self, exchanges) -> str | None:
        if not exchanges or self.card is None:
            self.card = self.persona.play(random.Random(f"{self.seed}:{self.name}:{self.played}"))
            self.played += 1
            self.leaving = False
        if self.leaving:
            return None
        packet = {"persona": self.card.text, "conversation": transcript(exchanges), "opened": opened(exchanges)}
        text = await say(
            self.provider,
            _PROMPT,
            packet,
            label=f"traveller:{self.name}",
            model=self.model,
            effort=self.effort,
            timeout=self.timeout,
        )
        message, self.leaving = parting(text)
        return message
