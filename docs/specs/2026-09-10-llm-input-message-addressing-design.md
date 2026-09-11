# Per-message content addressing for llm.input artifacts - design

Date: 2026-09-10
Status: designed
Base: `main`. Cut a `perf` branch from the latest tip; this work is unrelated
to the branch it was designed on.

## Goal

Every model call records its whole conversation as one artifact payload, so an
agent loop rewrites the same prefix on every turn. `persist_artifact`
deduplicates at the granularity of a full payload sha1, which cannot see that
turn N+1 differs from turn N by one appended message.

Measured on this host, 2026-09-10, over two days of retained artifacts. The
store is live and grew between measurements, so each figure below names its
own snapshot rather than pretending to one.

In a 1,733-artifact snapshot holding 6.794 GiB, 106,247 message occurrences
carried only 6,454 distinct contents - a 93.9% reuse rate. One trace alone
held 27,181 occurrences of 395 distinct messages (98.5%).

Address each message by its content sha1, store one copy, and have the shell
reference it. On the largest recorded artifact this takes 32.906 MiB to
0.063 MiB (99.8%). Retained artifacts averaged ~4.3 GiB/day (8.665 GiB over
two days) and arrive in bursts: between two snapshots taken during one working
session the store gained 73 artifacts and 1.871 GiB, ~26 MiB each. The
distinct-message corpus grows at ~65 MiB/day.

## Preceding step (separate change)

Dropping the unread `historyMessages` view from the payload is a separate,
smaller change on its own branch: over a 1,806-artifact snapshot it takes
8.665 GiB to 5.007 GiB, a 42.2% saving of 3.658 GiB. This design assumes it
has landed, and the shell it produces carries no such derived view. The
single-artifact ladder under Measurements starts from the pre-drop size and
shows that step explicitly, so the two savings are not double-counted.

## Scope

`llm.input` only. In one `du` run over the live store, every other artifact
kind together held 65 MiB - under 1% of what `llm.input` held in the same run -
so a generic chunked-artifact layer is not built. The write API is generic enough to be
reused later without a format change.

## On-disk format

### Message blobs

    <state_dir>/logs/audit-artifacts/_messages/<sha1[:2]>/<sha1>.json

Beside `_blobs/`, never inside it. `compact._sweep_orphan_blobs` walks
`_blobs/` and removes any blob whose `st_nlink == 1` and which is not fresh. A
message blob is referenced by a sha1 written into the shell's JSON text, which
the filesystem cannot see, so its link count is permanently 1: placed under
`_blobs/` the whole message corpus would be swept on the next
`raven tracing compact`.

A sibling directory is not free, and the first review of this change is what
established the cost: `_compact._iter_day_dirs` skipped only `_blobs/`, and
`_messages/<sha1[:2]>/<sha1>.json` has exactly the shape of
`<kind>/<day>/<file>`, so the walk read the message store as an artifact tree -
rehashing the whole retained corpus on every run and hard-linking each message
into `_blobs/`, taking its link count to 2. `compact` therefore excludes both
content-addressed stores by name (`_CONTENT_STORES`), which is one condition
covering both walks that reach the artifact tree. Its "no referenced artifact
is removed, pure convergence" invariant is untouched; what changed is which
directories it considers artifacts at all. `isSafeArtifactPath` admits the
path, since it accepts anything under `audit-artifacts/`.

A blob holds one message serialized as
`json.dumps(message, ensure_ascii=False, default=str)`, key order preserved.

Sorting keys was the first choice, and implementation rejected it: it lets two
orderings of one message share a blob, but it also makes resolution hand back
a reordered copy, and the text fields below carry that order baked inside a
serialized string - so the round trip stops being exact, which is the one
invariant this design rests on. Measured over 406,357 recorded message
occurrences, sorting changed the distinct count by zero (8,837 either way):
messages are built by the same code path every turn, so the trade bought
nothing. The serialization is part of this format's contract and may not
change without a format version bump.

### Shell

    {
      "artifactFormat": "audit.artifact.v2",
      "provider": "openrouter",
      "providerClass": "LiteLLMProvider",
      "model": "openrouter/z-ai/glm-5.3-flash",
      "systemPrompt": {"$msg": "<sha1>"},
      "prompt":       {"$msg": "<sha1>"},
      "messages": [{"$msg": "<sha1>"}, {"$msg": "<sha1>"}, ...],
      "tools": [...]
    }

`artifactFormat` is the discriminator. An artifact without it is v1 and is
read by the old path; v1 artifacts already on disk are never rewritten (see
Non-goals).

A reference is an envelope, `{"$msg": "<sha1>"}`, not a bare sha1 string.
`systemPrompt` is a string in v1, so a bare sha1 there would render as prompt
text in any reader that ignored `artifactFormat` - a silent, plausible-looking
corruption. A dict where a string is expected fails loudly instead. The
envelope also lets one shell mix references and inlined messages, which the
write-failure path below depends on. Cost is ~13 bytes per reference,
~1.4 MiB across the measured corpus.

