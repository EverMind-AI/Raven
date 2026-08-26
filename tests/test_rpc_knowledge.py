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

    def list_bases(self) -> list[_FakeBase]:
        return list(self._bases)

    def list_documents(self, base_id: str) -> list[object]:
        self.asked.append(base_id)
        return [object()] * self._docs.get(base_id, 0)

    def get_base(self, base_id: str):
        return next((b for b in self._bases if b.id == base_id), None)

    async def create_base(self, *, name: str, description: str = ""):
        if self.create_raises is not None:
            raise self.create_raises
        made = _FakeBase(f"b{len(self._bases) + 1}", name)
        made.description = description
        self._bases.append(made)
        return made

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

    monkeypatch.setattr("raven.knowledge.load_embedding_config", lambda: _Config())

    assert await kb.knowledge_status({}) == {"configured": True, "model": "bge-m3"}


async def test_status_says_unconfigured_rather_than_failing(monkeypatch) -> None:
    """A deployment with no embedding endpoint is an ordinary state: the page
    has to open and say "configure this first", not fail to load."""
    monkeypatch.setattr("raven.knowledge.load_embedding_config", lambda: None)

    assert await kb.knowledge_status({}) == {"configured": False, "model": ""}


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
        "dimensions",
        "created_at",
        "updated_at",
        "documents",
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
    def __init__(self, doc_id: str = "d1", status: str = "pending") -> None:
        self.id = doc_id
        self.base_id = "b1"
        self.source = "handbook.md"
        self.media_type = "text/markdown"
        self.size = 5
        self.status = status
        self.chunk_count = 0
        self.error = ""
        self.created_at = "2026-08-24T00:00:00"
        self.updated_at = "2026-08-24T00:00:00"


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
    class _Chunk:
        text = "the answer"

    class _Hit:
        score = 0.87
        document_id = "d1"
        chunk = _Chunk()

    manager = _FakeManager([], {})
    asked: list[tuple[list[str], str, int]] = []

    async def _search(base_ids, query, top_k=5):
        asked.append((base_ids, query, top_k))
        return [_Hit()]

    manager.search = _search  # type: ignore[assignment]
    kb._set_manager_for_tests(manager)

    out = await kb.knowledge_search({"base_ids": ["b1"], "query": "what", "top_k": 3})

    assert asked == [(["b1"], "what", 3)]
    assert out["hits"] == [{"score": 0.87, "document_id": "d1", "text": "the answer"}]


async def test_search_without_a_base_or_a_query_is_refused() -> None:
    kb._set_manager_for_tests(_FakeManager([], {}))

    with pytest.raises(ConfigValidationError):
        await kb.knowledge_search({"base_ids": [], "query": "what"})
    with pytest.raises(ConfigValidationError):
        await kb.knowledge_search({"base_ids": ["b1"], "query": "   "})
