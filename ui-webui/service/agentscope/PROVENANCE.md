# agentscope -- provenance

This package is part of the web service. `service/main.py` builds its FastAPI app
from `create_app()` here, and the Redis storage layer, the RAG/knowledge-base
managers and the sub-agent DAG machinery all live in this tree. It is imported as
plain `agentscope` because it sits next to `main.py`, so `sys.path[0]` (the
service's own directory) resolves it -- no `PYTHONPATH`, no install step.

It is *not* an external checkout and no longer tracks an upstream remote. This
file records where the code originally came from, which matters for the license
and for anyone diffing against the public project.

| | |
|---|---|
| Origin | `agentscope` 2.0.4 |
| License | Apache-2.0 (`LICENSE` in this directory) |
| Public project | <https://github.com/agentscope-ai/agentscope> |
| Adopted from | the RavenX fork, commit `c84fd1a54dcc207257a8d616ff63b65470febc95` |
| That fork's base | public `4cb5832075e7bf42d7f4c226239f15bae9ab24c2` |

The fork was 188 commits ahead of the public project, and its `src/agentscope`
differed by 61 files -- 30 added, but also **31 modified**, including
`app/_router/_session.py`, `app/storage/_redis_storage.py` and most of
`subagent/`. Because it changes internals rather than only adding to them,
depending on the published package and layering the additions on top was never an
option; the code had to be adopted outright.

## Local changes

- **Removed the 78 model-card YAML catalogs** (`*/_models/*.yaml` and
  `tts/_dashscope/_cosyvoice_models/*.yaml`) that listed candidate openai /
  dashscope / anthropic / gemini / xai / ollama / deepseek / moonshot models,
  embeddings and voices. They fed AgentScope's own credential UI, which Raven
  replaced: credentials are configured through `/raven/providers` ->
  `~/.raven/config.json`, so nothing asks AgentScope for a model catalog.

  These catalogs *do* still have a reader, contrary to what this file claimed on
  adoption: `CredentialBase.list_models()` / `list_tts_models()` back
  `GET /model/` and `GET /tts-model/`, so both now answer
  `{"models": [], "total": 0}`. That is inert for the model picker, which reads
  `GET /credential/{id}/models` -> `list_effective_models()` and is served by each
  credential's own `models` overrides. It does mean the TTS voice picker in
  `ModelParametersPopover.tsx` renders its empty state for any credential type
  that would have offered voices. Restore `tts/_dashscope/_cosyvoice_models/` if
  TTS voice selection is ever wanted.

  Safe by construction otherwise: the loader globs a directory (empty list when
  missing) and wraps each parse in `try/except`.

- **Sorted imports** in 224 files (ruff `I001` auto-fix) and removed one malformed
  `# noqa:` directive (bare, no codes). No logic changes.

- **Trimmed to the code this deployment reaches** -- 65 modules, ~15k lines (349
  `.py` files -> 284). See [Trimmed subsystems](#trimmed-subsystems).

## Trimmed subsystems

AgentScope ships every backend it supports; `service/main.py` selects exactly one
of each. The alternatives were removed rather than carried as code that can never
execute here. What went, and what pins the choice:

| Removed | Why it cannot run | Chosen instead |
|---|---|---|
| `workspace/_{docker,e2b,daytona,k8s,opensandbox}` + their `app/workspace_manager/` managers | `main.py` constructs `LocalWorkspaceManager`; the SDKs were never in `requirements.txt` | `LocalWorkspace` |
| `workspace/_sandboxed_base.py`, `_gateway_client.py`, `_gateway_shim.py`, `_mcp_gateway/` | in-sandbox MCP gateway; only the removed sandbox backends launched it | local MCP clients |
| `rag/_vdb/_{milvus_lite,mongodb}.py` | `main.py` constructs `QdrantStore` | `QdrantStore` |
| `app/rag/blob_store/_s3.py` | `main.py` passes no `blob_store` | `LocalBlobStore` (`service/blobs/`) |
| `middleware/_longterm_memory/` (mem0 / ReMe / agentic-memory) | `_service/_chat.py` builds a fixed middleware list; nothing selects these, and `mem0` / `reme` are not dependencies | none |
| `middleware/_tracing/` | never added to the middleware list; dropped the 4 `opentelemetry-*` requirements with it | none |
| `middleware/_budget.py` | never added to the middleware list | none |
| `app/middleware/_protocol/` (AG-UI) | `main.py` passes only `CORSMiddleware`; dropped the `ag-ui-protocol` requirement with it | the native SSE stream (`GET /sessions/{id}/stream`) |
| `model/_openai_response/` + `formatter/_openai_response_formatter.py` | `OpenAICredential.get_chat_model_class()` returns `OpenAIChatModel` | Chat Completions |
| `app/_router/_schema/_mcp.py` | zero references; `_router/_workspace.py` declares its own models | inline models |
| `embedding/_file_cache.py`, `app/storage/_model/_user.py`, `types/_hook.py` | no reference outside their own `__init__` re-export | -- |
| `credential/_kimi.py` | 0-byte file | -- |

Each removal also drops the matching `__init__.py` re-export, so the package's
public surface shrank with it. `workspace/_utils.py` kept only the four layout
constants `workspace/_base.py` reads.

**Before deleting anything else, note that an import graph is not sufficient
evidence.** Two things defeat it:

- **Path-based loading.** `tool/_builtin/_scripts/_glob_helper.py` has no importer
  -- `tool/_builtin/_glob.py` resolves it through `importlib.resources` and runs
  it as a subprocess. It is live. The deleted `_mcp_gateway_app.py` was reached
  the same way, from the sandbox backends.
- **Eager re-export chains.** Package `__init__.py` files import nearly the whole
  tree, so 339 of the original 349 modules loaded at startup. Import-time
  reachability proves almost nothing here; resolve each imported *symbol* through
  the re-export chain to the module that defines it instead.

Runtime confirmation beats both: `GET /knowledge_bases/supported_content_types`
returning text-only media types is what actually proves the non-text document
parsers are unwired.

## Kept despite having no caller

- `rag/_parser/_{pdf,word,excel,ppt,image}.py` and `_utils.py` -- `create_app()`
  defaults `knowledge_parsers` to `[TextParser()]` and `main.py` does not override
  it, so knowledge bases currently accept text only. The parsers are one
  `create_app(knowledge_parsers=[...])` argument away from working and their
  dependencies (`pypdf`, `python-docx`, `python-pptx`, `openpyxl`, `xlrd`,
  `pandas`) are already installed. This is a feature that is switched off, not
  dead weight -- deleting it would be a capability regression.
- `app/message_bus/_redis_message_bus.py` and `app/rag/index_worker/` -- the
  multi-process deployment path (shared bus + out-of-process indexing worker).
  Unused by this single-process stack, but `main.py` documents `RedisMessageBus`
  as the recommended production swap. Their remaining `S3BlobStore` mentions are
  docstring examples only.

## Linted, not restyled

This tree is linted with the rest of the repo. Its own conventions are
grandfathered through `[tool.ruff.lint.per-file-ignores]` in the root
`pyproject.toml` -- bare asserts (`S101`), capitalised locals and functions
(`N806`/`N802`/`N812`), exception names without an `Error` suffix (`N818`), and one
`token` variable ruff reads as a credential (`S105`) -- rather than rewritten.
The 224 `I001` import-sort findings were auto-fixed on adoption; that was verified
safe by re-running the endpoint sweep afterwards (13 endpoints, no 5xx, all 339
modules still importing) before being applied here.

Formatting is the one thing skipped: `ruff-format` excludes this path in
`.pre-commit-config.yaml`, because reformatting would rewrite 248 files and about
4,200 lines with no functional change, and would make comparison against the
public project pointless. If you ever want it formatted, do it as its own commit
so the diff stays reviewable.

## Dependencies

Declared in `service/requirements.txt`, since this package is not installed and
its own metadata is not consulted by any resolver.
