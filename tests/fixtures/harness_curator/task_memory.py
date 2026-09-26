"""Task memory example retaining deduplicated execution evidence."""

from pydantic import BaseModel, ConfigDict

from experimental.curator.harness.strategies import MemoryStrategy


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: str
    value: str


class Context(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[str]


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stored: bool


class Memory(MemoryStrategy[Query, Context, Evidence, Receipt]):
    def __init__(self, state):
        self.state = state
        state.setdefault("facts", {})

    async def recall(self, query: Query) -> Context:
        return Context(facts=[value for value in self.state["facts"].values() if not query.text or query.text in value])

    async def retain(self, record: Evidence) -> Receipt:
        if record.call_id in self.state["facts"]:
            return Receipt(stored=False)
        self.state["facts"][record.call_id] = record.value
        return Receipt(stored=True)


def create(state, task):
    return Memory(state)
