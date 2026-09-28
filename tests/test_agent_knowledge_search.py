"""The tool that searches what a conversation was pointed at.

Two halves under test. The scope: which bases a turn may reach, which is the
reader's choice and never the model's. And the answer: what comes back, in
words a person could check.
"""

from __future__ import annotations

from typing import Any

import pytest

from raven.agent import knowledge_scope
from raven.agent.tools.knowledge import DEFAULT_LIMIT, MAX_CHARS, MAX_LIMIT, KnowledgeSearchTool

pytestmark = pytest.mark.asyncio


class _Chunk:
    def __init__(self, text: str, page: int | None = None) -> None:
        self.text = text
        self.metadata = {"page_number": page} if page is not None else {}


class _Hit:
    def __init__(self, document_id: str, text: str, page: int | None = None, score: float = 0.9) -> None:
        self.document_id = document_id
        self.chunk = _Chunk(text, page)
        self.chunk_id = f"c-{document_id}"
        self.score = score
        self.retrieval = "vector"


class _Found:
    def __init__(self, hits: list[_Hit], by_keyword: dict[str, str] | None = None) -> None:
        self.hits = hits
        self.by_keyword = by_keyword or {}


class _Record:
    def __init__(self, source: str) -> None:
        self.source = source


class _Library:
    """A manager that answers from memory and records what it was asked."""

    def __init__(self, hits: list[_Hit] | None = None, by_keyword: dict[str, str] | None = None) -> None:
        self._hits = hits or []
        self._by_keyword = by_keyword or {}
        self.asked: list[tuple[list[str], str, int | None]] = []
        self.raises: Exception | None = None

    async def search(self, base_ids: list[str], query: str, top_k: int | None = None) -> _Found:
        self.asked.append((list(base_ids), query, top_k))
        if self.raises is not None:
            raise self.raises
        return _Found(self._hits, self._by_keyword)

    def get_document(self, document_id: str) -> Any:
        return _Record(f"{document_id}.pdf")


async def test_the_tool_searches_what_the_session_was_pointed_at() -> None:
    """The bases come from the turn's binding, not from an argument: a model
    that could name a base could read material nobody offered it."""
    library = _Library([_Hit("d1", "the answer", page=4)])
    tool = KnowledgeSearchTool(library)

    with knowledge_scope.bind(("kb-a", "kb-b")):
        out = await tool.execute(query="what is the rota")

    assert library.asked == [(["kb-a", "kb-b"], "what is the rota", DEFAULT_LIMIT)]
    assert "the answer" in out


async def test_a_base_cannot_be_named_by_the_caller() -> None:
    """There is no parameter for it, and passing one changes nothing."""
    library = _Library([_Hit("d1", "the answer")])
    tool = KnowledgeSearchTool(library)

    assert "base" not in str(tool.parameters).lower()

    with knowledge_scope.bind(("kb-a",)):
        await tool.execute(query="q", base_ids=["kb-secret"], knowledge_bases=["kb-secret"])

    assert library.asked[0][0] == ["kb-a"]


async def test_a_hit_says_which_document_and_page_it_came_from() -> None:
    """A hit a reader cannot place is a hit they cannot check."""
    library = _Library([_Hit("handbook", "backups run nightly", page=12)])
    tool = KnowledgeSearchTool(library)

    with knowledge_scope.bind(("kb-a",)):
        out = await tool.execute(query="backups")

    assert "handbook.pdf" in out
    assert "page 12" in out


async def test_a_format_with_no_pages_is_named_without_one() -> None:
    """Absent means the format has none, never page zero."""
    library = _Library([_Hit("notes", "a line")])
    tool = KnowledgeSearchTool(library)

    with knowledge_scope.bind(("kb-a",)):
        out = await tool.execute(query="anything")

    assert "notes.pdf" in out
    assert "page" not in out


async def test_nothing_selected_is_said_rather_than_searched() -> None:
    """Withheld from the schema, but a plugin or a subagent can still call it."""
    library = _Library([_Hit("d1", "unreachable")])
    tool = KnowledgeSearchTool(library)

    out = await tool.execute(query="anything")

    assert library.asked == []
    assert "no knowledge bases" in out.lower()


async def test_an_empty_query_is_refused_before_the_engine_is_touched() -> None:
    library = _Library()
    tool = KnowledgeSearchTool(library)

    with knowledge_scope.bind(("kb-a",)):
        out = await tool.execute(query="   ")

    assert library.asked == []
    assert "needs a query" in out


async def test_the_limit_is_bounded_at_both_ends() -> None:
    """A model asking for a hundred pieces is asking for the whole base, and
    the answer would fill the context it was trying to save."""
    library = _Library()
    tool = KnowledgeSearchTool(library)

    with knowledge_scope.bind(("kb-a",)):
        await tool.execute(query="q", limit=500)
        await tool.execute(query="q", limit=0)
        await tool.execute(query="q", limit="nine")

    assert [asked[2] for asked in library.asked] == [MAX_LIMIT, 1, DEFAULT_LIMIT]


