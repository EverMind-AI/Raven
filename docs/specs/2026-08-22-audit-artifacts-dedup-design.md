# Audit-artifact deduplication - design

Date: 2026-08-22
Status: designed
Base: `main`. Cut a `perf` branch from the latest tip; this work is unrelated
to the branch it was designed on.

## Goal

`~/.raven/traces/logs/audit-artifacts/` is append-only and nothing ever folds
or removes what it writes. On the host it was measured on it holds 8.1 GB in
603,629 files after one month. Make the writer store each distinct payload
once, and give the operator a command that applies the same folding to what is
already on disk - without changing the path contract any consumer reads.

`store.py:10` already claims the directory is "SHA1-deduped". It is not. This
change makes that sentence true.

## What is on disk today

`TraceStore.persist_artifact` (`raven/tracing/store.py:126-165`) hashes the
payload, then unconditionally writes a new file at

```
audit-artifacts/<kind>/<YYYY-MM-DD>/<HHMMSS_us>-<traceId>-<sessionId>-<label>-<sha1[:10]>.<ext>
```

The sha1 appears only as a filename suffix. The microsecond prefix makes every
write unique, so identical payloads are stored in full every time. On one day
of `llm.input`, sha1 `bacc491105` has 574 separate copies.

Counting all 603,629 files by `(sha1[:10], size)`, distinct counted within
each kind:

| kind | files | distinct | content MB | dedup |
|---|---|---|---|---|
| `llm.input` | 119,697 | 75,084 | 5,960 | 7% |
| `memory.store` | 40,827 | 26,953 | 79 | 4% |
| `subagent.external.transcript` | 22,447 | 3,991 | 48 | 85% |
| `llm.output` | 119,697 | 6,763 | 32 | 69% |
| `tool.output` | 55,545 | 9,031 | 16 | 42% |
| `tool.input` | 55,545 | 6,482 | 6 | 63% |
| `skill.inject` | 24,530 | 212 | 4 | 99% |
| `turn.input` | 42,650 | 447 | 4 | 87% |
| `memory.recall` | 51,032 | 104 | 1 | 74% |
| `turn.output` | 42,641 | 1 | 1 | 100% |
| others (6 kinds) | 29,018 | 4,367 | 5 | - |
| **total** | **603,629** | **133,435** | **6,156** | **8%** |

Two facts drive the design.

**The 8.1 GB `du` reports is 6.0 GB of content plus 2.1 GB of block slack.**
Most kinds write tiny files that each still occupy a 4 KiB block: `memory.recall`
is 51,032 files holding 1 MB of content and occupying 206 MB. Folding those
51,032 files onto 104 inodes recovers essentially all of it.

**Content dedup alone saves 8%, because the kind holding 97% of the bytes
barely dedups.** `llm.input` is not repeated, it is *accumulating*: call N+1 is
call N plus a few messages, so a session of n calls writes O(n^2) bytes. Only
7% of it is byte-identical to something else.

## Why the ceiling is where it is

The redundancy in `llm.input` is *inside* each file, not between files. Measured
on the three largest traces of 2026-08-21 (10.4 MB of `llm.input`):

| strategy | size | of original |
|---|---|---|
| today (`indent=2` full JSON) | 10.4 MB | 100% |
| compact JSON | 9.7 MB | 93% |
| compact + gzip | 2.9 MB | 28% |
| message-level content addressing + gzip | 0.3 MB | 3% |

Within that sample the `messages` arrays total 4.3 MB but hold only 0.5 MB of
distinct messages (88% redundant), and `systemPrompt` and the `tools` schema are
re-serialised in full on every call.

Reaching 28% or 3% requires giving up the property that `artifact_path` points
at a self-contained plaintext file that `cat` and `json.load()` can read.
`raven/tracing/viewer/server.js:831` (`readArtifact`) and the assertions in
`tests/test_tracing_api.py` and `tests/test_subagent_acp.py:1116` all depend on
it. That property is being kept, so file-level dedup plus slack recovery is the
whole lever, and 8.1 GB -> ~5.8 GB is the ceiling. The blob store this design
introduces is, however, exactly the substrate a later compression or
message-level change would build on: the path contract would not have to move
a second time.

