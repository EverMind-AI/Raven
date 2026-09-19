"""``knowledge.*`` -- what the page is told about the bases on disk."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from raven.rpc.errors import ConfigValidationError, InternalError
from raven.rpc.methods import knowledge as kb

pytestmark = pytest.mark.asyncio


class _FakeBase:
    def __init__(self, base_id: str, name: str) -> None:
        self.id = base_id
        self.name = name
        self.description = "notes"
        self.embedding_model = "bge-m3"
        self.embedding_provider = "siliconflow"
        self.dimensions = 1024
        self.created_at = "2026-08-24T00:00:00"
        self.updated_at = "2026-08-24T00:01:00"


class _FakeManager:
    """Only the two calls the list handler makes."""

    def __init__(self, bases: list[_FakeBase], docs: dict[str, int]) -> None:
        self._bases = bases
        self._docs = docs
        self.asked: list[str] = []
        self.create_raises: Exception | None = None
        self.switch_raises: Exception | None = None
        self.switched: tuple[str, str] | None = None

    def list_bases(self) -> list[_FakeBase]:
        return list(self._bases)

    def embedding_reach(self, base: object) -> str:
        """Reachable unless a test says otherwise; the rule itself is the
        engine's, and is tested there."""
        return str(getattr(base, "embedding_reach", "") or "")

    def list_documents(self, base_id: str) -> list[object]:
        self.asked.append(base_id)
        return [object()] * self._docs.get(base_id, 0)

    def get_base(self, base_id: str):
        return next((b for b in self._bases if b.id == base_id), None)

    async def create_base(
        self,
        *,
        name: str,
        description: str = "",
        embedding: bool = True,
        embedding_model: str = "",
        embedding_provider: str = "",
    ):
        if self.create_raises is not None:
            raise self.create_raises
        made = _FakeBase(f"b{len(self._bases) + 1}", name)
        made.description = description
        if embedding_model:
            made.embedding_model = embedding_model
            made.embedding_provider = embedding_provider
        # An empty model is how a base with no vectors is recorded, which is
        # what every reader tests for.
        if not embedding:
            made.embedding_model = ""
            made.embedding_provider = ""
            made.dimensions = 0
        self.created_with_embedding = embedding
        self._bases.append(made)
        return made

    async def switch_embedding(self, base_id: str, *, model: str, provider: str = ""):
        if self.switch_raises is not None:
            raise self.switch_raises
        base = self.get_base(base_id)
        if base is None:
            return None
        self.switched = (model, provider)
        base.embedding_model = model
        base.embedding_provider = provider if model else ""
        base.dimensions = 1024 if model else 0
        return base

    def rename_base(self, base_id: str, *, name=None, description=None):
        base = self.get_base(base_id)
        if base is None:
            return None
        if name is not None:
            base.name = name
        if description is not None:
            base.description = description
        return base

    async def delete_base(self, base_id: str) -> bool:
        base = self.get_base(base_id)
        if base is None:
            return False
        self._bases.remove(base)
        return True


@pytest.fixture(autouse=True)
def _no_leak():
    """A manager set for one test must not answer the next one."""
    yield
    kb._set_manager_for_tests(None)


async def test_status_reports_the_model_when_embedding_is_configured(monkeypatch) -> None:
    class _Config:
        model = "bge-m3"
        provider = "siliconflow"

    monkeypatch.setattr("raven.knowledge.load_embedding_config", lambda: _Config())

    out = await kb.knowledge_status({})

    assert (out["configured"], out["model"]) == (True, "bge-m3")
    # The provider travels with it: the page preselects the pair in the picker
    # a base is created from, and a model id alone cannot be selected with.
    assert out["provider"] == "siliconflow"
    # Reported so a surface walking a folder can filter by what this build can
    # actually index, rather than by a list written down beside it.
    assert ".md" in out["extensions"]


async def test_status_says_unconfigured_rather_than_failing(monkeypatch) -> None:
    """A deployment with no embedding endpoint is an ordinary state: the page
    has to open and say "configure this first", not fail to load."""
    monkeypatch.setattr("raven.knowledge.load_embedding_config", lambda: None)

    out = await kb.knowledge_status({})

    assert (out["configured"], out["model"]) == (False, "")
    # Still reported: what can be parsed does not depend on an embedding
    # endpoint, and a base made without one still takes documents.
    assert ".md" in out["extensions"]


async def test_bases_carry_their_document_count(monkeypatch) -> None:
    manager = _FakeManager([_FakeBase("b1", "handbook"), _FakeBase("b2", "specs")], {"b1": 3, "b2": 0})
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_bases_list({})

    assert [(b["id"], b["name"], b["documents"]) for b in out["bases"]] == [
        ("b1", "handbook", 3),
        ("b2", "specs", 0),
    ]
    # Counted per base, from the records already on disk beside them, rather
    # than left to a second call the list view would have to make per row.
    assert manager.asked == ["b1", "b2"]


async def test_a_base_reports_the_model_it_was_built_with(monkeypatch) -> None:
    """Not today's configured model: a base outlives a config change, and the
    page has to be able to show the mismatch rather than hide it."""
    manager = _FakeManager([_FakeBase("b1", "handbook")], {"b1": 1})
    kb._set_manager_for_tests(manager)

    (base,) = (await kb.knowledge_bases_list({}))["bases"]

    assert base["embedding_model"] == "bge-m3"
    assert base["dimensions"] == 1024


async def test_no_bases_is_an_empty_list_not_an_error() -> None:
    kb._set_manager_for_tests(_FakeManager([], {}))

    assert await kb.knowledge_bases_list({}) == {"bases": []}


async def test_the_manager_is_built_once_and_under_the_data_dir(monkeypatch, tmp_path: Path) -> None:
    """Lazily, so a deployment whose page never opens the tab grows no
    directory, and once, so two calls share a store."""
    kb._set_manager_for_tests(None)
    built: list[Any] = []

    class _Recorder:
        def __init__(self, root: Any) -> None:
            built.append(root)

    monkeypatch.setattr("raven.config.paths.get_runtime_subdir", lambda name: tmp_path / name)
    monkeypatch.setattr("raven.knowledge.KnowledgeManager", _Recorder)

    first, second = kb.knowledge_manager(), kb.knowledge_manager()

    assert first is second
    assert built == [tmp_path / "knowledge"]