`systemPrompt` and `prompt` reference the same blob as their element of
`messages`. They are 94% of a messages-only shell (0.913 MiB and 0.149 MiB
against 0.010 MiB of references on the measured artifact), so leaving them as
text would cap the saving at 96.6% instead of 99.8%.

## Write path

### `TraceStore.address_items`

    def address_items(self, items: list[Any]) -> list[Any]:
        """Publish each item under _messages/; return {"$msg": sha1} refs.

        An item whose blob cannot be written comes back verbatim, so one
        filesystem failure costs that item its sharing, not the record.
        """

A separate method rather than a `persist_artifact(address_fields=...)`
parameter. `address_fields` cannot express the `systemPrompt` / `prompt`
aliases: the sha1 is computed inside the store, so a caller cannot fill the
reference in beforehand, and the store cannot know which field aliases which
element. With `address_items` the alias is `refs[i]` - the same object, so
there is no second hashing site that could drift from the first.

`persist_artifact` is unchanged and keeps treating the payload as opaque.
`_publish_blob`, `_blob_is_intact`, `_repair_blob` and the `_verified_blobs`
cache are reused verbatim against the new directory; only the target changes.
`_materialize` is not: it exists to hard-link an artifact path onto a blob, and
a message blob has no second path - it is itself the only file. That is why the
hard-link ceiling cannot be reached here (see Failure modes). `_publish_blob`
still uses one `os.link`, to publish its temp file into place; the link that
accumulates is `_materialize`'s, and it is the one not used.
`spans.address_items` mirrors `spans.persist_artifact`: `_get_store()` plus a
bare `except Exception` return, because tracing must never break the host.

### Caller

`semconv._llm_input_payload` calls `_spans.address_items(msgs)` and builds the
shell, aliasing `refs[sys_idx]` and `refs[user_idx]`. This makes the payload
builder do I/O, which is safe: `trace.py`'s `_close` already wraps the whole
`extract(...)` call - inside which the builder is evaluated - in
`try/except Exception`, so a failure is logged at debug and the traced call is
unaffected.

The extractor runs synchronously in the traced coroutine's `finally`, so its
cost is on the event loop. Measured on the 237-message artifact: 219.8 ms today
(serialize and write 32.9 MiB), 86.3 ms warm (every message already stored),
211.1 ms cold (no message stored yet). The steady state of an agent loop is the
warm case, so this reduces an existing event-loop stall by ~60% rather than
adding one.

### Span attributes

`llm.input.artifact_sha1` becomes the shell's hash and `artifact_bytes` the
shell's size - 64 KiB where it was 32.9 MiB on the measured artifact, of which
the 237 references are ~13 KiB and `tools` the remaining ~41 KiB. Nothing
reads `llm.input`'s
`artifact_bytes` - `server.js:273` reads only the `skill.read` and
`tool.output` ones - but it is the only signal of request size for a human
reading a span, so add `llm.input.content_bytes` carrying the resolved total.
That number is exactly what `artifact_bytes` means today; this moves it rather
than introducing a concept.

Hashing the shell, which itself lists every message sha1, makes the record a
Merkle chain: tamper evidence now localizes to a specific message instead of
only proving that the payload changed somewhere.

## Read path

Two resolution points, both at boundaries that already reformat: `readArtifact`
already parses, and `bundle` already makes a trajectory self-contained.
Resolving there leaves `replay.py`, `cassette.py` and `app.js` with no changes
at all.

### Field rules (identical on both sides)

| field | resolves to |
| --- | --- |
| `messages` | the list of message objects |
| `systemPrompt`, `prompt` | that message's `content`, coerced to text |

The second row is not optional. `app.js:1232` takes
`parsed.systemPrompt || ... || ''` and calls `.match()` on the result; a
resolved message object is truthy and has no `.match`, so resolving that field
to an object throws.

A sha1 read out of a shell must be rejected unless it is 40 hex characters,
before any path is built from it, on both sides. `isSafeArtifactPath` catches
an escape but is the second line, not the first.

### `bundle.py`

After copying a shell into `artifacts/`, resolve it and write the resolved
payload in its place. The bundle therefore holds v1-shaped artifacts and
returns to full size (~33 MiB for one call). That is intentional: the bundle's
promise is that it "survives being copied to another machine". Keeping
references inside a bundle would break that promise, and this is recorded here
so it is not later mistaken for an oversight.

A missing message blob is replaced by a labelled placeholder object at the same
index, and its sha1 is recorded in the manifest's missing list. It must not be
omitted: `replay.py:457` compares message-list length and per-message
role/content, so dropping one element shifts every later message and turns a
locatable gap into a spurious full divergence. `cassette.minimize` already
refuses a bundle that is not fully replayable, so a placeholder propagates
correctly without further work.

### `server.js:readArtifact`

Resolve after `JSON.parse`, before returning, so every caller receives a
v1-shaped `parsed` and `app.js` needs no change. `content` keeps holding the
raw shell text.

## Failure modes

