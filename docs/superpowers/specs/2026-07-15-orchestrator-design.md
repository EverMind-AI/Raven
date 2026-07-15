# Cold-Start Import Orchestrator Design Spec

## Context

Layer 1 (types, Scanner Protocol, ImportState, store metadata) and Layer 2
(ClaudeCodeScanner) are complete. This spec covers Layer 3: the orchestration
layer that connects Scanners to MemoryBackend, managing batching, idempotent
state tracking, and progress reporting.

Approach: a single async function (`run_import`) with no class wrapper. The
caller (CLI layer) is responsible for scanning, tier/platform filtering, user
interaction, and MemoryBackend lifecycle (start/stop). The orchestrator only
does: read -> batch -> store -> track state.

## Public Interface

### `run_import`

```python
async def run_import(
    items: Sequence[tuple[Scanner, ScanResult]],
    backend: MemoryBackend,
    state: ImportState,
) -> ImportSummary:
```

**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| items | Sequence[tuple[Scanner, ScanResult]] | Pre-filtered list from CLI layer (already filtered by tier and platform) |
| backend | MemoryBackend | Already started; caller manages start/stop |
| state | ImportState | Idempotent state tracker for resume support |

Progress is available via `state.get_summary()` (polled by `raven import status`)
and loguru info-level logging for each session.

### Data Types

```python
@dataclass(frozen=True)
class ImportSummary:
    total: int           # len(items)
    submitted: int       # successfully imported this run
    skipped: int         # already imported, skipped via ImportState
    failed: int          # failed this run
    errors: tuple[ImportFailure, ...]

@dataclass(frozen=True)
class ImportFailure:
    platform: str
    source_key: str
    error: str
```

## Main Loop

```
for i, (scanner, result) in enumerate(items):
    if state.is_submitted(platform, key):
        skipped += 1; log "skipping"; continue

    try:
        session = await scanner.read(result)
        await _feed_session(backend, session)
        state.mark_submitted(platform, key)
        submitted += 1; log "imported"
    except Exception as e:
        state.mark_failed(platform, key, str(e))
        failed += 1; log warning "failed"

return ImportSummary(...)
```

Key behaviors:
- Idempotent check before read() to avoid unnecessary file I/O
- mark_submitted only after all batches of a session succeed
- Mid-session crash -> session not marked -> next run re-processes from scratch
- EverOS memorize is append-buffer with extraction dedup, so re-processing is safe

## Batching Strategy (`_feed_session`)

Each ImportSession is fed independently (never mix sessions in a batch). Within
a session, messages are batched by two limits (OR):

| Limit | Value | Role |
|---|---|---|
| Message count | 100 per batch | Primary |
| Character count | 30,000 per batch | Fallback safety net |

Character count is measured by `len(msg_dict["content"])` only (metadata
overhead is negligible). The 30K limit is the conservative CJK bound (EverOS
buffer ~50K English / 30K Chinese).

Single messages never exceed 30K because Scanner layer already truncates
content at 10K chars.

### Algorithm

```
BATCH_MSG_LIMIT = 100
BATCH_CHAR_LIMIT = 30_000

current_batch = []
current_chars = 0

for msg_dict in all_dicts:
    msg_chars = len(msg_dict["content"])
    if current_batch and (
        len(current_batch) >= BATCH_MSG_LIMIT
        or current_chars + msg_chars > BATCH_CHAR_LIMIT
    ):
        store(..., is_final=False)
        reset batch

    current_batch.append(msg_dict)
    current_chars += msg_chars

# Last batch: is_final=True triggers EverOS flush
if current_batch:
    store(..., is_final=True)
```

Empty sessions (no messages) skip store() entirely and are still marked
submitted.

### Message Conversion

ImportMessage -> dict for MemoryBackend.store():

```python
{
    "role": msg.role,
    "content": msg.content,
    "sender_id": msg.sender_id,
    "timestamp": msg.timestamp,
    # optional:
    "tool_calls": list(msg.tool_calls),   # if present
    "tool_call_id": msg.tool_call_id,     # if present
}
```

This dict shape matches what EverosBackend._convert_messages expects. The
backend handles further conversion to EverOS MessageItemDTO format.

## Error Handling

Three error boundaries:

| Source | Handling |
|---|---|
| scanner.read() failure | catch -> mark_failed -> log warning -> continue |
| backend.store() failure | catch -> mark_failed -> log warning -> continue |
| Orchestrator internal bug | Not caught; propagates to CLI layer |

Single-session failure never aborts the overall import.

## Logging

All events at info level or above (cold-start is a low-frequency user-initiated
operation; debug would hinder troubleshooting):

| Event | Level |
|---|---|
| Orchestrator start (total items) | info |
| Session start | info |
| Session success (messages, batches) | info |
| Session skipped (already submitted) | info |
| Session failed | warning |
| Orchestrator end (summary) | info |

## Responsibility Split

| Responsibility | Owner |
|---|---|
| scan() to discover all units | CLI layer |
| Filter by Tier / Platform | CLI layer |
| Display scan results, user confirmation | CLI layer |
| state.set_total() | CLI layer |
| backend.start() / backend.stop() | CLI layer |
| read -> batch -> store -> state tracking | Orchestrator |
| Progress display | CLI layer (reads ImportState or loguru output) |

## Resume (Checkpoint)

Restart-resume at ScanResult granularity:
1. CLI layer re-scans (read-only, fast)
2. CLI layer re-filters and calls run_import with same items
3. Orchestrator checks state.is_submitted() -> skips completed ones
4. Continues from first non-submitted item

Within a session, if the process crashes mid-batch, the session is not marked
submitted. Next run re-processes the entire session. EverOS deduplicates at
extraction time, so duplicate buffer entries are harmless.

Runtime pause (pausing mid-session at batch N) is not supported -- YAGNI. A
single session's batches complete in seconds.

## File Structure

```
raven/importer/orchestrator.py           # ~150 lines
tests/test_importer_orchestrator.py      # tests
```

Data types (ImportSummary, ImportFailure) live in orchestrator.py -- they only
serve the orchestrator and are not independently referenced.

Update `raven/importer/__init__.py` to re-export `run_import`, `ImportSummary`,
`ImportFailure`.

## Dependencies

```
orchestrator.py
  +-- raven.importer.types    (Scanner, ImportSession, ImportMessage, ScanResult)
  +-- raven.importer.state    (ImportState)
  +-- raven.memory_engine.backend  (MemoryBackend -- type annotation only)
  +-- loguru                  (logger)
```

No new external dependencies. No EverOS imports -- orchestrator interacts with
storage exclusively through the MemoryBackend Protocol.

## Test Plan

Mock MemoryBackend + Mock Scanner, verify:

- Batching: message count limit (100) triggers batch split
- Batching: char count fallback (30K) triggers batch split
- Batching: is_final=True only on last batch per session
- Batching: empty session skips store(), still marked submitted
- Idempotent: already-submitted items skipped
- Error isolation: failed session does not abort remaining items
- ImportSummary: total/submitted/skipped/failed counts correct
- Message conversion: tool_calls and tool_call_id pass through