# -- the writes ---------------------------------------------------------------


async def test_creating_a_base_answers_the_row_the_list_would_show() -> None:
    """The same shape as a list row, so the page can insert it without a
    refetch."""
    manager = _FakeManager([], {})
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_bases_create({"name": "handbook", "description": "ops"})

    assert out["base"]["name"] == "handbook"
    assert out["base"]["description"] == "ops"
    assert out["base"]["documents"] == 0
    assert set(out["base"]) == {
        "id",
        "name",
        "description",
        "embedding_model",
        "embedding_provider",
        "dimensions",
        "created_at",
        "updated_at",
        "documents",
        "top_k",
        "smart_chunking",
        "separator",
        "table_context_size",
        "image_context_size",
        "embedding_reach",
        "chunk_size",
        "chunk_overlap",
        "file_processing",
    }


async def test_creating_without_a_name_is_refused_before_the_endpoint_is_touched() -> None:
    manager = _FakeManager([], {})
    manager.create_raises = AssertionError("must not reach the engine")
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError):
        await kb.knowledge_bases_create({"name": "   "})


async def test_an_endpoint_that_refuses_is_reported_not_swallowed() -> None:
    """Creating measures the model's width, so it reaches the network and can
    fail. The page has to be told why rather than shown a base that is not
    there."""
    manager = _FakeManager([], {})
    manager.create_raises = RuntimeError("connection refused")
    kb._set_manager_for_tests(manager)

    with pytest.raises(InternalError, match="connection refused"):
        await kb.knowledge_bases_create({"name": "handbook"})


async def test_renaming_leaves_the_field_that_was_not_sent() -> None:
    manager = _FakeManager([_FakeBase("b1", "old")], {"b1": 0})
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_bases_rename({"base_id": "b1", "name": "new"})

    assert out["base"]["name"] == "new"
    assert out["base"]["description"] == "notes"


async def test_renaming_an_unknown_base_says_so() -> None:
    kb._set_manager_for_tests(_FakeManager([], {}))

    with pytest.raises(ConfigValidationError, match="no such base"):
        await kb.knowledge_bases_rename({"base_id": "ghost", "name": "x"})


async def test_deleting_twice_is_not_an_error() -> None:
    """A stale page deleting again reached the outcome it wanted."""
    kb._set_manager_for_tests(_FakeManager([_FakeBase("b1", "gone")], {"b1": 0}))

    assert await kb.knowledge_bases_delete({"base_id": "b1"}) == {"removed": True}
    assert await kb.knowledge_bases_delete({"base_id": "b1"}) == {"removed": False}


async def test_documents_are_listed_for_a_base_that_exists() -> None:
    class _Doc:
        def __init__(self) -> None:
            self.id = "d1"
            self.base_id = "b1"
            self.source = "handbook.md"
            self.media_type = "text/markdown"
            self.size = 12
            self.status = "ready"
            self.chunk_count = 3
            self.error = ""
            self.created_at = "2026-08-24T00:00:00"
            self.updated_at = "2026-08-24T00:00:01"

    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.list_documents = lambda base_id: [_Doc()]  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    (doc,) = (await kb.knowledge_documents_list({"base_id": "b1"}))["documents"]

    assert (doc["source"], doc["status"], doc["chunk_count"]) == ("handbook.md", "ready", 3)


async def test_listing_documents_of_an_unknown_base_says_so() -> None:
    """Rather than an empty list, which reads as "this base has no documents"."""
    kb._set_manager_for_tests(_FakeManager([], {}))

    with pytest.raises(ConfigValidationError, match="no such base"):
        await kb.knowledge_documents_list({"base_id": "ghost"})


# -- documents and search -----------------------------------------------------


class _FakeDoc:
    def __init__(
        self,
        doc_id: str = "d1",
        status: str = "pending",
        *,
        source: str = "handbook.md",
        origin: str = "file",
        origin_ref: str = "",
        warning: str = "",
    ) -> None:
        self.id = doc_id
        self.base_id = "b1"
        self.source = source
        self.media_type = "text/markdown"
        self.size = 5
        self.status = status
        self.chunk_count = 0
        self.error = ""
        self.created_at = "2026-08-24T00:00:00"
        self.updated_at = "2026-08-24T00:00:00"
        self.origin = origin
        self.origin_ref = origin_ref
        self.warning = warning


async def test_a_row_carries_what_the_parse_could_not_do(monkeypatch) -> None:
    """A warning is not an error and does not replace the status: the document
    is ready and searchable, and the line says which part of it is not in the
    index."""
    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.list_documents = lambda base_id: [  # type: ignore[assignment]
        _FakeDoc("d1", "ready", warning="2 of 5 pictures in this file could not be read: rate limited")
    ]
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_documents_list({"base_id": "b1"})

    row = out["documents"][0]
    assert row["status"] == "ready"
    assert row["error"] == ""
    assert "2 of 5 pictures" in row["warning"]


async def test_a_row_with_nothing_to_report_carries_an_empty_warning(monkeypatch) -> None:
    """Never absent: the page reads the field, and a gateway answering nothing
    for it would have every row look warned or none of them, depending on how
    the page spelled the check."""
    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.list_documents = lambda base_id: [_FakeDoc("d1", "ready")]  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_documents_list({"base_id": "b1"})

    assert out["documents"][0]["warning"] == ""


def _fence(monkeypatch, tmp_path: Path, *, restrict: bool = True) -> None:
    """Point the filesystem policy at tmp_path, as the real one points at the
    workspace."""

    class _Tools:
        restrict_to_workspace = restrict

    class _Cfg:
        workspace_path = tmp_path
        tools = _Tools()

    monkeypatch.setattr("raven.config.load_config", lambda: _Cfg())
    # `raven.rpc.files` binds `load_config` at import, so patching the module it
    # came from does not reach the copy `resolve_readable` calls.
    monkeypatch.setattr("raven.rpc.files.load_config", lambda: _Cfg())


