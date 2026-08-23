# An instance's conversation: what is on disk, and what one read path needs

Context: a colleague's requirement for the sub-agent surface reads, in
translation, "the key thing is to sort out the session files as they exist today
across the main raven, a raven subprocess, a sub-raven and an externally
connected raven -- there are several separate schemes right now". This is that
inventory, the one duplication worth removing, and why removing it is not the
small change it looks like.

## What is on disk

Eight stores. Only one pair answers the same question.

| store | written by | holds |
|---|---|---|
| `<workspace>/sessions/<group>/<chat>.jsonl` | `raven/session/manager.py` | the user's own conversation |
| `.../<chat>/subagents/instances/<agent>/<handle>.jsonl` | `instance_log.append_turn`, through the single funnel `add_turn_to_instance_log` | one instance's whole conversation, across every lane |
| `.../subagents/spawn/<call_id>/` | `SpawnRecord` | `prompt.md`, `out.md`, `error.md`, `meta.json`, `transcript.jsonl` |
| `.../subagents/mas_dag/<run_id>/` | `subagent_dag/_store.py` | per-node prompt and output, `index.json`, `<node>.transcript.jsonl` |
| `.../subagents/direct/<agent>/<handle>/<call_id>/` | `direct_chat.py` | `prompt.md`, `out.md`, `meta.json`, `transcript.jsonl` |
| `<raven_home>/subagent_instances.json` | `subagent/instances.py` | a cross-session index: liveness, status, the external session id. No messages |
| `.../subagents/direct/<agent>/<handle>/messages.json` | `instance_state.py` | an instance's mutable resume state |
| `<tracing state>/logs/acp-frames/` -- `~/.raven/traces/logs/acp-frames` by default, moved by `RAVEN_TRACING_DIR` or `RAVEN_HOME` | `agent/acp/journal.py` (`journal_root`) | connection-scoped protocol traffic, spanning sessions. No messages, and deliberately outside any workspace a sub-agent can read |

## What is not a duplicate

- The **registry** is an index. It answers which instances a session has,
  whether they are alive, and what external session id a resume binds to --
  before any file exists, and across sessions.
- The **ACP journal** is connection-scoped and holds the agent's own requests,
  raven's refusals and its stderr. None of that is a message. It also lives
  outside the workspace, under the tracing state directory, which is the point:
  a sub-agent cannot read its own audit trail. `instance_log.py` already
  measured the trade of copying it into the workspace -- the same record count
  at 78x the bytes -- and chose to point rather than copy.
- **`messages.json`** is resume state: mutable, overwritten, and consulted to
  continue a conversation rather than to show one. Folding it into the
  append-only log would make the log load-bearing for correctness instead of
  only for reading.
- The per-call **`transcript.jsonl`** in the spawn and DAG lanes is keyed by
  *call*, while the log is keyed by *instance*, and each serves a view keyed the
  same way: `subagent_context` reads the spawn one, `subagent_dag/_reader.py` the
  DAG one. `append_turn` writes no call id, so the log cannot answer "what
  happened in call `c123`".

The direct lane's `transcript.jsonl` is the exception: it is written and read by
nothing, since that view reads the log. A genuine duplicate, and a candidate for
deletion on its own -- the other two lanes keep theirs, so removing one makes the
three inconsistent about what they record, which is a judgement worth making
separately.

## The duplication that matters: two read paths

`subagents.instance.history` prefers an instance's log and, when there is none,
stitches the three record directories back together. Two sources for one
question. The second can only fire on a conversation older than the log, so on a
new installation it is unreachable -- while staying coupled to the shape of a run
manifest (it matches on its keys) and to three directory layouts. A reader nobody
exercises, wired to formats that move, does not fail loudly; it fails as an empty
screen, months later.

## Why it is still here

The obvious removal is a migration: write those conversations into their logs
once, then read one place. That was attempted and does not work without a change
the migration cannot make on its own.

`finish` writes a record's terminal metadata and the output file, and *then*
calls `add_turn_to_instance_log`. Those are two separate steps, so from outside
that process a record that reads as finished may or may not have its rows in the
log yet -- and a migration that takes it there writes them itself, leaving the
turn in an append-only file twice. Each of these was tried and each fails:

- **Ask whether anything is running, then act.** Check-then-act: a dispatch
  opening after the check is still read.
- **Reconcile -- add only what the log lacks.** The turn's writer lands after any
  read this side can make, so the same turn is written by both.
- **Refuse when a log appeared meanwhile.** Turns a lost race into permanent
  loss: the winner's turn becomes the whole log, and the next run reads that as
  migrated.
- **Wait until the record is a minute old.** Lowers the probability and closes
  nothing: `SIGSTOP`, a debugger, or starvation can suspend the writer between
  those two steps for longer than any threshold.
- **Order the rows on read instead of on disk.** Breaks the common case: a
  normal tool turn's prompt and closing answer carry append-time clocks while
  its steps carry execution-time ones, so sorting rows individually puts the tool
  episode before the question that caused it.

The pattern in all five is inferring a fact that cannot be inferred. What would
close it is making the terminal write and the log append **one critical
section**, for which this repository already has the idiom: the fcntl-locked
sibling file behind `MemoryStore.locked`, described there as shared across all
processes. With that, "finished" implies "appended", and a migration needs no
heuristic at all.

Alternatively, giving each log row the call id it belongs to would let both
writers deduplicate by construction -- and would also let the spawn and node
detail views read the log, retiring two more files. That is a log format change
with its own migration.

Either is a change to the turn writers, not to the reader, and belongs in its own
review.

## What this change does

The safe part, and nothing that rests on an assumption about a live writer:

1. The stitching moves out of the RPC handler into
   `raven/agent/subagent/instance_records.py`, named as the second source it is,
   with the above recorded where the next person will find it.
2. "Does this instance have a conversation on record" gets one definition,
   `instance_log.message_rows`, asked by the reader -- which also removes a
   second copy of that reader from the RPC module.
3. `_ms_of` rounds instead of truncating. A fractional second has no exact
   binary form, so `1.001 * 1000` is `1000.999...` and every log read had been
   losing the millisecond.

## Testing

- The stitching's rules keep their tests where they were, against the reader:
  which lanes reach one instance, that order comes from each call's recorded
  start, that a DAG node matches by its declared instance or falls back to its
  node id.
- The reader prefers the log when there is one and falls back when there is not.
- `_ms_of` recovers the millisecond it was given, including one whose fraction
  has no exact binary form -- `test_a_log_rows_clock_survives_the_read`, which
  fails against truncation.