async def test_a_long_passage_is_cut_rather_than_sent_whole() -> None:
    """A base cut at eight thousand tokens has pieces that would swallow a turn."""
    library = _Library([_Hit("d1", "x" * (MAX_CHARS + 500))])
    tool = KnowledgeSearchTool(library)

    with knowledge_scope.bind(("kb-a",)):
        out = await tool.execute(query="q")

    assert len(out) < MAX_CHARS + 200
    assert out.rstrip().endswith("...")


async def test_finding_nothing_says_so_in_the_words_that_were_asked() -> None:
    library = _Library([])
    tool = KnowledgeSearchTool(library)

    with knowledge_scope.bind(("kb-a",)):
        out = await tool.execute(query="the rota")

    assert "'the rota'" in out


async def test_a_keyword_fallback_is_reported_after_the_hits_not_before() -> None:
    """Those hits are in the list, so it is not an error and does not lead."""
    library = _Library([_Hit("d1", "found by words")], by_keyword={"kb-a": "no endpoint"})
    tool = KnowledgeSearchTool(library)

    with knowledge_scope.bind(("kb-a",)):
        out = await tool.execute(query="q")

    assert out.index("found by words") < out.index("matching words")


async def test_a_search_that_fails_is_told_rather_than_raised() -> None:
    """The turn goes on: one unreachable base is not the end of the answer."""
    library = _Library()
    library.raises = RuntimeError("the collection is gone")
    tool = KnowledgeSearchTool(library)

    with knowledge_scope.bind(("kb-a",)):
        out = await tool.execute(query="q")

    assert "the collection is gone" in out


# ---------------------------------------------------------------------------
# the scope itself
# ---------------------------------------------------------------------------


class _Sessions:
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        self._records: dict[str, Any] = {}
        self._seed = metadata or {}

    def get_or_create(self, key: str) -> Any:
        if key not in self._records:
            self._records[key] = type("_S", (), {"metadata": dict(self._seed)})()
        return self._records[key]


async def test_the_selection_is_read_off_the_session() -> None:
    sessions = _Sessions({knowledge_scope.METADATA_KEY: ["kb-a", "kb-b"]})

    assert knowledge_scope.read(sessions, "tui:1") == ("kb-a", "kb-b")


async def test_a_session_that_chose_nothing_reaches_nothing() -> None:
    assert knowledge_scope.read(_Sessions(), "tui:1") == ()
    assert knowledge_scope.read(None, "tui:1") == ()
    assert knowledge_scope.read(_Sessions(), "") == ()


async def test_a_selection_keeps_its_order_and_drops_its_repeats() -> None:
    """The order is the only thing saying which base the reader reached for
    first, and a list is not a set."""
    sessions = _Sessions()

    written = knowledge_scope.write(sessions, "tui:1", ["kb-b", "kb-a", "kb-b", "", "  "])

    assert written == ("kb-b", "kb-a")
    assert knowledge_scope.read(sessions, "tui:1") == ("kb-b", "kb-a")


async def test_writing_replaces_rather_than_adds() -> None:
    """The picker sends what is ticked, and a call that added would have no way
    to say that something was unticked."""
    sessions = _Sessions()
    knowledge_scope.write(sessions, "tui:1", ["kb-a", "kb-b"])

    assert knowledge_scope.write(sessions, "tui:1", ["kb-c"]) == ("kb-c",)


async def test_a_binding_does_not_outlive_its_block() -> None:
    """One loop serves every session on the process."""
    with knowledge_scope.bind(("kb-a",)):
        assert knowledge_scope.selected() == ("kb-a",)

    assert knowledge_scope.selected() == ()


async def test_a_session_record_that_cannot_be_read_selects_nothing() -> None:
    class _Broken:
        def get_or_create(self, key: str) -> Any:
            raise OSError("the session file is gone")

    assert knowledge_scope.read(_Broken(), "tui:1") == ()


# ---------------------------------------------------------------------------
# the seam: what a loop offers, and what a turn binds
# ---------------------------------------------------------------------------


class _StubProvider:
    """Construction reads the default model; no turn is run here."""

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs: Any) -> Any:  # pragma: no cover - never invoked
        raise NotImplementedError


def _offered(loop: Any) -> set[str]:
    return {d["function"]["name"] for d in loop.tools.get_definitions()}


async def test_the_tool_is_withheld_where_nothing_was_selected(tmp_path: Any) -> None:
    """A conversation with no bases is the ordinary case, and a tool that is
    always offered and always answers "you have not selected anything" spends a
    slot in every schema to say so."""
    from raven.agent.loop.main import AgentLoop
    from tests._wiring import wire

    loop = AgentLoop(provider=_StubProvider(), workspace=tmp_path / "ws", **wire())

    assert "knowledge_search" not in _offered(loop)


async def test_the_tool_is_offered_once_a_base_is_attached(tmp_path: Any) -> None:
    """Both directions, mid-conversation: the withholding is read from the
    turn's binding rather than from config, so attaching a base takes effect on
    the next model call."""
    from raven.agent.loop.main import AgentLoop
    from tests._wiring import wire

    loop = AgentLoop(provider=_StubProvider(), workspace=tmp_path / "ws", **wire())

    with knowledge_scope.bind(("kb-a",)):
        assert "knowledge_search" in _offered(loop)

    assert "knowledge_search" not in _offered(loop)