async def test_adding_a_document_reads_it_through_the_workspace_fence(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "handbook.md").write_text("hello", encoding="utf-8")
    _fence(monkeypatch, tmp_path)

    took: list[tuple[str, str, bytes]] = []
    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.add_document = lambda base_id, *, filename, content: (  # type: ignore[assignment]
        took.append((base_id, filename, content)),
        _FakeDoc(),
    )[1]
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_documents_add({"base_id": "b1", "path": "uploads/handbook.md"})

    assert took == [("b1", "handbook.md", b"hello")]
    assert out["document"]["status"] == "pending"


async def test_a_path_outside_the_workspace_is_refused(monkeypatch, tmp_path: Path) -> None:
    """The whole reason the path is resolved rather than opened as given: a
    client that could name any absolute path could have the server read anything
    on the host into a base."""
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    (tmp_path / "uploads").mkdir()
    _fence(monkeypatch, tmp_path)

    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.add_document = lambda *a, **k: pytest.fail("must not reach the engine")  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError):
        await kb.knowledge_documents_add({"base_id": "b1", "path": str(outside)})


async def test_the_state_directory_is_refused_with_the_shipped_default(monkeypatch, tmp_path: Path) -> None:
    """`restrict_to_workspace` ships off, and with no allowed roots the path
    policy returns any absolute path unchanged. What keeps `serve.json` -- whose
    token mints session nonces -- out of a base is the state-directory fence, so
    that is what this pins, in the configuration users actually run."""
    home = tmp_path / "home"
    workspace = home / "workspace"
    (workspace / "uploads").mkdir(parents=True)
    (workspace / "uploads" / "handbook.md").write_text("hello", encoding="utf-8")
    (home / "serve.json").write_text('{"token": "sekrit"}', encoding="utf-8")
    monkeypatch.setenv("RAVEN_HOME", str(home))
    _fence(monkeypatch, workspace, restrict=False)

    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.add_document = lambda *a, **k: pytest.fail("must not reach the engine")  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError):
        await kb.knowledge_documents_add({"base_id": "b1", "path": str(home / "serve.json")})

    # The same call, on a path the fence does allow, still goes through -- a test
    # that only ever refuses would pass against a helper that refuses everything.
    took: list[tuple[str, str, bytes]] = []
    manager.add_document = lambda base_id, *, filename, content: (  # type: ignore[assignment]
        took.append((base_id, filename, content)),
        _FakeDoc(),
    )[1]
    await kb.knowledge_documents_add({"base_id": "b1", "path": "uploads/handbook.md"})
    assert took == [("b1", "handbook.md", b"hello")]


async def test_a_traversal_is_refused_too(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "uploads").mkdir()
    (tmp_path.parent / "escape.md").write_text("nope", encoding="utf-8")
    _fence(monkeypatch, tmp_path)

    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.add_document = lambda *a, **k: pytest.fail("must not reach the engine")  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError):
        await kb.knowledge_documents_add({"base_id": "b1", "path": "../escape.md"})


async def test_a_directory_is_not_a_document(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "uploads").mkdir()
    _fence(monkeypatch, tmp_path)
    kb._set_manager_for_tests(_FakeManager([_FakeBase("b1", "handbook")], {}))

    with pytest.raises(ConfigValidationError, match="not a file"):
        await kb.knowledge_documents_add({"base_id": "b1", "path": "uploads"})


async def test_deleting_a_document_reports_what_it_did() -> None:
    """The page's only way out of a document that will not index. Without it a
    row stuck on `pending` or `failed` could be cleared only by deleting the
    base around it -- every other document with it."""
    manager = _FakeManager([], {})
    gone: list[str] = []

    async def _delete(document_id: str) -> bool:
        gone.append(document_id)
        return True

    manager.delete_document = _delete  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_documents_delete({"document_id": "d1"})

    assert gone == ["d1"]
    assert out == {"removed": True}


async def test_deleting_what_is_already_gone_is_not_an_error() -> None:
    """Two clicks on one row, or a row another tab removed first: answering
    with a failure would report the reader's own success back as a problem."""
    manager = _FakeManager([], {})

    async def _delete(document_id: str) -> bool:
        return False

    manager.delete_document = _delete  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    assert await kb.knowledge_documents_delete({"document_id": "ghost"}) == {"removed": False}


async def test_deleting_needs_a_document_id() -> None:
    kb._set_manager_for_tests(_FakeManager([], {}))
    with pytest.raises(ConfigValidationError, match="document_id is required"):
        await kb.knowledge_documents_delete({})


async def test_indexing_answers_the_record_rather_than_a_bare_ok() -> None:
    """Indexing is the step that can half-succeed, and the row shows its own
    status and error."""
    manager = _FakeManager([], {})

    async def _index(document_id: str):
        return _FakeDoc(document_id, status="ready")

    manager.index_document = _index  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_documents_index({"document_id": "d1"})

    assert out["document"]["status"] == "ready"


async def test_indexing_an_unknown_document_says_so() -> None:
    manager = _FakeManager([], {})

    async def _index(document_id: str):
        return None

    manager.index_document = _index  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError, match="no such document"):
        await kb.knowledge_documents_index({"document_id": "ghost"})


