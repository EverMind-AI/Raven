# Sharded reads for the tracing viewer

Status: steps 1 to 4 implemented on this branch. Compression (5, 6) and an
opt-in prune (7) are not. Written before the work and corrected against it, so
the three places reality disagreed with the plan are marked below rather than
quietly edited away.

Measured after the work, on the same store: the session list is 0.045s for
57 KB and one session 0.13s for 102 KB, against 3.4s and 171 MB for the single
payload it replaced.

Every number below was measured on one real store: `~/.raven/traces` at 3.2 GB,
323k artifact files, 270,335 span records across 38 log files, with a raven
actively writing to it.

## What the reader pays after !267

!267 took the panel from unusable to usable by not rebuilding what had not
changed. What it did not do is make a rebuild cheap, and a rebuild still happens
every time a span is appended -- which, with any raven running, is continuously.

| | cost |
|---|---|
| `/api/data`, snapshot reusable | 0.25s |
| `/api/data`, rebuild needed | 3.4s |
| payload | 171 MB |
| `/api/search` | 2.9s (a rebuild plus a scan) |
| resident, idle | ~650 MB (the held body) |

The 3.4s decomposes roughly as: 1.3s parsing every retained span, 0.9s grouping
and building trees, 0.4s normalizing, and ~1.5s serializing 171 MB. Nothing in
that list is a defect. They are all the same thing: **the reader treats the whole
retained history as one document, and rebuilds all of it to answer any question
about any part of it.**

The page then re-parses those 171 MB in the browser (2.3s at the old 325 MB, so
roughly 1.2s now) to render a session list whose visible rows are a few hundred
bytes each.

## The store is already sharded. The reader is not.

Rotation exists and has existed. `TraceStore._rotate_if_needed`
(`raven/tracing/store.py`) moves the active log into `archive/<date>/` daily, or
sooner if it passes `TRACE_LOG_MAX_BYTES` (50 MB default), and `rotateIfNeeded`
in `raven/tracing/viewer/log-store.js` does the same from the JS side. The store
on the machine measured here is already 38 separate files.

The reader throws that away. `readJsonl(kind)` concatenates every file into one
array, `buildSessions` folds that array into one object, and `/api/data` ships
the whole object. The shard boundaries the writer took care to create are
invisible one function later.

So the fix is not a retention policy. Nothing needs deleting. The fix is that
**the reader should read shards.**

## Where to cut, according to the data

The natural instinct is to page by file, since that is what rotation produced.
The data says otherwise:

```
270,335 span records, 307 MB on disk, 38 files, 187 sessions

per-session size:   median 0.05 MB    p90 0.49 MB    max 48.61 MB
files per session:  156 sessions in 1 file
                     16 in 2, 4 in 3, 2 in 4
                      1 each in 6, 8, 16, 17, 21
                      3 in 22, 1 in 23
```

A file boundary is arbitrary -- it falls wherever 50 MB or midnight landed --
and a session can straddle 23 of them. A session, by contrast, is exactly the
unit the page asks about: the left pane lists sessions, and everything else on
screen is downstream of clicking one.

Hence the split this design proposes:

- **index per file**, because a rotated file never changes again, so an index
  over it is computed once and is valid forever;
- **serve per session**, because that is the question the page asks.

Against the median session, that turns a 171 MB response into 50 KB.

## The constraint that decides the design

A span cannot be attributed to a session by looking at the span. Verified on the
measured store:

```
span records                : 270,335
  carrying a session.id     : 118,276     <- 56% carry none
  session.id in uuid form   : 0
distinct session.id         : 187
distinct session.key        : 187
```

`buildSessionIdentityIndex` (`server.js`) exists for this. It counts, across
**every span in the corpus**, which `session.id` each `session.key` appears with,
sorts the candidates by uuid-likeness and then by frequency, and elects a
canonical id per key. `projectSpanForDisplay` then rewrites a span's `sessionId`
through that election. `synthesizeSubagentCallSpans` has the same shape: it
derives `subagent.call` spans from `tool.call` spans, and dedupes them against
the set of `subagent.tool_call_id` values found anywhere in the corpus.

Both are genuinely global. A naive per-file index would therefore be *wrong*,
not merely incomplete: a sidecar built from one file could attribute a span to
one session, and a file rotated a week later could change the election that
attribution depended on.

This is the part of the design worth getting right before writing any code,
because it is invisible until the answers are subtly wrong.

**The resolution: split the index by what actually needs global scope.** What
the election consumes is not spans, it is *counts*. Counts aggregate. So:

- each per-file sidecar stores its own raw, local tallies -- for that file alone,
  `sessionKey -> sessionId -> count`, `sessionId -> {key,agent,workspace} -> count`,
  and the set of `subagent.tool_call_id` values it contains;
- a global pass merges those 38 small tallies and runs the same election. Merging
  aggregates is cheap and, because addition is associative, produces the identical
  result the current whole-corpus pass produces;
