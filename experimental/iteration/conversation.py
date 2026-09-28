"""A trial that is one conversation: a conversant speaks, the worker answers, until the conversant stops."""

from typing import Protocol
from uuid import uuid4

from .protocols import Exchange, Sessions


class Conversant(Protocol):
    """Whoever talks to the worker; None ends the conversation."""

    name: str

    async def speak(self, exchanges: list[Exchange]) -> str | None: ...


class Conversation:
    def __init__(self, conversant: Conversant, *, max_turns: int = 6):
        if not isinstance(max_turns, int) or max_turns < 1:
            raise ValueError("a conversation needs a positive turn budget")
        self.conversant, self.max_turns = conversant, max_turns

    async def run(self, worker) -> Sessions:
        """Each run is a fresh worker session, so rounds do not continue one another's history."""
        exchanges, session_key = [], f"curator:{self.conversant.name}:{uuid4().hex}"
        for _ in range(self.max_turns):
            text = await self.conversant.speak(exchanges)
            if text is None:
                break
            execution = await worker.run(text, session_key=session_key)
            exchanges.append(Exchange(text, execution))
        return {self.conversant.name: exchanges}