async def test_search_projects_the_hit_to_score_document_and_text() -> None:
    from raven.knowledge import SearchOutcome

    class _Chunk:
        text = "the answer"
        chunk_index = 8
        total_chunks = 12
        source = "handbook.md"

    class _Hit:
        score = 0.87
        document_id = "d1"
        chunk = _Chunk()

    manager = _FakeManager([], {})
    asked: list[tuple[list[str], str, int]] = []

    async def _search(base_ids, query, top_k=5):
        asked.append((base_ids, query, top_k))
        return SearchOutcome(hits=[_Hit()], embed_ms=182.44, search_ms=6.02)

    manager.search = _search  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_search({"base_ids": ["b1"], "query": "what", "top_k": 3})

    assert asked == [(["b1"], "what", 3)]
    assert out["hits"] == [
        {
            "score": 0.87,
            # What that number is. Always stated, so a surface never has to
            # guess what an absent field meant.
            "retrieval": "vector",
            "document_id": "d1",
            "text": "the answer",
            # Where in its document the chunk sat, which is the first thing
            # anyone testing recall asks of a retrieved fragment.
            "chunk_index": 8,
            "total_chunks": 12,
            "source": "handbook.md",
        }
    ]
    # Reported apart: embedding is a round trip to the configured endpoint and
    # runs to hundreds of milliseconds, and calling that "the search time"
    # would describe the provider rather than the index.
    assert (out["search_ms"], out["embed_ms"]) == (6.0, 182.4)


async def test_a_hit_survives_a_chunk_that_carries_none_of_its_place() -> None:
    """Chunks written before the fields existed, and any parser that leaves
    them unset, still have to render as a row rather than fail the search."""
    from raven.knowledge import SearchOutcome

    class _Bare:
        text = "still readable"

    class _Hit:
        score = 0.5
        document_id = "d1"
        chunk = _Bare()

    manager = _FakeManager([], {})

    async def _search(base_ids, query, top_k=5):
        return SearchOutcome(hits=[_Hit()])

    manager.search = _search  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_search({"base_ids": ["b1"], "query": "what"})

    assert out["hits"][0]["chunk_index"] == 0
    assert out["hits"][0]["source"] == ""


async def test_search_without_a_base_or_a_query_is_refused() -> None:
    kb._set_manager_for_tests(_FakeManager([], {}))

    with pytest.raises(ConfigValidationError):
        await kb.knowledge_search({"base_ids": [], "query": "what"})
    with pytest.raises(ConfigValidationError):
        await kb.knowledge_search({"base_ids": ["b1"], "query": "   "})


async def test_a_duplicate_name_is_the_callers_mistake_not_the_gateways(monkeypatch) -> None:
    """Reported as a validation error so the page shows the sentence.

    Through InternalError it arrived as "could not create the base: ..." with
    the reason buried inside a message that reads like a fault in the gateway,
    which is the wrong thing to tell someone who typed a name twice.
    """
    from raven.knowledge import DuplicateBaseNameError

    manager = _FakeManager([], {})
    manager.create_raises = DuplicateBaseNameError("a knowledge base called 't3' already exists")
    monkeypatch.setattr(kb, "knowledge_manager", lambda: manager)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_bases_create({"name": "t3"})

    assert "already exists" in str(caught.value)


# ── notes and pages ───────────────────────────────────────────────


def _taking_manager() -> tuple[_FakeManager, list[dict[str, Any]]]:
    """A manager that records what ``add_document`` was handed."""
    took: list[dict[str, Any]] = []
    manager = _FakeManager([_FakeBase("b1", "handbook")], {})

    def _add(base_id, *, filename, content, origin="file", origin_ref=""):
        took.append(
            {
                "base_id": base_id,
                "filename": filename,
                "content": content,
                "origin": origin,
                "origin_ref": origin_ref,
            }
        )
        return _FakeDoc(source=filename, origin=origin, origin_ref=origin_ref)

    manager.add_document = _add  # type: ignore[assignment]
    return manager, took


async def test_a_note_is_stored_as_the_markdown_it_was_written_in() -> None:
    manager, took = _taking_manager()
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_documents_add_note({"base_id": "b1", "title": "Release plan", "text": "# Ship it"})

    assert (took[0]["filename"], took[0]["content"]) == ("Release plan.md", b"# Ship it")
    # Markdown, not plain text: the page's preview renders it and the chunker
    # splits it on its headings, both of which a .txt would lose.
    assert took[0]["origin"] == "note"
    assert out["document"]["origin"] == "note"


async def test_an_untitled_note_is_named_from_its_first_line() -> None:
    """Asking for a title before the text would put a required field in front
    of the thing the reader came to write."""
    manager, took = _taking_manager()
    kb._set_manager_for_tests(manager)

    await kb.knowledge_documents_add_note({"base_id": "b1", "text": "Standup notes\n\nshipped the picker"})

    assert took[0]["filename"] == "Standup notes.md"


async def test_an_empty_note_is_refused_rather_than_stored() -> None:
    manager, took = _taking_manager()
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError):
        await kb.knowledge_documents_add_note({"base_id": "b1", "title": "Later", "text": "   "})

    assert took == []


async def test_a_note_for_a_base_that_is_gone_says_so() -> None:
    manager, took = _taking_manager()
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_documents_add_note({"base_id": "nope", "text": "hi"})

    assert "no such base" in str(caught.value)
    assert took == []


async def test_editing_a_note_rewrites_it_and_sends_it_back_to_the_queue() -> None:
    rewrote: list[tuple[str, str, bytes]] = []
    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.get_document = lambda doc_id: _FakeDoc(doc_id, origin="note")  # type: ignore[assignment]

    async def _replace(document_id, *, filename, content):
        rewrote.append((document_id, filename, content))
        return _FakeDoc(document_id, source=filename, origin="note")

    manager.replace_document = _replace  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_documents_update_note({"document_id": "d7", "title": "Plan v2", "text": "# Ship it twice"})

    assert rewrote == [("d7", "Plan v2.md", b"# Ship it twice")]
    assert out["document"]["source"] == "Plan v2.md"


async def test_only_a_note_can_be_edited_in_place() -> None:
    """Every other origin is a copy of something the reader holds elsewhere;
    editing it here would make this base the only place the change exists."""
    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.get_document = lambda doc_id: _FakeDoc(doc_id, origin="file")  # type: ignore[assignment]
    manager.replace_document = lambda *a, **k: pytest.fail("must not rewrite an uploaded file")  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_documents_update_note({"document_id": "d7", "text": "no"})

    assert "only a note" in str(caught.value)