- only then does a session id map to a set of files, and only those files are read.

The expensive thing -- spans -- stays on disk in shards. The cheap thing --
counts -- is what goes global. The election's semantics do not change at all,
which is what makes this safe to land under the existing behaviour.

## Design

### 1. A sidecar index per rotated file

One sidecar per log file, under `logs/index/` mirroring the log's own relative
path, so the log tree stays free of files that `isLogName` would have to learn to
ignore.

Contents, per file:

- schema version;
- the source file's size and mtime, which is the whole validity check: mismatch
  means rebuild this one sidecar and nothing else;
- per session appearing in the file: span count, first and last timestamp, and
  the trace ids present;
- the local tallies described above;
- the local `subagent.tool_call_id` set.

Deliberately **not** in it: byte offsets. See the compression section.

A rotated file is immutable, so its sidecar is built once. A full cold build of
all 38 is one parse pass, about 2.4s on the measured store, and never repeats.
The active log is the only mutable file and gets the same treatment in memory,
rescanned when its size or mtime moves -- typically 1 MB, 50 MB at the rotation
ceiling.

Self-healing is not optional here. A missing, unparseable, or stale sidecar must
degrade to "rebuild it", never to "skip that file", or a corrupt sidecar silently
deletes history from the panel. That failure mode -- data quietly missing rather
than an error -- is the same one that made the deposit-walk regression in !267
hard to see, and it deserves a test that plants a corrupt sidecar.

### 2. Two endpoints replace `/api/data`

**Corrected during implementation.** The plan had a list row carry a trace
count, on the assumption that counting distinct visible trace ids reproduces it.
It does not. `buildTraceGroups` groups by parent-child linkage rather than by
trace id, so a session's trace count is the number of spans whose parent is
absent from the session -- which cannot be decided one file at a time, and a
test with a parent and child split across two files caught the index reporting 2
against the reader's 1. Exactness would mean holding every span id and edge in
the index, roughly 18 MB of sidecar for this store, parsed on every list
request. The row carries an exact span count instead and the card reads
"N spans" until the session's own response brings the trace count.

| endpoint | returns | size on the measured store |
|---|---|---|
| `GET /api/sessions` | one row per session: id, key, agent, workspace, trigger, channel, surface, started, updated, span count | 57 KB for 187 rows |
| `GET /api/sessions/:id` | that session's traces, each with metadata and its spans | 50 KB median, 490 KB p90 |

One page consumes this server's `/api/data`
(`raven/tracing/viewer/ui/app.js`), so both sides change in one commit and no
compatibility shim is owed to anyone. Two other callers exist and neither wants
one:

- `tests/integration/test_tracing_viewer_e2e.py` asserts against `/api/data` in
  five places and moves with the endpoints;
- `subagents/*/*/raven/tracing/viewer/` holds four vendored raven trees, each
  with its own matched `server.js` and `ui/app.js`. They serve themselves, so an
  API change here cannot reach them. They are also all still pre-!267 -- none has
  `statLogFiles` -- so whether they should track this tree at all is a question
  for whoever owns the vendoring, and not a dependency of this work.

The snapshot cache from !267 does not disappear, it narrows: the session list is
what gets held and refreshed behind a response, and it is small enough that
holding it costs nothing worth measuring. Per-session responses can be cached
per (session id, fingerprint) with a small LRU.

### 3. Search reads the same index

**Corrected during implementation, and worse than this section assumed.** The
2.6s quoted here was measured on common terms only. A term too rare to reach the
result cap never stops the scan, so it reads every artifact in the store: 106s
for a trace id against 4s for a common word, on the merged tree. The cap that
!267 fixed only ever stopped scans that reach it. Search is also synchronous, so
those seconds are seconds in which nothing else the viewer serves can be
answered -- a fact that shaped the work: a trace id typed into the sidebar is
resolved through the index in 0.017s rather than through search, and that lookup
is issued before the content search so it is not queued behind it.

Step 4 as planned -- `searchSpans` picking candidate files from the index rather
than calling `buildSessions()` -- is still right and still unbuilt. It is no
longer the whole fix.

This also retires the problem !267 could not solve. Two in-memory search indexes
were tried there and both were rejected on measurement: retaining the searchable
text of every span exhausted memory outright, and re-parsing the held body per
query was slower than rebuilding (73s on a broad term). Neither is needed once
the unit of work is a shard rather than the corpus.

### 4. The long-lived sessions

Three sessions on the measured store are ~48 MB spread over 22 files, because
their ids (`s1`, `cli:c`, `tui:default`) are reused across weeks. Per-session
paging alone does not help them: the response is still 48 MB.

So `/api/sessions/:id` should return **trace metadata**, with spans fetched per
trace. The page is already session -> trace -> span, and the sidecar already
knows the trace ids per file, so the third level costs nothing extra to index.
A newest-first window over traces with explicit paging is enough; the point is
that no single response is proportional to a session's whole lifetime.