## Design

### 1. On-disk layout

```
audit-artifacts/
  _blobs/<sha1[:2]>/<sha1>.<ext>        # one copy per distinct payload, global
  <kind>/<YYYY-MM-DD>/<existing name>   # hard link to the blob
```

Consumer-visible paths do not change. A hard link is indistinguishable from a
regular file to `open()`, `stat()`, and `fs.readFileSync`, so the file remains
readable, complete, and plaintext.

`_blobs/` lives *inside* `audit-artifacts/` because `isSafeArtifactPath`
(`raven/tracing/viewer/server.js:826`) requires a resolved artifact path to sit
under `ARTIFACTS_DIR`; a sibling directory would be refused. Nothing enumerates
the kind directories - `ARTIFACTS_DIR` is referenced only by that guard - so the
extra directory is not mistaken for a 17th kind.

Blobs are named by the full 40-character sha1, not the 10 characters already in
the filename. Across 603,629 files a 40-bit prefix carries a non-negligible
collision expectation; it is adequate as a locator suffix and not as an identity.

The store is global: one blob is shared across kinds and across days, and the
filesystem's own link count is the reference count - no table has to be
maintained. Note that the entry in `_blobs/` is itself a link, so a blob with
`st_nlink == 1` is one that nothing references any more: deleting every
`<kind>/<day>/` path that pointed at it does not on its own free its blocks.
Reclaiming those is the sweep in section 3, step 8.

Hard links are available: the host's `~/.raven` is ext4 with a 4096-byte block
size, and a probe confirmed `links=2` on a shared inode.

### 2. Write path

`TraceStore.persist_artifact` replaces its `file_path.write_text(...)`
(`raven/tracing/store.py:155`) with a blob-then-link sequence:

```python
blob = self._blob_path(sha1, extension)
try:
    if not blob.exists():
        self._ensure(blob.parent)
        tmp = blob.parent / f"{sha1}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            tmp.write_text(text, encoding="utf-8")
            os.link(tmp, blob)
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)
    os.link(blob, file_path)
except FileExistsError:
    pass
except OSError:
    file_path.write_text(text, encoding="utf-8")
```

The returned dict (`kind`, `path`, `sha1`, `bytes`, `preview`) and every span
attribute produced from it by `artifact_attributes`
(`raven/tracing/store.py:167`) are unchanged.

Properties this relies on:

- **The hot path gets cheaper, not more expensive.** A repeated payload costs
  one `stat` and one `link` instead of a full write. `turn.output` goes from
  42,641 writes to one write and 42,640 links.
- **Concurrent writers are safe.** The gateway, the TUI, and each sub-agent
  share one `~/.raven`. A blob is published from a temp file with `os.link`,
  which fails rather than overwriting if another writer got there first, so the
  blob's inode is never swapped out from under a link already handed to a span.
  The temp name carries a `uuid4`, not just the pid: two writers *in one
  process* would otherwise derive the same temp path, and the second one's
  `write_text` would truncate a blob the first had already published to N
  artifact paths. The write and the publishing `os.link` share one
  `try`/`finally`, so a failed write leaves no orphan temp file. The worst case
  is two writers producing identical temp files, one of which is discarded.
- **The fallback preserves today's behaviour exactly.** A filesystem without
  hard links takes the `OSError` branch and writes the file directly, at the
  same path with the same bytes.

### 3. `raven tracing compact`

`tracing` is deliberately a leaf command rather than a sub-command group, so the
TUI command catalog surfaces it as a plain `/tracing` slash
(`raven/cli/tracing_commands.py:281-289`), and it already takes a positional
action (`stop`, line 298). `compact` is added as a second action. No sub-typer,
no change to the TUI catalog. `("tracing", "compact")` is also added to
`cli.dispatch`'s `_DISPATCH_BLACKLIST` (`raven/rpc/methods/cli_dispatch.py`):
a full store's hash walk can outrun the RPC's timeout, and the backing thread
is not cancelled when that happens. Bare `tracing` and `tracing stop` are
unaffected and stay dispatchable from the TUI.

```
raven tracing compact [--dry-run]
```

For each file under `<kind>/<day>/`, skipping `_blobs/`:

1. Skip anything with an mtime inside the last 60 seconds, so a file currently
   being written is never touched.
2. Read the file and compute the full sha1. The filename prefix is not trusted.
3. If the blob does not exist, `os.link(file, blob)` - the existing file becomes
   the blob, at zero copy cost.
4. If the blob exists and `st_ino` already matches, skip.
5. Otherwise, before trusting a pre-existing blob, confirm its content still
   hashes to its own name. That blob is data this process did not just write,
   so a torn or corrupted one must never silently replace a good artifact. The
   check is cached per run, keyed by the blob's full file name (sha1 plus
   extension, not sha1 alone), so a blob shared by many artifacts is hashed at
   most once. A mismatch is reported in `CompactResult.errors` and that file is
   left untouched.
6. Once the blob is trusted, `os.link(blob, tmp)` in the same directory, then
   `os.replace(tmp, file)`. A blob already holding the filesystem's maximum
   hard-link count (ext4: ~65,000) answers that `os.link` with `EMLINK`; this is
   not treated as an error, since the artifact simply keeps its own copy and
   every other file in the run still converges. Reporting it would only repeat
   the same entry on every later run once a blob is popular enough to hit the
   ceiling.
7. After the walk, first sweep leftover `.compact.tmp` files - the temp link
   staged by step 6 before its `os.replace` - that outlived the run that staged
   them, because the artifact they were about to replace was removed, or the
   walk already short-circuited past it as folded before reaching it again.
   Staleness here is judged by ctime, not mtime: the tmp is always a fresh link
   to an already-existing blob, and linking bumps an inode's ctime while
   leaving its content-modification time untouched, so mtime would read as
   however old the blob's content is rather than how long ago the link was
   made. This sweep runs before the blob sweep below, since a live tmp still
   holds a reference to the blob it points at.
8. Then sweep `_blobs/` and unlink every blob with `st_nlink == 1` whose mtime
   is outside the same 60-second window. Such a blob has no remaining
   reference; the mtime condition avoids the window between publishing a blob
   and linking the first span path to it in the write path, where a live blob
   briefly has a link count of 1. This sweep is what makes removing a day's
   directory by hand actually reclaim space.

`os.replace` is atomic within a directory, so a reader sees either the old file
or the new link, never a partial one. The command is therefore safe to run
against a live Raven and does not require stopping anything. A run is
idempotent in effect - nothing a second run does changes any path's content or
any consumer-visible state - but not in cost: step 2 re-reads and re-hashes
every non-fresh file on every run, folded or not, since no on-disk record
marks a file as already compacted. It never removes a referenced artifact and
never changes a path; the only thing it unlinks is an unreferenced blob.

Output reports files scanned, files folded, and bytes reclaimed. `--dry-run`
reports without writing.

### 4. Expected result

The design was sized against a store measured on 2026-08-22 holding 603,629
files in 8.1 GB. Between design and implementation that store was pruned - the
days from 2026-07-23 to 2026-08-14 are gone - so the figures below are restated
against what is actually on disk. The mechanism and its ratios are unchanged;
only the base is smaller.

Measured (`raven tracing compact --dry-run`, read-only, against the live store):

| | measured |
|---|---|
| paths walked | 280,033 (exactly what `find -type f` counts, so the walk misses nothing) |
| paths that would fold | 218,843 (78%) |
| content that would be reclaimed | 230.8 MB |
| `du` today | 5,709 MB allocated, 4,759 MB apparent - 950 MB of block slack |

Projected after a real run: roughly 61,000 distinct blobs holding ~4,528 MB of
content, plus a much smaller slack tail, so **about 5.6 GB -> about 4.7 GB**.

Note what the two numbers mean. `bytes_reclaimed` is apparent size (`st_size`),
so its 230.8 MB is content dedup only. The larger half of the saving is block
slack, which no byte counter in this design reports: 218,843 of the folded files
are tiny records that each still occupy a 4 KiB block, and folding them onto
shared inodes is what recovers most of the 950 MB. Judge a real run by `du -sh`,
not by the reclaimed figure the command prints.

Three consequences to document for operators:

**Growth is barely affected.** 96% of the remaining bytes are `llm.input`, which
dedups at 7%. Daily `llm.input` volume is dominated by rare long-context runs
rather than steady state: 100 MB on 2026-08-17, 96 MB on 2026-08-19, 297 MB on
2026-08-21, and 3,856 MB on 2026-08-20, when a raven-ppt run produced single
artifacts of 67 MB. Every file in such a spike is distinct, so hard links do
nothing for it. Bounding growth is a separate decision (retention, or relaxing
the plaintext constraint), deliberately not taken here.

**`du -sh <kind>` becomes misleading.** When one blob is referenced from several
kind directories, `du` counts it only on first encounter, so per-kind figures
vary with traversal order. `du -sh audit-artifacts` remains correct because it
deduplicates by inode.

**A linked artifact's mtime and ctime describe its payload, not its own path.**
A file created today that links onto a blob first written 90 days ago inherits
that blob's 90-day-old mtime; a file that has genuinely sat untouched for 90
days but happens to share a payload with something written this morning
inherits today's mtime instead. `find <kind> -mtime +N -delete` run against
such a tree therefore does not prune by path age at all: it deletes artifacts
created today that happen to share an old payload, while sparing hot old ones
that happen to share a new one. Age-based pruning of individual artifact files
is unsafe under this design; prune by removing a whole `<kind>/<day>/`
directory instead, which also lets step 8 above reclaim any blob that day held
the last reference to. The same property means any backup process walking this
tree must be hard-link-aware (`rsync -H`, or a `tar` invocation that preserves
links) or it will silently re-expand every shared blob back into N full-size
copies in the backup.

## Testing

The compaction walk lives in its own module (`raven/tracing/compact.py`) rather
than inside the writer, so it gets its own test file. The two files that cover
existing surfaces are extended, not replaced: per AGENTS.md 5.4 the CLI's tests
stay in `tests/test_cli_tracing_commands.py`.

`tests/test_tracing_api.py`:

- persisting identical payloads twice yields two paths with the same `st_ino`,
  and each path independently reads back the complete payload;
- when `os.link` raises `OSError`, the fallback still produces a correct file at
  the expected path;
- `_blobs/` does not disturb any existing artifact assertion.

`tests/test_tracing_compact.py` (new):

- duplicates are folded onto one inode, distinct payloads are not;
- a second run folds nothing (idempotent);
- files newer than the freshness window are skipped;
- `--dry-run` writes nothing;
- an unreferenced blob is removed, a referenced one and a fresh one are kept;
- `_blobs/` is not walked as if it were a kind.

`tests/test_cli_tracing_commands.py`:

- `raven tracing compact` folds duplicates and reports counts;
- `--dry-run` reports without writing;
- a missing artifacts directory is not an error;
- an unknown action is still rejected with exit code 2.

The seven existing `artifact_path` assertions across `test_tracing_api.py` and
`test_subagent_acp.py` are not modified. Keeping them green is the regression
evidence that the contract did not move.

## Out of scope

- The four vendored `store.py` copies under `subagents/` (raven-code,
  raven-research, raven-ppt, raven-oncall). Those are pinned upstream snapshots
  and follow their own version bumps.
- Automatic pruning or any retention policy. Deleting audit records should be an
  explicit operational decision, not a side effect of a dedup change.
- Compression and message-level content addressing, both excluded by the
  self-contained-plaintext constraint above.

## Rejected alternatives

**Point `artifact_path` at the first file with that sha1 instead of creating a
link.** Saves a directory entry, but the path would then carry another trace's
id and another day's date, and removing an old day would strip the evidence out
from under a newer record. Hard-link reference counting exists precisely to
avoid that.

**Symlinks instead of hard links.** They pass the viewer's guard (`path.resolve`
does not resolve symlinks) and read correctly, but they give no reference
counting: deleting a blob leaves dangling links, and deleting a day frees
nothing.

**Deduplicate per day rather than globally.** Makes per-day removal free the
exact bytes that day owns, but loses the cross-day sharing that collapses
`turn.output` from 42,641 files to one blob, and buys nothing that hard-link
counts do not already provide.

**Drop `indent=2` from the serialisation.** Measured at 7% - not worth making
stored artifacts less readable.
