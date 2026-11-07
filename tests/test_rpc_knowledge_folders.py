"""``knowledge.folders.*``, ``documents.move/index`` and the chunk editors.

The handlers themselves are thin -- read the parameters, refuse what is
missing, hand the rest to the manager -- and that is exactly what is worth
pinning: which parameter is required, which failure is a validation error the
page can show against a field, and which is an internal one.

The manager is supplied. What it does with a folder is its own, and is tested
in `tests/test_knowledge_manager.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from raven.rpc.errors import ConfigValidationError, InternalError
from raven.rpc.methods import knowledge as kb

pytestmark = pytest.mark.asyncio


class _Folder:
    def __init__(self, folder_id: str, base_id: str = "b1", name: str = "notes") -> None:
        self.id = folder_id
        self.base_id = base_id
        self.name = name
        self.created_at = "2026-08-24T00:00:00"


class _Doc:
    """Every field `_doc_row` reads. Spelled out rather than mocked, so a field
    added to the row without a value here fails loudly instead of answering
    the page with a default."""

    def __init__(self, doc_id: str = "d1", folder_id: str = "") -> None:
        self.id = doc_id
        self.base_id = "b1"
        self.folder_id = folder_id
        self.source = "report.pdf"
        self.media_type = "application/pdf"
        self.origin = "upload"
        self.origin_ref = ""
        self.status = "ready"
        self.error = ""
        self.warning = ""
        self.chunk_count = 3
        self.size = 1024
        self.created_at = "2026-08-24T00:00:00"
        self.updated_at = "2026-08-24T00:01:00"


class _Manager:
    """Only the calls these handlers make."""

    def __init__(self, *, base: object | None = object(), folders=(), documents=()) -> None:
        self._base = base
        self.folders = list(folders)
        self.documents = list(documents)
        self.created: list[tuple[str, str]] = []
        self.renamed: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.moved: list[tuple[str, str]] = []
        self.indexed: list[str] = []
        self.move_answer: object | None = _Doc()
        self.index_answer: object | None = _Doc()
        self.index_raises: Exception | None = None

    def get_base(self, base_id: str):
        return self._base

    def list_folders(self, base_id: str):
        return list(self.folders)

    def list_documents(self, base_id: str):
        return list(self.documents)

    def create_folder(self, base_id: str, name: str):
        self.created.append((base_id, name))
        return _Folder("f-new", base_id, name)

    def rename_folder(self, folder_id: str, name: str):
        self.renamed.append((folder_id, name))
        return _Folder(folder_id, name=name)

    def delete_folder(self, folder_id: str) -> bool:
        self.deleted.append(folder_id)
        return True

    def move_document(self, document_id: str, folder_id: str):
        self.moved.append((document_id, folder_id))
        return self.move_answer

    async def index_document(self, document_id: str):
        self.indexed.append(document_id)
        if self.index_raises is not None:
            raise self.index_raises
        return self.index_answer


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch):
    def install(instance: _Manager) -> _Manager:
        monkeypatch.setattr(kb, "knowledge_manager", lambda: instance)
        return instance

    return install


class TestListingFolders:
    async def test_a_folder_carries_how_many_documents_are_in_it(self, manager) -> None:
        """The count is computed here rather than stored, so a document moved
        by any other route cannot leave it stale."""
        manager(
            _Manager(
                folders=[_Folder("f1")],
                documents=[_Doc("d1", folder_id="f1"), _Doc("d2", folder_id="f1"), _Doc("d3", folder_id="")],
            )
        )

        out = await kb.knowledge_folders_list({"base_id": "b1"})

        assert out["folders"][0]["documents"] == 2

    async def test_root_is_not_a_folder(self, manager) -> None:
        """A document with no folder is in Root, which is why every document
        written before folders existed is already somewhere sensible."""
        manager(_Manager(folders=[], documents=[_Doc("d1", folder_id="")]))

        assert await kb.knowledge_folders_list({"base_id": "b1"}) == {"folders": []}

    async def test_a_base_that_is_not_there_is_refused(self, manager) -> None:
        manager(_Manager(base=None))

        with pytest.raises(ConfigValidationError):
            await kb.knowledge_folders_list({"base_id": "nope"})

    async def test_the_base_is_required(self, manager) -> None:
        manager(_Manager())

        with pytest.raises(ConfigValidationError):
            await kb.knowledge_folders_list({})


class TestCreatingAFolder:
    async def test_it_answers_the_row_the_list_would_show(self, manager) -> None:
        instance = manager(_Manager())

        out = await kb.knowledge_folders_create({"base_id": "b1", "name": "papers"})

        assert out["folder"]["name"] == "papers"
        assert instance.created == [("b1", "papers")]

    async def test_a_name_of_spaces_is_refused(self, manager) -> None:
        instance = manager(_Manager())

        with pytest.raises(ConfigValidationError):
            await kb.knowledge_folders_create({"base_id": "b1", "name": "   "})

        assert instance.created == [], "nothing written"

    async def test_the_name_is_trimmed(self, manager) -> None:
        instance = manager(_Manager())

        await kb.knowledge_folders_create({"base_id": "b1", "name": "  papers  "})

        assert instance.created == [("b1", "papers")]


class TestRenamingAFolder:
    async def test_the_folder_is_required(self, manager) -> None:
        manager(_Manager())

        with pytest.raises(ConfigValidationError):
            await kb.knowledge_folders_rename({"name": "papers"})

    async def test_an_empty_name_is_refused(self, manager) -> None:
        instance = manager(_Manager())

        with pytest.raises(ConfigValidationError):
            await kb.knowledge_folders_rename({"folder_id": "f1", "name": ""})

        assert instance.renamed == []


class TestDeletingAFolder:
    async def test_the_folder_is_required(self, manager) -> None:
        manager(_Manager())

        with pytest.raises(ConfigValidationError):
            await kb.knowledge_folders_delete({})


class TestMovingADocument:
    async def test_a_move_answers_the_row(self, manager) -> None:
        instance = manager(_Manager())

        out = await kb.knowledge_documents_move({"document_id": "d1", "folder_id": "f1"})

        assert out["document"]["id"] == "d1"
        assert instance.moved == [("d1", "f1")]

    async def test_an_empty_folder_means_root(self, manager) -> None:
        instance = manager(_Manager())

        await kb.knowledge_documents_move({"document_id": "d1"})

        assert instance.moved == [("d1", "")]

    async def test_the_document_is_required(self, manager) -> None:
        manager(_Manager())

        with pytest.raises(ConfigValidationError):
            await kb.knowledge_documents_move({"folder_id": "f1"})

    async def test_a_move_the_manager_refused_is_a_validation_error(self, manager) -> None:
        """One message for both halves: the page cannot tell a missing document
        from a missing folder apart, and neither can the reader."""
        instance = manager(_Manager())
        instance.move_answer = None

        with pytest.raises(ConfigValidationError):
            await kb.knowledge_documents_move({"document_id": "gone", "folder_id": "f1"})


class TestIndexingADocument:
    async def test_it_answers_the_record_rather_than_a_bare_ok(self, manager) -> None:
        """Indexing is the step that can half-succeed, and the row's own status
        and error are what says so."""
        instance = manager(_Manager())

        out = await kb.knowledge_documents_index({"document_id": "d1"})

        assert out["document"]["status"] == "ready"
        assert instance.indexed == ["d1"]

    async def test_the_document_is_required(self, manager) -> None:
        manager(_Manager())

        with pytest.raises(ConfigValidationError):
            await kb.knowledge_documents_index({})

    async def test_a_document_that_is_not_there_is_a_validation_error(self, manager) -> None:
        instance = manager(_Manager())
        instance.index_answer = None

        with pytest.raises(ConfigValidationError):
            await kb.knowledge_documents_index({"document_id": "gone"})

    async def test_a_failure_inside_the_engine_is_an_internal_error(self, manager) -> None:
        """Not a validation error: there is no field the page could correct."""
        instance = manager(_Manager())
        instance.index_raises = RuntimeError("the embedding host refused")

        with pytest.raises(InternalError):
            await kb.knowledge_documents_index({"document_id": "d1"})

    async def test_the_reason_rides_along(self, manager) -> None:
        instance = manager(_Manager())
        instance.index_raises = RuntimeError("the embedding host refused")

        with pytest.raises(InternalError, match="the embedding host refused"):
            await kb.knowledge_documents_index({"document_id": "d1"})


class TestFolderRow:
    @pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
    async def test_the_row_is_the_shape_the_contract_declares(self) -> None:
        instance = _Manager(documents=[_Doc("d1", folder_id="f1")])

        row: dict[str, Any] = kb._folder_row(instance, _Folder("f1"))

        assert set(row) == {"id", "base_id", "name", "created_at", "documents"}