## Compression, and why it lands after the index

Measured on the same store:

| | now | gzip | ratio |
|---|---|---|---|
| one rotated span log | 50.0 MB | 3.7 MB | **13.6x** |
| span archives in total | 307 MB | 24.4 MB | 12.6x |
| artifacts (600-file sample) | 3.62 MB | 0.88 MB | 4.1x |
| artifacts in total | 3.0 GB | ~730 MB | 4.1x |

A span log compresses 13.6x because JSONL repeats the same attribute keys on
every line. Rotation is the natural moment to compress, since that is exactly
when the file stops being written.

Two consequences that constrain the design above:

1. **gzip is not seekable**, which is why the sidecar stores no byte offsets. The
   index answers *which files*, and a file is then inflated whole and filtered.
   At 50 MB uncompressed per file worst case, and a median session living in one
   file, that is the right trade. Reaching for a block-framed format (zstd
   frames, or per-session gzip members) to regain seeking is a later
   optimization, not a prerequisite.
2. **`isLogName` in `log-store.js` must accept `.log.gz`** alongside `.log`, and
   the reader must handle both for as long as any un-compressed archive remains
   -- which is forever, unless a migration rewrites them. That function was
   narrowed in !267 and is the single place this lands.

Order matters: compression on its own reduces disk and cold-read IO but leaves
the reader parsing the whole corpus, so latency barely moves. The index on its
own fixes latency but leaves 3.2 GB on disk. The index first, compression second.

## What this does not do

It deletes nothing, and after compression it mostly does not need to.

`raven tracing compact` is separate and already exists: it folds duplicate
artifact payloads onto hard links. A dry run on the measured store reports
323,283 artifacts scanned, 241,816 foldable, at least 252 MB reclaimed -- and
that is a floor, since a dry run cannot observe the link counts a real run would
create. Worth running, worth scheduling, and unrelated to everything above.

A retention policy is still worth having, but as an explicit, opt-in
`raven tracing prune --older-than 30d --dry-run` sitting beside `compact` -- not
as a default that quietly removes a session someone was about to look at. After
compression the pressure to reach for it is much lower: the store measured here
would be roughly 760 MB rather than 3.2 GB.

## What landed

Steps 1 to 4 of the table below, as five commits: the per-file index and the
shared election, the two session endpoints, trace ownership and windowed llm
calls, the page fetching per session, and the tests that pin the sharded reader
against the whole-corpus one. `/api/data` stays, unchanged and still verified
byte for byte, because the tests compare against it.

## Order of work

| | change | fixes | risk |
|---|---|---|---|
| 1 | sidecars + global tally merge | correctness foundation for everything below | medium: the identity election must produce identical results, and needs a test comparing old and new attribution over a real corpus |
| 2 | `/api/sessions` + `/api/sessions/:id`, page fetches per session | 171 MB per poll -> ~50 KB | medium: the page moves from one payload to on-demand fetches, so loading states and the refresh model both change |
| 3 | trace-level paging within a session | the 48 MB sessions | low |
| 4 | search reads sidecars | a rebuild per query; not the rare-term scan | low |
| 5 | gzip at rotation, readers accept `.log.gz` | 307 MB -> ~23 MB | low, self-contained |
| 6 | gzip artifacts | 3.0 GB -> ~730 MB | low |
| 7 | `prune`, opt-in | a bound for whoever wants one | low |

## Things that will bite

- **The election must be proven equivalent, not assumed.** Step 1 is a
  refactor of attribution logic that currently sees everything at once. The test
  that matters compares session attribution between the current code and the
  sharded code over a corpus with the awkward shapes in it: spans with no
  `session.id` (56% of the measured store), a `session.key` seen with more than
  one `session.id`, and a `subagent.call` whose parent `tool.call` sits in a
  different file.
- **Nothing under `raven/tracing/viewer/` is covered by a lint or type gate.**
  The root prettier config sets `semi: false`, which those files do not follow,
  and no eslint or tsc target includes them. The only automated coverage is the
  Python tests, so the viewer's e2e suite is where this work gets pinned, and
  anything that changes rendering needs driving in a browser to be believed.
- **The active log is the only mutable input** and every invalidation path has to
  treat it separately from the archives. Conflating them is how a sidecar goes
  stale without anyone noticing.
- **Four vendored copies of this viewer exist under `subagents/` and are
  tracked.** They receive nothing from this work, or from !267, because each
  runs its own server against its own page. If they are meant to stay current,
  that is a separate change with its own risk, and it should be settled before
  someone assumes a fix here reaches every raven in the repo.
- **A per-session cache keyed on a global fingerprint invalidates on every
  append**, which defeats it while raven runs. Key per-session entries on
  something local -- the files that session touches -- or the cache will look
  effective in a quiet test and do nothing in practice. This is the same trap
  !267 hit with its first snapshot cache.