| situation | behaviour |
| --- | --- |
| message blob cannot be written | that message is inlined in the shell |
| concurrent writers, same message | `_publish_blob`'s uuid4 temp name, `os.link`, `FileExistsError: pass` - reused verbatim |
| hard-link ceiling (`EMLINK`) | not applicable; `_materialize`'s accumulating link is not used here |
| blob missing at bundle time | labelled placeholder at the same index, sha1 in the manifest |
| blob missing at viewer time | a visible placeholder renders; no throw |
| torn blob (content does not hash to its name) | repaired on the write path by the reused `_repair_blob`; on the read path detected and reported, never repaired |
| artifact without `artifactFormat` | read by the v1 path, permanently |
| `RAVEN_TRACING=0` | nothing in this path runs |

### Deliberate: the read path does not verify

`_repair_blob` can restore a torn blob only because the write path still holds
the content; a reader does not. Re-hashing every message on every render is
also not affordable, and `readArtifact` performs no verification today either.
So the read path keeps today's behaviour and verification belongs to `compact`
or a future verify command. This is a decision, not an omission.

### Shells are still folded by compact

A shell is an ordinary artifact under `llm.input/<date>/` and `compact` keeps
hashing it and linking it into `_blobs/` exactly as before. At tens of KiB the
gain is small and harmless; no change.

## Non-goals

- **No GC of `_messages/`.** The corpus grows ~65 MiB/day (7,092 messages /
  130.5 MiB over the measured two days, with near-zero cross-day reuse: 1,368
  distinct on day one, 5,735 on day two). This
  can wait (~68x lower than the artifact growth it replaces), and its natural
  trigger is the deletion of a referencing shell -
  that is, the retention tooling `store.py:11` already reserves a place for.
  Adding GC now would mean teaching `compact` to delete, discarding the
  invariant this design was shaped to preserve.
- **No migration of existing artifacts.** Rewriting a v1 artifact would make
  its content stop hashing to the name recorded in `llm.input.artifact_sha1`,
  which is the integrity check `_blob_is_intact` enforces. Existing artifacts
  stay as they are.
- **No extension to other artifact kinds.** See Scope.
- **No performance assertion in the test suite.** The measurements above are
  design evidence; as a gate they would depend on disk state and blob hit rate.

## Testing

The invariant the whole design rests on:

    v1 payload -> address_items -> shell -> resolve == the original v1 payload

byte for byte. Everything else is a boundary case of it.

| # | test | pins |
| --- | --- | --- |
| 1 | v2 shell key set and `artifactFormat` value are frozen | mirrors `test_audit_span_v1_record_shape_is_frozen` |
| 2 | round-trip equality (above) | the design's premise |
| 3 | cross-language round trip: Python's resolution and the viewer's, over HTTP, are byte-identical | one rule, two implementations, in two languages - a divergence here is silent |
| 4 | bundle a v2 artifact; assert the bundle holds v1-shaped artifacts and that the existing replay/cassette tests pass **unmodified** | the "no consumer changes" claim itself |
| 5 | blob write fails (monkeypatched): that message inlines, the record stays complete | write-failure row |
| 6 | a blob is deleted: bundle emits a placeholder and records it; the viewer does not throw | the two missing-blob rows |
| 7 | `{"$msg": "../../etc/passwd"}` is rejected on both sides | sha1 validation |
| 8 | resolved `systemPrompt` is a string, not an object | the `.match()` trap |

Tests 3 and 4 carry the design's real risk; the rest are ordinary boundaries.
Test 3 is written against the existing harness in
`tests/integration/test_tracing_viewer_e2e.py`, which already spawns the real
Node viewer and drives it over HTTP.

Test 4 asserts something unusual: that no existing test needed editing. If
making it pass requires changing a line of the replay or cassette tests, the
boundary-resolution design has failed and the answer is to revisit the design,
not to edit the tests.

## Measurements

All figures from this host, 2026-09-10, over the live store.

The store was live throughout, so figures are grouped by snapshot. Nothing
below is derived from a figure in a different group.

Snapshot A - 1,733 llm.input artifacts, 6.794 GiB:

| quantity | value |
| --- | --- |
| message occurrences / distinct | 106,247 / 6,454 |
| reuse rate | 93.9% |
| largest single trace | 27,181 occurrences / 395 distinct (98.5%) |

Snapshot B - 1,806 llm.input artifacts, serialized as `persist_artifact` does
(`indent=2`):

| quantity | value |
| --- | --- |
| before the `historyMessages` drop | 8.665 GiB |
| after it | 5.007 GiB (42.2%, 3.658 GiB) |

Snapshot C - the distinct-message corpus:

| quantity | value |
| --- | --- |
| distinct messages / bytes | 7,092 / 130.5 MiB |
| distinct appearing on day one / day two | 1,368 / 5,735 |
| implied cross-day overlap | ~11 (1,368 + 5,735 - 7,092) |

One artifact, the largest recorded (237 messages), through both steps:

| stage | size | cumulative |
| --- | --- | --- |
| on disk today | 32.906 MiB | - |
| after the `historyMessages` drop | 17.541 MiB | 46.7% |
| plus `messages` addressed | 1.125 MiB | 96.6% |
| plus `systemPrompt` / `prompt` addressed | 0.063 MiB | 99.8% |

Write cost for that artifact: 219.8 ms today, 86.3 ms warm (every message
already stored), 211.1 ms cold (none stored).