async def test_a_page_is_fetched_and_kept_with_the_url_it_came_from(monkeypatch) -> None:
    from raven.knowledge import _sources

    manager, took = _taking_manager()
    kb._set_manager_for_tests(manager)

    async def _fetch(url: str, *, api_key: str = ""):
        return _sources.FetchedPage(title="Raven Docs", markdown="# Raven\n\nhello")

    monkeypatch.setattr(_sources, "fetch_page", _fetch)

    out = await kb.knowledge_documents_add_url({"base_id": "b1", "url": "https://example.com/docs"})

    assert took[0]["filename"] == "Raven Docs.md"
    assert took[0]["content"] == b"# Raven\n\nhello"
    # Kept on the record rather than written into the text: the page shows
    # where a row came from, and a header inside the markdown would be
    # indexed and searched as if the reader had written it.
    assert took[0]["origin_ref"] == "https://example.com/docs"
    assert out["document"]["origin"] == "url"


async def test_a_page_that_cannot_be_read_says_why_and_stores_nothing(monkeypatch) -> None:
    from raven.knowledge import _sources

    manager, took = _taking_manager()
    kb._set_manager_for_tests(manager)

    async def _fetch(url: str, *, api_key: str = ""):
        raise _sources.SourceFetchError("the reader answered HTTP 404 for https://example.com/gone")

    monkeypatch.setattr(_sources, "fetch_page", _fetch)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_documents_add_url({"base_id": "b1", "url": "https://example.com/gone"})

    assert "404" in str(caught.value)
    assert took == []


async def test_a_url_document_needs_a_url() -> None:
    manager, took = _taking_manager()
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError):
        await kb.knowledge_documents_add_url({"base_id": "b1", "url": "  "})

    assert took == []


async def test_a_configured_reader_key_is_sent_with_the_fetch(monkeypatch) -> None:
    """Jina reads anonymously at a lower rate limit, so the key is optional --
    but a deployment that has set one should not be rate-limited as if it had
    not."""
    from raven.knowledge import _sources

    manager, _ = _taking_manager()
    kb._set_manager_for_tests(manager)
    monkeypatch.setattr(kb, "_jina_key", lambda: "k-1")
    keys: list[str] = []

    async def _fetch(url: str, *, api_key: str = ""):
        keys.append(api_key)
        return _sources.FetchedPage(title="Docs", markdown="# Docs")

    monkeypatch.setattr(_sources, "fetch_page", _fetch)

    await kb.knowledge_documents_add_url({"base_id": "b1", "url": "https://example.com"})

    assert keys == ["k-1"]


async def test_an_unreadable_config_does_not_stop_a_fetch(monkeypatch) -> None:
    """The key is an optimisation, not a requirement; refusing to read a page
    because the config could not be parsed helps nobody."""

    def _boom(**kwargs):
        raise RuntimeError("no config here")

    monkeypatch.setattr("raven.config.update_tools.get_jina_api_key", _boom)

    assert kb._jina_key() == ""


async def test_editing_a_note_that_is_gone_says_so() -> None:
    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.get_document = lambda doc_id: None  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_documents_update_note({"document_id": "gone", "text": "hi"})

    assert "no such document" in str(caught.value)


async def test_a_note_edit_needs_a_document_and_some_text() -> None:
    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.get_document = lambda doc_id: _FakeDoc(doc_id, origin="note")  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError):
        await kb.knowledge_documents_update_note({"text": "hi"})
    with pytest.raises(ConfigValidationError):
        await kb.knowledge_documents_update_note({"document_id": "d1", "text": "  "})


async def test_a_note_needs_a_base_id_at_all() -> None:
    manager, _ = _taking_manager()
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_documents_add_note({"text": "hi"})

    assert "base_id is required" in str(caught.value)


async def test_a_base_that_refuses_a_note_is_reported_rather_than_raised_raw() -> None:
    manager = _FakeManager([_FakeBase("b1", "handbook")], {})

    def _boom(*a, **k):
        raise RuntimeError("the store is gone")

    manager.add_document = _boom  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_documents_add_note({"base_id": "b1", "text": "hi"})

    assert "the store is gone" in str(caught.value)


async def test_a_base_that_refuses_a_page_is_reported_rather_than_raised_raw(monkeypatch) -> None:
    from raven.knowledge import _sources

    manager = _FakeManager([_FakeBase("b1", "handbook")], {})

    def _boom(*a, **k):
        raise RuntimeError("the store is gone")

    manager.add_document = _boom  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    async def _fetch(url: str, *, api_key: str = ""):
        return _sources.FetchedPage(title="Docs", markdown="# Docs")

    monkeypatch.setattr(_sources, "fetch_page", _fetch)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_documents_add_url({"base_id": "b1", "url": "https://example.com"})

    assert "the store is gone" in str(caught.value)


async def test_a_rewrite_that_vanishes_mid_flight_says_so() -> None:
    """Between the read and the write the row can be deleted from another
    surface; answering with a document that is not there is worse than saying
    it is gone."""
    manager = _FakeManager([_FakeBase("b1", "handbook")], {})
    manager.get_document = lambda doc_id: _FakeDoc(doc_id, origin="note")  # type: ignore[assignment]

    async def _replace(*a, **k):
        return None

    manager.replace_document = _replace  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_documents_update_note({"document_id": "d1", "text": "hi"})

    assert "no such document" in str(caught.value)


# ── settings ──────────────────────────────────────────────────────


def _settings_manager():
    """A manager whose bases carry settings and record what was written."""
    base = _FakeBase("b1", "handbook")
    base.top_k = 5
    base.smart_chunking = True
    base.separator = "\n\n"
    base.chunk_size = 512
    base.chunk_overlap = 50
    base.file_processing = ""
    manager = _FakeManager([base], {})
    wrote: list[dict[str, Any]] = []

    def _configure(base_id, **settings):
        wrote.append(settings)
        for key, value in settings.items():
            setattr(base, key, value)
        return base

    manager.configure_base = _configure  # type: ignore[assignment]
    return manager, wrote, base


