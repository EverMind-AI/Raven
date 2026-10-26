# Knowledge bases and document retrieval

A knowledge base stores documents you deliberately supply and, when configured
with embeddings, retrieves matching passages. It is separate from the memory
backend's recollection of conversations.

Manage bases and documents on the WebUI's Knowledge page. Attach bases to a
conversation in the composer so the agent can retrieve passages with
`knowledge_search`. For scripts and integrations, use the Python library or
`knowledge.*` RPC methods.

## Choose documents, knowledge, or memory

| Need | Choose |
| --- | --- |
| Read a file for this task | Attach the file or provide a readable working-directory path |
| Repeatedly retrieve passages from a document set | An indexed knowledge base |
| Keep a set of source documents without vector search | A base created with `embedding: false` |
| Recall user preferences or agent experience | [Memory and skills](skills-and-extensions.md) |

The agent searches the bases selected for the conversation when it needs
supporting material, and receives passages with document references. Retrieved
text is evidence to evaluate, not trusted instructions.

## Use knowledge in the WebUI

1. Open **Knowledge** and create a base. Choose an embedding model for semantic
   retrieval, or keep the base as a document collection.
2. Add files, URLs, or notes. Check each document's indexing status and preview
   its content before using it as a source.
3. Select the intended bases in the conversation composer, then ask a question
   about those documents. The `knowledge_search` tool searches that selection.
4. Follow the returned source references to inspect the passages used in the
   answer.

The Knowledge page also provides document organization, search, previews, and
base settings. Use the library/RPC workflow below for programmatic access.

## Configure embeddings

The endpoint is configured in Raven, not by depending on an installed memory
plugin. Merge this fragment, using an already configured provider and an
embedding model it actually serves:

```json
{
  "embedding": {
    "provider": "custom",
    "model": "YOUR_EMBEDDING_MODEL"
  }
}
```

The provider owns the URL and credentials. A chat model is not necessarily an
embedding model. Base creation probes vector width; indexing sends document
chunks, and search sends the query, to that endpoint. These operations can
incur charges and disclose their text to that provider.

The vector index is embedded/file-backed; a separate vector service is not
required. Each base records its embedding provider, model, and dimensions.
Changing the default model leaves existing bases on their recorded model.
Use the base's settings to switch models and rebuild its index. If an indexed
base's model is unavailable, search falls back to keyword matching and reports
which bases used that fallback.

## A small end-to-end example

For a trusted source-checkout script, save this as `knowledge_demo.py` outside
the repository and run it with the project's environment. It uses a separate
`knowledge-demo/` directory under the current working directory, not the
running gateway's store. It performs real embedding calls.

```python
"""Create and query a small isolated knowledge base."""

import asyncio
from pathlib import Path

from raven.knowledge import KnowledgeManager


async def main():
    manager = KnowledgeManager(Path("./knowledge-demo"))
    base = await manager.create_base(name="Release handbook")
    document = manager.add_document(
        base.id,
        filename="release.md",
        content=b"# Release checklist\nRun unit tests before publishing a release.\n",
    )
    indexed = await manager.index_document(document.id)
    if indexed is None or indexed.status != "ready" or not indexed.chunk_count:
        raise RuntimeError(indexed.error if indexed else "Document disappeared")
    result = await manager.search([base.id], "What must run before publishing?", top_k=3)
    for hit in result.hits:
        print(hit.document_id, hit.chunk.source, hit.score, hit.chunk.text)


asyncio.run(main())
```

```bash
uv run python /absolute/path/to/knowledge_demo.py
```

The directory and index remain afterward. Running again with the same base
name is refused; use the existing base id or a fresh demo directory.
Check that the returned passage really says unit tests are required. A
similarity score is ranking information, not a probability that an answer is
correct. Ask a model to answer from those passages only after checking sources.

## Use the hosted RPC workflow

An authenticated client of Raven RPC follows the same lifecycle. These are
RPC methods, not HTTP resource paths or A2A methods:

| Step | Method and parameters | Check |
| --- | --- | --- |
| Discover | `knowledge.status` with `{}` | Configured model and supported extensions |
| Create | `knowledge.bases.create` with name/description | Save returned `base.id` |
| Add a note | `knowledge.documents.add_note` with `base_id`, `title`, `text` | Save returned document id |
| Add a file | `knowledge.documents.add` with `base_id` and uploaded `path` | The path must be readable on the host |
| Index | `knowledge.documents.index` with `document_id` | Inspect `status`, `error`, and `chunk_count` |
| Search | `knowledge.search` with `base_ids`, `query`, optional `top_k` | Inspect hits, source, and chunk positions |

For example, the search parameters after creating a base are:

```json
{
  "base_ids": ["BASE_ID_FROM_CREATE"],
  "query": "What must run before publishing?",
  "top_k": 3
}
```

Adding and indexing are separate. “Uploaded” does not mean searchable, and
an indexing RPC can return a document marked failed. A non-embedding base can
be ready with zero chunks and is skipped by vector search.

`knowledge.documents.add_url` fetches and stores a page snapshot; it is not
continuous website synchronization. File uploads likewise store a copy, not a
live filesystem subscription.

## Formats, updates, and deletion

Use `knowledge.status.extensions` or `supported_extensions()` for the full
upload list. The default parsers support PDF, Word (`.docx`, `.doc`), spreadsheets
(`.xlsx`, `.xls`, `.csv`), presentations (`.pptx`, `.ppt`), images, structured
Markdown/HTML, and plain-text formats including JSON/YAML.

Legacy Office formats (`.doc`, `.xls`, `.ppt`) use LibreOffice for conversion.
Image indexing requires a configured vision model. PDF layout and table
analysis use the resources installed by `raven resources`; see
[Quick Start](quick-start.md) for installation options.

Notes can be revised with `knowledge.documents.update_note`, then reindexed.
Reindexing replaces that document's vectors rather than appending duplicates.
Changing chunk settings requires reindexing existing documents to apply them;
switching embedding models rebuilds the index from the base's stored documents.

`knowledge.documents.delete` removes a document and associated stored data;
`knowledge.bases.delete` removes the base with its documents and index.
Neither operation is an undoable trash workflow. Keep original source files
and backups outside the knowledge store.

The gateway stores records, blobs, and vectors under its knowledge data
directory. Use the running service's RPC instead of a second process opening
the same store. The standalone example is deliberately separate.

## Privacy and troubleshooting

Host file imports pass through the readable-path policy, including protection
of Raven's state/credential files. An id-based preview is not permission to
read arbitrary host paths. Library scripts are trusted local code and do not
replace the hosted RPC access checks.

| Symptom | Check |
| --- | --- |
| Embedding not configured | Provider/model pair, endpoint capability, and credentials |
| Indexing failed | Document error, actual parser support, and embedding endpoint |
| Search empty | Indexed chunks, selected base ids, and whether embeddings were enabled |
| Keyword fallback or stale base | The base's recorded model, provider credentials, and dimensions; restore access or switch its model in base settings |
| Changed source not reflected | Stored copy versus live source; update/import and index again |
| Answer lacks evidence | Select the intended bases in the composer, check indexed content, and ask the agent to cite retrieved passages |

Implementation: `ui-web/src/features/knowledge/`, `raven/knowledge/`,
`raven/agent/tools/knowledge.py`, `raven/rpc/methods/knowledge.py`, and
`raven/rpc/knowledge_preview.py`.
