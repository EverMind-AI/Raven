"""LanceDB vector store: embedded, file-backed, no server.

LanceDB is embedded and file-backed, so a knowledge base adds no process to
the deployment: a collection is a directory under the state root, opened from
the gateway process the same way a config file is. The dependency is pinned
directly rather than leaned on transitively.

Two details are load-bearing:

*Scores.* LanceDB reports cosine *distance*, ascending; the interface promises
similarity, descending. The conversion happens here so no caller has to know
which backend answered -- a relevance floor means the same thing either way.

*Metadata.* The filter's keys are user data, so they cannot be table columns.
Each entry is flattened into one ``key=<json>`` string in a list column, which
``array_has_all`` matches with exactly the AND-of-equals semantics the
interface specifies, pushed into the scan rather than applied to the top-k
afterwards. JSON-encoding the value keeps ``1`` and ``"1"`` distinct.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from raven.knowledge._types import Chunk, DocumentSummary, VectorRecord, VectorSearchResult
from raven.knowledge._vector_store import VectorStoreBase

if TYPE_CHECKING:
    from pathlib import Path


def _encode_metadata(metadata: dict[str, Any]) -> list[str]:
    """One ``key=<json value>`` string per entry, order-independent."""
    return [f"{key}={json.dumps(value, sort_keys=True, ensure_ascii=False)}" for key, value in metadata.items()]


def _sql_quote(value: str) -> str:
    """A single-quoted SQL literal, with embedded quotes doubled."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _filter_predicate(metadata_filter: dict[str, Any] | None) -> str | None:
    """The scan predicate for ``metadata_filter``, or ``None`` for no filter."""
    if not metadata_filter:
        return None
    wanted = ", ".join(_sql_quote(entry) for entry in _encode_metadata(metadata_filter))
    return f"array_has_all(metadata_kv, [{wanted}])"


class LanceDBVectorStore(VectorStoreBase):
    """A directory of LanceDB tables, one per collection."""

    def __init__(self, path: "str | Path") -> None:
        self._path = str(path)
        self._db: Any = None

    async def _connect(self) -> Any:
        """The open connection, made on first use.

        Lazy because constructing a store is a wiring step that happens while
        the gateway builds its agent, and creating the directory then would
        leave one behind for a deployment that never touches a knowledge base.
        """
        if self._db is None:
            import lancedb

            self._db = await lancedb.connect_async(self._path)
        return self._db

    async def _table(self, name: str) -> Any:
        db = await self._connect()
        return await db.open_table(name)

    async def _collection_names(self) -> set[str]:
        """Every table in the database, paged to the end.

        ``list_tables`` is paginated and its default page size is the engine's
        to choose, so reading only the first response makes "does this
        collection exist" answer False once a deployment has more knowledge
        bases than one page holds -- which then reports an indexed base as
        missing and refuses to drop it.
        """
        db = await self._connect()
        names: set[str] = set()
        page_token: str | None = None
        while True:
            response = await db.list_tables(page_token=page_token)
            names.update(response.tables)
            page_token = response.page_token
            if not page_token:
                return names

    async def create_collection(self, name: str, dimensions: int) -> None:
        import pyarrow as pa

        db = await self._connect()
        if name in await self._collection_names():
            return
        schema = pa.schema(
            [
                pa.field("vector", pa.list_(pa.float32(), dimensions)),
                pa.field("document_id", pa.string()),
                pa.field("source", pa.string()),
                pa.field("chunk_index", pa.int32()),
                # The chunk rides as JSON rather than as columns: it is handed
                # back whole and never queried by field, and a fixed schema
                # would have to change every time a parser adds metadata.
                pa.field("chunk_json", pa.string()),
                pa.field("metadata_kv", pa.list_(pa.string())),
            ]
        )
        await db.create_table(name, schema=schema)

    async def delete_collection(self, name: str) -> None:
        db = await self._connect()
        if name in await self._collection_names():
            await db.drop_table(name)

    async def has_collection(self, name: str) -> bool:
        return name in await self._collection_names()

    async def insert(self, collection: str, records: list[VectorRecord]) -> None:
        if not records:
            return
        table = await self._table(collection)
        await table.add(
            [
                {
                    "vector": record.vector,
                    "document_id": record.document_id,
                    "source": record.chunk.source,
                    "chunk_index": record.chunk.chunk_index,
                    "chunk_json": record.chunk.model_dump_json(),
                    "metadata_kv": _encode_metadata(record.chunk.metadata),
                }
                for record in records
            ]
        )

    async def delete(self, collection: str, document_id: str) -> None:
        table = await self._table(collection)
        await table.delete(f"document_id = {_sql_quote(document_id)}")

    async def search(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int = 5,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        table = await self._table(collection)
        query = table.vector_search(query_vector).distance_type("cosine").limit(top_k)
        predicate = _filter_predicate(metadata_filter)
        if predicate:
            query = query.where(predicate)
        rows = await query.to_list()
        return [
            VectorSearchResult(
                # Cosine distance is 1 - similarity, so this restores the
                # similarity the interface promises without changing the order.
                score=1.0 - float(row["_distance"]),
                document_id=row["document_id"],
                chunk=Chunk.model_validate_json(row["chunk_json"]),
            )
            for row in rows
        ]

    async def list_documents(
        self,
        collection: str,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[DocumentSummary]:
        table = await self._table(collection)
        # Vectors are the bulk of a row and nothing here reads them, so the
        # scan names its columns instead of pulling the table through memory.
        query = table.query().select(["document_id", "source", "chunk_json"])
        predicate = _filter_predicate(metadata_filter)
        if predicate:
            query = query.where(predicate)
        rows = await query.to_list()

        summaries: dict[str, DocumentSummary] = {}
        for row in rows:
            document_id = row["document_id"]
            existing = summaries.get(document_id)
            if existing is None:
                chunk = Chunk.model_validate_json(row["chunk_json"])
                summaries[document_id] = DocumentSummary(
                    document_id=document_id,
                    source=row["source"],
                    chunk_count=1,
                    metadata=chunk.metadata,
                )
            else:
                existing.chunk_count += 1
        return list(summaries.values())