async def test_settings_write_only_the_fields_that_were_sent() -> None:
    """A panel saving one slider should not have to send the rest back, and a
    field this build does not know about must not be blanked by one that
    does."""
    manager, wrote, _ = _settings_manager()
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_bases_settings({"base_id": "b1", "top_k": 12})

    assert wrote == [{"top_k": 12}]
    assert out["base"]["top_k"] == 12
    assert out["base"]["chunk_size"] == 512


async def test_every_setting_can_be_written_at_once() -> None:
    manager, wrote, _ = _settings_manager()
    kb._set_manager_for_tests(manager)

    await kb.knowledge_bases_settings(
        {
            "base_id": "b1",
            "top_k": 6,
            "smart_chunking": False,
            "separator": "\n",
            "chunk_size": 1024,
            "chunk_overlap": 200,
            "file_processing": "",
        }
    )

    assert wrote == [
        {
            "top_k": 6,
            "chunk_size": 1024,
            "chunk_overlap": 200,
            "smart_chunking": False,
            "separator": "\n",
            "file_processing": "",
        }
    ]


@pytest.mark.parametrize("value", [0, 51, -1])
async def test_a_top_k_outside_the_slider_is_refused_not_clamped(value) -> None:
    """Clamping answers a request the caller did not make and reports success,
    which on a slider is invisible."""
    manager, wrote, _ = _settings_manager()
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_bases_settings({"base_id": "b1", "top_k": value})

    assert "between 1 and 50" in str(caught.value)
    assert wrote == []


async def test_a_top_k_that_is_not_a_whole_number_is_refused() -> None:
    manager, wrote, _ = _settings_manager()
    kb._set_manager_for_tests(manager)

    for value in ("6", 6.5, True):
        with pytest.raises(ConfigValidationError):
            await kb.knowledge_bases_settings({"base_id": "b1", "top_k": value})
    assert wrote == []


async def test_an_overlap_as_big_as_the_chunk_is_refused() -> None:
    """Every chunk would contain the whole of the one before it."""
    manager, wrote, _ = _settings_manager()
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_bases_settings({"base_id": "b1", "chunk_size": 256, "chunk_overlap": 256})

    assert "smaller than chunk_size" in str(caught.value)
    assert wrote == []


async def test_the_overlap_is_judged_against_what_the_base_will_hold() -> None:
    """A call that moves only the overlap has to be checked against the chunk
    size already recorded, not against one it did not send."""
    manager, wrote, base = _settings_manager()
    base.chunk_size = 256
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError):
        await kb.knowledge_bases_settings({"base_id": "b1", "chunk_overlap": 300})

    await kb.knowledge_bases_settings({"base_id": "b1", "chunk_overlap": 100})
    assert wrote == [{"chunk_overlap": 100}]


async def test_a_picked_model_is_what_the_base_is_built_on() -> None:
    """The page offers every embedding model the install can reach, so the pair
    it picked has to reach the engine -- a base built on the configured pin
    whatever was chosen is a picker that decides nothing."""
    manager = _FakeManager([], {})
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_bases_create(
        {"name": "handbook", "embedding_model": "text-embedding-3-large", "embedding_provider": "openai"}
    )

    assert out["base"]["embedding_model"] == "text-embedding-3-large"
    assert out["base"]["embedding_provider"] == "openai"


async def test_a_base_created_without_a_pair_takes_the_configured_pin() -> None:
    """Which is what every base was built on before the choice existed."""
    manager = _FakeManager([], {})
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_bases_create({"name": "handbook"})

    assert out["base"]["embedding_model"] == "bge-m3"


async def test_sending_a_model_rebuilds_the_base_rather_than_writing_a_field() -> None:
    """The collection is sized to the model's width and holds vectors that
    model made, so this is a rebuild -- which is why it does not go through
    the settings write beside it."""
    manager, wrote, base = _settings_manager()
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_bases_settings(
        {"base_id": "b1", "embedding_model": "text-embedding-3-large", "embedding_provider": "openai"}
    )

    assert manager.switched == ("text-embedding-3-large", "openai")
    # And not written twice: the rebuild records the provider that served the
    # width it measured, so a settings write of the same key would be a second
    # opinion about it.
    assert wrote == []
    assert out["base"]["embedding_model"] == "text-embedding-3-large"


async def test_an_empty_model_turns_embedding_off() -> None:
    """The one choice that used to be fixed at creation."""
    manager, _, _ = _settings_manager()
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_bases_settings({"base_id": "b1", "embedding_model": ""})

    assert manager.switched == ("", "")
    assert out["base"]["embedding_model"] == ""


async def test_the_provider_alone_still_only_moves_the_address() -> None:
    """Sent without a model it is the repair it has always been: the same model
    reached through another account, with nothing to rebuild."""
    manager, wrote, _ = _settings_manager()
    kb._set_manager_for_tests(manager)

    await kb.knowledge_bases_settings({"base_id": "b1", "embedding_provider": "dashscope"})

    assert wrote == [{"embedding_provider": "dashscope"}]
    assert manager.switched is None


async def test_a_model_that_cannot_be_embedded_with_is_the_callers_mistake() -> None:
    """So the panel shows the sentence rather than "internal error" -- and the
    base is untouched, because the width is measured before anything drops."""
    from raven.knowledge import KnowledgeError

    manager, _, base = _settings_manager()
    manager.switch_raises = KnowledgeError("provider 'openai' has no usable credential")
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_bases_settings({"base_id": "b1", "embedding_model": "text-embedding-3-large"})

    assert "no usable credential" in str(caught.value)
    assert base.embedding_model == "bge-m3"


async def test_the_settings_land_before_the_documents_are_requeued() -> None:
    """A rebuild re-cuts every document, and it should cut them by the numbers
    the same call just wrote rather than by the ones it replaced."""
    manager, wrote, _ = _settings_manager()
    order: list[str] = []
    configure = manager.configure_base

    def _configure(base_id, **settings):
        order.append("settings")
        return configure(base_id, **settings)

    switch = manager.switch_embedding

    async def _switch(base_id, **kwargs):
        order.append("rebuild")
        return await switch(base_id, **kwargs)

    manager.configure_base = _configure  # type: ignore[assignment]
    manager.switch_embedding = _switch  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    await kb.knowledge_bases_settings({"base_id": "b1", "chunk_size": 1024, "embedding_model": "bge-large"})

    assert order == ["settings", "rebuild"]


