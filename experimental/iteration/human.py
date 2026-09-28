"""A person at the terminal as the conversant of a conversation trial and as the round's evaluator."""

import asyncio

from .protocols import Signal

DONE = "/done"


class Human:
    name = "user"

    def __init__(self, ask=input, show=print):
        self.ask, self.show = ask, show

    async def _prompt(self, label):
        try:
            return await asyncio.to_thread(self.ask, label)
        except EOFError:
            return None

    async def speak(self, exchanges):
        if exchanges:
            self.show(exchanges[-1].assistant)
            for error in exchanges[-1].execution.errors:
                self.show(f"Execution issue: {error.get('error', error['kind'])}")
        while True:
            text = await self._prompt("> ")
            if text is None or text.strip() == DONE:
                return None
            if text.strip():
                return text

    async def evaluate(self, sessions):
        text = await self._prompt("feedback> ")
        if not text or not text.strip():
            return None
        return Signal("human", text.strip())
