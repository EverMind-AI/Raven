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

from loguru import logger

from raven.knowledge._types import Chunk, DocumentSummary, StoredChunk, VectorRecord, VectorSearchResult
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


def _id_predicate(chunk_ids: list[str]) -> str:
    """The clause matching a set of chunk ids.

    An empty id never matches: rows from a build before ids existed all carry
    one, and a caller asking to act on "" means something went wrong upstream,
    not that every unnamed row in the document should change at once.
    """
    wanted = ", ".join(_sql_quote(chunk_id) for chunk_id in chunk_ids if chunk_id)
    return f"chunk_id IN ({wanted})" if wanted else "false"


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
                # What a reader addresses one piece by, and the state they can
                # put it in. Columns rather than metadata because both are
                # queried: the id to act on a row, `enabled` on every search.
                pa.field("chunk_id", pa.string()),
                pa.field("enabled", pa.bool_()),
                pa.field("manual", pa.bool_()),
                # The chunk's text as its own column, for the keyword index.
                # Not read back from here -- `chunk_json` is still what a
                # caller gets -- but BM25 needs a column to tokenize, and
                # tokenizing the JSON would index its keys and punctuation.
                pa.field("text", pa.string()),
            ]
        )
        await db.create_table(name, schema=schema)

    async def delete_collection(self, name: str) -> None:
        db = await self._connect()
        if name in await self._collection_names():
            await db.drop_table(name)

    async def has_collection(self, name: str) -> bool:
        return name in await self._collection_names()

    async def _writable(self, collection: str) -> Any:
        """The table, with the columns this build writes.

        A collection made by an earlier build has neither an id nor a state on
        its rows, and adding them is a metadata edit rather than a rewrite --
        so the widening happens on first use rather than in a migration pass.
        Existing rows come out enabled and unnamed: they answer searches as
        they always did, and a reader who wants to act on one piece at a time
        reindexes the document, which writes ids.
        """
        table = await self._table(collection)
        names = set((await table.schema()).names)
        missing = {
            "chunk_id": ("string", "''"),
            "enabled": ("bool", "true"),
            "manual": ("bool", "false"),
            "text": ("string", "''"),
        }
        adding = {column: default for column, (_, default) in missing.items() if column not in names}
        if adding:
            await table.add_columns(adding)
        return table

    async def insert(self, collection: str, records: list[VectorRecord]) -> None:
        if not records:
            return
        table = await self._writable(collection)
        await table.add(
            [
                {
                    "vector": record.vector,
                    "document_id": record.document_id,
                    "source": record.chunk.source,
                    "chunk_index": record.chunk.chunk_index,
                    "chunk_json": record.chunk.model_dump_json(),
                    "metadata_kv": _encode_metadata(record.chunk.metadata),
                    "chunk_id": record.chunk_id,
                    "enabled": record.enabled,
                    "manual": record.manual,
                    "text": record.chunk.text,
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
        document_id: str | None = None,
    ) -> list[VectorSearchResult]:
        table = await self._table(collection)
        query = table.vector_search(query_vector).distance_type("cosine").limit(top_k)
        # Disabled means not retrieved, which has to be enforced where the
        # retrieval happens -- a filter applied by any one caller is a filter
        # the next caller forgets. `enabled IS NULL` covers the rows a build
        # before this column wrote, which are enabled by construction.
        clauses = ["(enabled IS NULL OR enabled = true)"]
        if document_id:
            clauses.append(f"document_id = {_sql_quote(document_id)}")
        predicate = _filter_predicate(metadata_filter)
        if predicate:
            clauses.append(predicate)
        query = query.where(" AND ".join(clauses))
        rows = await query.to_list()
        return [
            VectorSearchResult(
                # Cosine distance is 1 - similarity, so this restores the
                # similarity the interface promises without changing the order.
                score=1.0 - float(row["_distance"]),
                document_id=row["document_id"],
                chunk=Chunk.model_validate_json(row["chunk_json"]),
                chunk_id=row.get("chunk_id") or "",
            )
            for row in rows
        ]

    async def _keyword_index(self, table: Any) -> bool:
        """Make sure the text column is indexed for BM25, once per collection.

        The tokenizer is n-grams rather than the default word splitter, which
        is what makes this work at all on Chinese: the default splits on
        whitespace and punctuation, so a query in a language that writes
        without spaces matches nothing. N-grams cost a larger index and a
        looser match in English, and they are the only setting that serves a
        corpus holding both without a language model to download.
        """
        from lancedb.index import FTS

        try:
            for index in await table.list_indices():
                if "text" in (getattr(index, "columns", None) or []):
                    return True
            await table.create_index(
                "text",
                config=FTS(base_tokenizer="ngram", ngram_min_length=2, ngram_max_length=3, lower_case=True),
            )
            return True
        except Exception as exc:
            # Not fatal: a base whose keyword index cannot be built is one that
            # answers by vector only, which is what it did before this existed.
            logger.warning("knowledge: no keyword index for this collection: {}", exc)
            return False

    async def keyword_search(
        self,
        collection: str,
        query: str,
        top_k: int = 5,
        document_id: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        if not query.strip() or not await self.has_collection(collection):
            return []
        table = await self._writable(collection)
        if not await self._keyword_index(table):
            return []
        clauses = ["(enabled IS NULL OR enabled = true)"]
        if document_id:
            clauses.append(f"document_id = {_sql_quote(document_id)}")
        predicate = _filter_predicate(metadata_filter)
        if predicate:
            clauses.append(predicate)
        search = await table.search(query, query_type="fts")
        rows = await search.where(" AND ".join(clauses)).limit(top_k).to_list()
        return [
            VectorSearchResult(
                # BM25, not a cosine similarity. Higher is still nearer, which
                # is the direction every caller reads, but the scale is the
                # index's own -- so these are never compared with vector scores
                # by value. The caller merges by rank instead.
                score=float(row.get("_score", 0.0)),
                document_id=row["document_id"],
                chunk=Chunk.model_validate_json(row["chunk_json"]),
                chunk_id=row.get("chunk_id") or "",
            )
            for row in rows
        ]

    async def list_chunks(
        self,
        collection: str,
        document_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        enabled: bool | None = None,
    ) -> tuple[list[StoredChunk], int]:
        if not await self.has_collection(collection):
            return [], 0
        table = await self._writable(collection)
        clauses = [f"document_id = {_sql_quote(document_id)}"]
        if enabled is True:
            clauses.append("(enabled IS NULL OR enabled = true)")
        elif enabled is False:
            clauses.append("enabled = false")
        rows = await (
            table.query()
            .where(" AND ".join(clauses))
            # The vector is the bulk of a row and nothing here reads it.
            .select(["chunk_index", "chunk_json", "chunk_id", "enabled", "manual"])
            .to_list()
        )
        stored = [
            StoredChunk(
                chunk_id=row.get("chunk_id") or "",
                chunk=Chunk.model_validate_json(row["chunk_json"]),
                # Null is what a row written before the column had; those rows
                # are enabled, which is how search already treats them.
                enabled=row.get("enabled") is not False,
                manual=bool(row.get("manual")),
            )
            for row in rows
        ]
        # By the chunk's own number rather than the row's: a scan has no order
        # to promise, and the number is what the chunker wrote as it walked the
        # document. Paged after sorting, so a page is a window on the document
        # and not on whatever the scan happened to return.
        stored.sort(key=lambda held: held.chunk.chunk_index)
        total = len(stored)
        if limit is None:
            return stored[offset:], total
        return stored[offset : offset + limit], total

    async def set_chunks_enabled(self, collection: str, chunk_ids: list[str], enabled: bool) -> int:
        """Turn pieces on or off. Returns how many rows the store changed."""
        if not chunk_ids or not await self.has_collection(collection):
            return 0
        table = await self._writable(collection)
        result = await table.update({"enabled": enabled}, where=_id_predicate(chunk_ids))
        return int(getattr(result, "rows_updated", 0) or 0)

    async def delete_chunks(self, collection: str, chunk_ids: list[str]) -> None:
        if not chunk_ids or not await self.has_collection(collection):
            return
        table = await self._writable(collection)
        await table.delete(_id_predicate(chunk_ids))

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