async def test_settings_for_a_base_that_is_gone_say_so() -> None:
    manager, wrote, _ = _settings_manager()
    kb._set_manager_for_tests(manager)

    with pytest.raises(ConfigValidationError) as caught:
        await kb.knowledge_bases_settings({"base_id": "nope", "top_k": 6})

    assert "no such base" in str(caught.value)
    assert wrote == []


async def test_a_search_with_no_top_k_leaves_the_number_to_the_engine() -> None:
    """So the base's own setting decides, rather than a default written twice."""
    from raven.knowledge import SearchOutcome

    asked: list[Any] = []
    manager = _FakeManager([], {})

    async def _search(base_ids, query, top_k=None):
        asked.append(top_k)
        return SearchOutcome(hits=[])

    manager.search = _search  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    await kb.knowledge_search({"base_ids": ["b1"], "query": "what"})
    await kb.knowledge_search({"base_ids": ["b1"], "query": "what", "top_k": 3})

    assert asked == [None, 3]


async def test_a_keyword_answer_says_so_on_every_hit_and_once_on_the_answer() -> None:
    """An unreachable embedding endpoint turns a search into an ordinary
    looking result set scored on another scale. Forwarded as a bare number
    under a field documented as a similarity, that is a silent change of
    retrieval mode -- so the mode travels with it."""
    from raven.knowledge import SearchOutcome
    from raven.knowledge._types import Chunk, TextBlock, VectorSearchResult

    hit = VectorSearchResult(
        score=8.42,
        document_id="d1",
        chunk=Chunk(content=TextBlock(text="alpha"), source="a.md", chunk_index=0, total_chunks=1),
        retrieval="keyword",
    )
    manager = _FakeManager([], {})

    async def _search(base_ids, query, top_k=None):
        return SearchOutcome(hits=[hit], by_keyword={"b1": "the model could not be reached"})

    manager.search = _search  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_search({"base_ids": ["b1"], "query": "alpha"})

    assert out["hits"][0]["retrieval"] == "keyword"
    assert out["by_keyword"] == [{"base_id": "b1", "reason": "the model could not be reached"}]


async def test_an_ordinary_search_says_vector_and_nothing_else() -> None:
    """The field is always present, so a surface never has to guess what an
    absent one meant."""
    from raven.knowledge import SearchOutcome
    from raven.knowledge._types import Chunk, TextBlock, VectorSearchResult

    hit = VectorSearchResult(
        score=0.83,
        document_id="d1",
        chunk=Chunk(content=TextBlock(text="alpha"), source="a.md", chunk_index=0, total_chunks=1),
    )
    manager = _FakeManager([], {})

    async def _search(base_ids, query, top_k=None):
        return SearchOutcome(hits=[hit])

    manager.search = _search  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_search({"base_ids": ["b1"], "query": "alpha"})

    assert out["hits"][0]["retrieval"] == "vector"
    assert out["by_keyword"] == []


async def test_a_merged_chunk_carries_where_each_of_its_parts_came_from() -> None:
    """The flattened fields describe where a chunk starts, which is all they
    can do once a chunk can merge across sections. Sent alone they say this
    piece is on page 4 under one heading while half of it came from page 9
    under another, and the row has no route to the difference."""

    class _Chunk:
        chunk_index = 0
        total_chunks = 1
        text = "Third.\nSeventh."
        metadata = {
            "layout_type": "text",
            "page_number": 4,
            "page_end": 9,
            "heading_path": ["Handbook", "Part 3"],
            "elements": [
                {
                    "reading_order": 0,
                    "layout_type": "text",
                    "char_start": 0,
                    "char_end": 6,
                    "page_number": 4,
                    "heading_path": ["Handbook", "Part 3"],
                    "section_ordinal": 3,
                },
                {
                    "reading_order": 1,
                    "layout_type": "text",
                    "char_start": 7,
                    "char_end": 15,
                    "page_number": 9,
                    "heading_path": ["Handbook", "Part 7"],
                    "section_ordinal": 7,
                },
            ],
        }

    class _Held:
        chunk = _Chunk()
        chunk_id = "c1"
        enabled = True
        manual = False

    row = kb._chunk_row(_Held())

    assert row["page_number"] == 4
    assert row["page_end"] == 9, "and where it ends, which one page cannot say"
    assert [part["section_ordinal"] for part in row["parts"]] == [3, 7]
    assert [part["page_number"] for part in row["parts"]] == [4, 9]
    assert row["parts"][1]["heading_path"] == ["Handbook", "Part 7"]
    assert (row["parts"][0]["char_start"], row["parts"][0]["char_end"]) == (0, 6)


async def test_a_chunk_that_merged_nothing_carries_no_parts() -> None:
    """Its own fields already say where it is, and a list of one would make
    "this chunk was merged" unreadable from the answer."""

    class _Chunk:
        chunk_index = 0
        total_chunks = 1
        text = "Alone."
        metadata = {
            "page_number": 4,
            "heading_path": ["Handbook"],
            "elements": [{"reading_order": 0, "layout_type": "text", "char_start": 0, "char_end": 6}],
        }

    class _Held:
        chunk = _Chunk()
        chunk_id = "c1"
        enabled = True
        manual = False

    row = kb._chunk_row(_Held())

    assert row["parts"] == []
    assert row["page_end"] is None


async def test_chunks_answer_carries_what_the_parser_found(monkeypatch) -> None:
    """The row is flattened for the page: which piece it is, what kind of
    region, what page. The parser records more -- a box, the character range
    each element covers -- and none of it has a reader yet."""

    class _Chunk:
        chunk_index = 2
        total_chunks = 5
        text = "the body of it"
        metadata = {
            "layout_type": "heading",
            "page_number": 3,
            "heading_path": ["Terms", "Payment"],
            "bbox": {"x0": 72.0, "x1": 540.0},
        }

    class _Held:
        chunk_id = "abc123"
        chunk = _Chunk()
        enabled = True
        manual = False

    class _Manager:
        async def document_chunks(self, document_id: str, **kw):
            assert document_id == "d1"
            return [_Held()], 1

    monkeypatch.setattr(kb, "knowledge_manager", lambda: _Manager())

    out = await kb.knowledge_documents_chunks({"document_id": "d1"})

    assert out == {
        "chunks": [
            {
                "chunk_index": 2,
                "total_chunks": 5,
                "text": "the body of it",
                "layout_type": "heading",
                "page_number": 3,
                # Where it ends, and what it merged. Both empty here: this
                # chunk came from one section and stayed on one page, which is
                # the ordinary shape and the one that must not grow noise.
                "page_end": None,
                "heading_path": ["Terms", "Payment"],
                "parts": [],
                "chunk_id": "abc123",
                "enabled": True,
                "manual": False,
            }
        ],
        "total": 1,
    }


async def test_chunks_of_a_text_file_carry_no_page(monkeypatch) -> None:
    """Absent, not zero: a text file has no pages, and a page number of 0 would
    read as one."""

    class _Chunk:
        chunk_index = 0
        total_chunks = 1
        text = "plain"
        metadata = {"reading_order": 0}

    class _Held:
        chunk_id = ""
        chunk = _Chunk()
        enabled = True
        manual = False

    class _Manager:
        async def document_chunks(self, document_id: str, **kw):
            return [_Held()], 1

    monkeypatch.setattr(kb, "knowledge_manager", lambda: _Manager())

    row = (await kb.knowledge_documents_chunks({"document_id": "d1"}))["chunks"][0]

    assert row["page_number"] is None
    assert row["layout_type"] == ""
    assert row["heading_path"] == []


async def test_chunks_needs_a_document_id() -> None:
    with pytest.raises(ConfigValidationError):
        await kb.knowledge_documents_chunks({"document_id": "  "})


async def test_a_page_of_chunks_is_asked_for_by_number(monkeypatch) -> None:
    """Twenty a page, and the offset is the page the caller named."""
    seen: dict[str, object] = {}

    class _Manager:
        async def document_chunks(self, document_id: str, **kw):
            seen.update(kw)
            return [], 57

    monkeypatch.setattr(kb, "knowledge_manager", lambda: _Manager())

    out = await kb.knowledge_documents_chunks({"document_id": "d1", "page": 3})

    assert seen["offset"] == 40 and seen["limit"] == 20
    assert out["total"] == 57


async def test_a_query_searches_the_document_instead_of_paging_it(monkeypatch) -> None:
    """The search box answers with what matched, best first -- paging that would
    promise a second page of relevance nobody asked the engine for."""

    class _Chunk:
        chunk_index = 4
        total_chunks = 9
        text = "matched"
        metadata: dict = {}

    class _Held:
        chunk_id = "c9"
        chunk = _Chunk()
        enabled = True
        manual = False

    class _Manager:
        async def search_document(self, document_id: str, query: str, *, limit: int):
            assert (document_id, query, limit) == ("d1", "latency", 20)
            return [_Held()]

        async def document_chunks(self, *a, **kw):  # pragma: no cover - must not be reached
            raise AssertionError("a query searches rather than pages")

    monkeypatch.setattr(kb, "knowledge_manager", lambda: _Manager())

    out = await kb.knowledge_documents_chunks({"document_id": "d1", "query": "  latency "})

    assert out["total"] == 1 and out["chunks"][0]["chunk_id"] == "c9"


async def test_switching_chunks_needs_ids(monkeypatch) -> None:
    with pytest.raises(ConfigValidationError):
        await kb.knowledge_chunks_switch({"document_id": "d1", "chunk_ids": [], "enabled": False})
    with pytest.raises(ConfigValidationError):
        await kb.knowledge_chunks_switch({"document_id": "d1", "chunk_ids": ["  "], "enabled": False})


async def test_switching_chunks_answers_with_what_changed(monkeypatch) -> None:
    class _Manager:
        async def set_chunks_enabled(self, document_id: str, chunk_ids: list, enabled: bool):
            assert (document_id, chunk_ids, enabled) == ("d1", ["a", "b"], False)
            return 2

    monkeypatch.setattr(kb, "knowledge_manager", lambda: _Manager())

    assert await kb.knowledge_chunks_switch({"document_id": "d1", "chunk_ids": ["a", " b "], "enabled": False}) == {
        "changed": 2
    }


async def test_deleting_chunks_answers_with_what_is_left(monkeypatch) -> None:
    class _Manager:
        async def delete_chunks(self, document_id: str, chunk_ids: list):
            return 7

    monkeypatch.setattr(kb, "knowledge_manager", lambda: _Manager())

    assert await kb.knowledge_chunks_delete({"document_id": "d1", "chunk_ids": ["a"]}) == {"remaining": 7}


async def test_a_written_chunk_comes_back_as_the_page_reads_it(monkeypatch) -> None:
    class _Chunk:
        chunk_index = 12
        total_chunks = 13
        text = "written by hand"
        metadata = {"layout_type": "text"}

    class _Held:
        chunk_id = "m1"
        chunk = _Chunk()
        enabled = True
        manual = True

    class _Manager:
        async def add_chunk(self, document_id: str, text: str):
            assert text == "written by hand"
            return _Held()

    monkeypatch.setattr(kb, "knowledge_manager", lambda: _Manager())

    out = await kb.knowledge_chunks_create({"document_id": "d1", "text": "  written by hand  "})

    assert out["chunk"]["manual"] is True and out["chunk"]["chunk_id"] == "m1"


async def test_an_empty_written_chunk_is_refused() -> None:
    with pytest.raises(ConfigValidationError):
        await kb.knowledge_chunks_create({"document_id": "d1", "text": "   "})
