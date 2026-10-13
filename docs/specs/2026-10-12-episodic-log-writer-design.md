# Episodic log writer: a periodic annotator after the turn

Change proposal. Reader: whoever reviews or implements the restoration of the
`episodes.md` writer tracked in #122. After reading, you can say which files
move, by how much, what could silently break, and how each acceptance item is
checked on a live gateway. Line numbers are against `main` at `829eb28e1`.

## Goal

`user_memory/episodic/episodes.md` has no writer on any surface people use.
The only writer, the annotator (`MemoryStore.annotate`,
`raven/memory_engine/consolidate/consolidator.py:985`), runs inside the
token-pressure compaction loop (`maybe_consolidate_by_tokens`, `:1746`), and
all three calls into that loop are gated on `owns_compaction`
(`raven/agent/loop/turn_path.py:2191`, `:2707`,
`raven/rpc/methods/session.py:1077`), which the only shipped context engine
hardcodes to `True` (`raven/context_engine/assembler.py:111`). The annotator
survives as live code with one caller, `/new` typed as message text in a chat
channel (`turn_path.py:2164` -> `consolidate_unconsolidated`,
`consolidator.py:1729`); the web and TUI "new session" buttons go through
`session.create` and never reach it. Six readers read the empty file: the
prompt line (no-backend branch only since #894), two attention producers
(`currently_focused`, `project_rhythm`) and three predictor modules
(`daily_analysis`, `task_discoverer`, `context_assembler`).

This change gives the log a writer that does not depend on compaction: after
a user turn, once enough user turns have accumulated since the last
annotation, the unannotated tail is handed to the annotator. The host's own
memory and the plugin backend run side by side; the plugin is untouched. The
prompt's backend branch names the log again, read-only.

Shapes rejected in discussion: pointing the readers at EverOS (its episodes
carry no `#tag` and no one-line summary, so `project_rhythm` and
`daily_analysis` cannot consume them without reinventing the annotator);
flipping `owns_compaction` (puts the old consolidator back in charge of the
Curator's pointer); deleting the log and its readers (the proactive layer is
parked, not retired); making native and plugin memory mutually exclusive
(withdrawn by the maintainer on 2026-10-12).

## Non-goals

- No memory-writing tool for the agent. A validated `remember` tool is a
  separate change.
- No change to `owns_compaction`, the Curator, `Session.last_consolidated`,
  or the `/new` path.
- No change to the Sentinel readers or to how `# Memory` reads `user.md`
  (its lexical section pick never matches a Chinese message and falls back to
  the whole file; that is a separate defect).
- No change to the EverOS plugin or the `MemoryBackend` contract.
- No annotation of history that predates the writer: an existing session is
  annotated from the first turn after upgrade onward, never back-filled.
- No annotation of machine-initiated turns (`Origin.SENTINEL`, `CRON`,
  `HEARTBEAT`, `SUBAGENT`); only `Origin.USER` turns count and are written.
- No lock on `episodes.md` itself. `append_history` stays a bare append; see
  F5 for why that is acceptable.
- No entry in the self-configuration surface (`raven/config/self_surface.py`,
  `raven/config/live.py`): the knob is read at boot, so changing it needs a
  gateway reload.
- `/compress` copy and the `memory.backend = None` docstring are separate
  branches.

## Constraints

| # | Kind | Constraint | How it is checked |
|---|---|---|---|
| C1 | design | The annotate step never moves `Session.last_consolidated`; its own cursor is `session.metadata["annotated_through"]`, written through `SessionManager.append_metadata_patch`; truncation moves the cursor where it happens (`Session.undo_last_turn` pulls it back to the cut, `Session.clear` drops it), the way the same two methods already treat `last_consolidated` | A5, plus a grep in review: no assignment to `last_consolidated` and no `sessions.save` in the new code |
| C2 | design | The step never splits a turn: the tail it annotates starts and ends on a turn boundary | A2 |
| C3 | design | The step never blocks the reply: it is a tracked background task, the shape of Step 4 at `turn_path.py:2710-2724` | A7 |
| C4 | design | Per session, annotation runs under the consolidator's session lock (`MemoryConsolidator.get_lock`), the same lock `/new` holds; the tail is computed inside the lock, so a task that waited re-reads the cursor and finds nothing to do | A8 |
| C5 | architecture | No new import edge: `raven/agent/loop/_shared.py:68` already imports `raven.memory_engine` | `uv run --frozen --all-extras lint-imports` stays at 10 contracts kept |
| C6 | assumption | `MemoryConsolidator.consolidate_messages` then `maybe_refresh_hot_tags`, under `get_lock(session.key)`, is the write path `/new` runs today (`consolidator.py:1729-1743`) | verified 2026-10-12 against a live gateway, not by reading: reading missed that the annotator's `tool_choice="required"` (`consolidator.py:1111`) is refused by the default model, which made the whole path inert. See the provider repair below |
| C11 | assumption | A forced tool choice reaches the model. It does not on every vendor: DeepSeek's thinking mode answers `HTTP 400: Thinking mode does not support this tool_choice`, and eleven call sites across the repo force a choice | measured 2026-10-12 against the live vendor, three ways (`required` -> 400, `auto` -> tool called, omitted -> tool called); the adapter now relaxes and retries once, which `tests/test_providers_forced_tool_choice.py` pins |
| C7 | assumption | A metadata patch survives a restart: `append_metadata_patch` appends one record and the last record wins on load (`raven/session/manager.py:682-703`, `_scan_file` at `:546`) | verified 2026-10-12 by reading; A6 restarts the gateway and checks the cursor |
| C8 | assumption | The consolidator's provider and model follow a `/model` switch (`raven/agent/loop/wiring.py:1119` calls `set_provider`) | verified 2026-10-12 by reading; not re-tested here |
| C9 | design | `annotate_every_turns = 0` short-circuits the step; the only other conditions are the turn's origin and the count | A4 |
| C10 | design | A cursor that is absent is seeded at `prev_len`, the message count before the turn that just ended, and counting continues in the same pass; so neither an upgraded install nor a forked session hands its history to one provider call, while a new session counts from its first turn | A1 (seed), A16 (fork) |

## Perspective checklist

| # | Perspective | Applies? |
|---|---|---|
| V1 context | Applies, see Participants. The new participant is "a session that existed before the writer": its history is never annotated; only turns from the first one seen onward are. A trajectory replay (`raven/trajectory/replay.py:1155`) calls the turn without an origin and is never annotated. |
| V2 scenarios | Applies, see Paths. |
| V3 structure | Applies, see Placement: one new block and one helper in the after-turn pipeline, no new module. |
| V4 data | Applies, see Data: `episodes.md` gains a writer; the cursor is a new persisted field. |
| V5 state | Applies, see Cursor states. The empty state is "never seen". |
| V6 interaction | Not applicable for new Raven-internal messages: there are none. The step adds one outbound provider call per `annotate_every_turns` user turns (`provider.chat_with_retry`, `consolidator.py:1104`), a path no shipped install has run before; its timeout and failure are F1. |
| V7 deployment | Applies, one row: the step runs inside the gateway process, in the turn's event loop, as a detached task. No new process. |
| V8 failure | Applies, see FMEA. "Another writer changed this data" is a row: `/new` and the eval adapter append to the same file. |
| V9 decisions | Applies, see Decisions. |
| V10 traceability | Applies, see Acceptance. |

## Participants

| Participant | Person or system | Wants | Crosses the boundary via |
|---|---|---|---|
| A reader on web or TUI | person | their conversations summarised into the log without typing `/new` | nothing new; the step fires after their turn |
| The agent | system | to grep the log when it helps | the prompt's read-only line |
| Two attention producers | system | non-empty episodes to count by `#tag` | `MemoryStore.history_file`, unchanged |
| Three predictor modules | system | episodes windowed by day, and a recent tail | `MemoryStore.history_file`, unchanged |
| EverOS plugin | system | the same turn slice it gets today | `_dispatch_backend_store`, unchanged |
| A session that existed before the writer | system | not to have its history replayed into one call | the seed rule (C10) |
| Sentinel, cron and heartbeat | system | their own turns kept out of the log they read | the origin check |
| An ACP host driving this Raven | system | its prompts answered; whether they belong in this instance's log is a deployment question | `turn.send`, which stamps `Origin.USER` -- a parent agent's prompt is indistinguishable from a person's here |

## Paths

| # | Given | When | Then | Acceptance |
|---|---|---|---|---|
| P1 | cursor absent, session has 12 messages from before this turn | a user turn ends | cursor is seeded at 12 (`prev_len`); the turn itself is counted from there and, alone, is below N, so nothing is annotated | A1 |
| P2 | cursor at i, `annotate_every_turns = 5` | the 5th user turn since i ends | the tail `messages[i:]` is annotated; the file gains lines; cursor = `len(messages)` | A2 |
| P3 | cursor at i, 3 user turns since | a user turn ends | nothing is called | A3 |
| P4 | knob = 0 | any turn ends | nothing is called, cursor untouched | A4 |
| P5 | cursor at i | the reader undoes two turns past i, then user turns end | `undo_last_turn` pulled the cursor back to the cut, so the first turn after the undo counts; no re-annotation of removed messages; `last_consolidated` unchanged | A5 |
| P6 | cursor at i | the gateway restarts | cursor is still i on the next turn | A6 |
| P7 | the annotator's provider call takes 10 s | a user turn ends | the reply is delivered before the call resolves | A7 |
| P8 | an annotate task for session S holds the lock | the next user turn on S ends and would qualify | the new task waits, re-reads the cursor the first task wrote, finds fewer than N new turns, and returns without a provider call | A8 |
| P9 | the provider call fails (`consolidate_messages` returns False) | a user turn ends | the cursor does not advance; a warning is logged; the next qualifying turn retries the same tail | A9 |
| P10 | the annotator answers but writes zero lines (`annotate` returns True with nothing kept) | a user turn ends | the cursor advances past the tail; a debug line records zero lines | A10 |
| P11 | `append_metadata_patch` or `maybe_refresh_hot_tags` raises inside the task | a user turn ends | the exception is logged with a traceback and the task ends; nothing is re-raised into the turn | A11 |
| P12 | a backend is wired | the identity prompt is rendered | the backend branch names the log, read-only; the no-backend branch is unchanged | A12, A13 |
| P13 | a backend is wired | a user turn ends | `_dispatch_backend_store` still runs with the same slice | A14 |
| P14 | a turn with `Origin.SENTINEL` (or `CRON`, `HEARTBEAT`, `SUBAGENT`) ends | - | nothing is called; machine-initiated turns neither count nor get written | A15 |
| P15 | session S is forked to S2 | the 5th user turn on S2 ends | S2's cursor was seeded at fork length on first sight; only S2's own turns are annotated | A16 |

## Data

**`episodes.md`** (exists; gains a writer)

| Question | Answer |
|---|---|
| Lives longer than | the session; it is per workspace |
| Who writes it | the annotator through `append_history` (`consolidator.py:719`), from this step, from `/new`, from the eval adapter (default off). `append_history` is a bare `open(..., "a")` + one `write` per entry; `MemoryStore.locked()` (`:647`) locks `user.md.lock` (`:622`) and does not cover this file |
| Reader's relation | the agent greps it; Sentinel parses it line by line with `parse_episode_line` (`:247`); a line that does not match is skipped silently by every reader |

**`session.metadata["annotated_through"]`** (new)

| Field | Type | Produced by | When |
|---|---|---|---|
| `annotated_through` | int, index into `session.messages` | the annotate step, via `append_metadata_patch` | on seed (first sight) and after each annotation that returned True |

| Entity | Created | Updated | Read | Deleted |
|---|---|---|---|---|
| cursor | first time the step sees the session: seeded at `prev_len` | after each `consolidate_messages` that returns True; pulled back to the cut by `Session.undo_last_turn`; read as `min(stored, len(messages))` as a last guard against a hand-edited file | the annotate step | dropped by `Session.clear` (`/new`, `session.clear`), so a cleared session is seen afresh and counts from its first new turn. `Session.fork` (`manager.py:866`) copies messages but not metadata; the child is seeded on first sight |

## Cursor states

| State | Entered when | Observable | Actions | Left when |
|---|---|---|---|---|
| never seen | session created or forked; key absent | file has no line for this session | seed at `len(messages)` | the first user turn after the writer exists |
| annotated through i | seed, or annotation returned True | `metadata.annotated_through == i` | count user turns in `messages[i:]` | annotation returns True again, or undo brings `len(messages) < i` |
| pulled back | undo removed messages past i | `annotated_through == cut_index` in the record the undo's save writes | count from the cut | the next annotation |
| locked | a task for this session holds `get_lock(key)` | a second task on the same key is waiting | the waiter recomputes from the cursor after acquiring | the holder releases |

## Placement

One block in `raven/agent/loop/turn_path.py`, directly after Step 4 (lines
2710-2724). It takes only the origin half of Step 4's guard: Step 4 is `if
self.personalization_enabled and origin is not Origin.SUBAGENT:` (`:2713`);
this step does not follow the personalization switch and admits only
`Origin.USER` (`raven/spine/turn.py:9-16`), which also keeps out the
Sentinel, cron and heartbeat turns the comment at `:2199-2203` says reach
this flow. It runs on the main-turn site only; the `deliver_text` site at
`turn_path.py:3129` does not run it.

Before (the end of Step 4, verbatim, `:2721-2724`):

```python
            _t4 = asyncio.create_task(_post_learn())
            self._consolidation_tasks.add(_t4)
            _t4.add_done_callback(self._consolidation_tasks.discard)
        # ── End Step 4 ──────────────────────────────────────────────────────
```

After (indicative; names are placeholders until written):

```python
        # ── End Step 4 ──────────────────────────────────────────────────────

        if origin is Origin.USER:
            self._maybe_annotate_tail(session, prev_len)
```

with, in the same file:

```python
    def _maybe_annotate_tail(self, session: Session, prev_len: int) -> None:
        every = self._annotate_every_turns          # MemoryConfig.annotate_every_turns
        if every <= 0:
            return

        async def _run() -> None:
            try:
                async with self.memory_consolidator.get_lock(session.key):
                    stored = session.metadata.get("annotated_through")
                    if stored is None:
                        stored = prev_len
                        self._set_annotated_through(session, prev_len)
                    start = min(int(stored), len(session.messages))
                    tail = session.messages[start:]
                    if sum(1 for m in tail if m.get("role") == "user") < every:
                        return
                    if not await self.memory_consolidator.consolidate_messages(tail):
                        logger.warning("episodic annotate for {} declined; cursor kept", session.key)
                        return
                    self._set_annotated_through(session, start + len(tail))
                    await self.memory_consolidator.maybe_refresh_hot_tags()
            except Exception:
                logger.exception("episodic annotate for {} failed", session.key)

        task = asyncio.create_task(_run())
        self._consolidation_tasks.add(task)
        task.add_done_callback(self._consolidation_tasks.discard)

    def _set_annotated_through(self, session: Session, n: int) -> None:
        session.metadata["annotated_through"] = n
        self.sessions.append_metadata_patch(session.key, {"annotated_through": n})
```

`append_metadata_patch` (`manager.py:682`) rather than `sessions.save`: the
task runs detached and can land mid-way through the next turn, and a whole
`save` of this task's copy of the metadata would drop keys the turn wrote in
between (the method's docstring says exactly this). `_consolidation_tasks` is
created at `main.py:650`.

`raven/config/raven.py`, `MemoryConfig` (`:1129`), one field after
`memory_top_k` (`:1161`):

```python
    annotate_every_turns: int = 0
    """How many user turns accumulate before the unannotated tail is handed
    to the annotator and written to ``episodes.md``. ``0``, the default,
    turns the step off: it costs one provider call per cadence, and an
    install that never reads the log should not pay for it. The tail always
    starts and ends on a turn boundary because the step runs after a turn,
    so this is a cadence, not a cut point. Read at boot; a change needs a
    gateway reload."""
```

`raven/context_engine/segments/render.py:413-419`, the backend branch.
Before (verbatim):

```python
    if has_memory_backend:
        home_memory = (
            " — your own skills, not a place for user artifacts.\n"
            "  - Memory is not yours to write: what is relevant is recalled into the `# Memory`\n"
            "    section each turn, and the host persists the conversation after it ends. You\n"
            "    cannot observe that write, so never report it to the user as done or as failed."
        )
```

After (indicative):

```python
    if has_memory_backend:
        home_memory = (
            " — your own skills, not a place for user artifacts.\n"
            "  - Memory is not yours to write: what is relevant is recalled into the `# Memory`\n"
            "    section each turn, and the host persists the conversation after it ends. You\n"
            "    cannot observe that write, so never report it to the user as done or as failed.\n"
            f"  - Episodic log, read-only: {home_path}/user_memory/episodic/episodes.md "
            "(grep-searchable; the host appends to it, you do not)."
        )
```

The no-backend branch (`:420-426`) is unchanged.

Also touched: `raven/providers/openai_responses_provider.py` gains the
repair its Anthropic sibling already has (`anthropic_messages_provider.py:505`):
a 400 that names `tool_choice` while the body forces one relaxes it to `auto`
and retries once. Without it the annotator never writes on the default model.
Eleven call sites force a choice today (two in the consolidator, one in the
behaviours extractor, four in the proactive layer, four in the playbook
compiler), so the repair is placed where all of them pass rather than at the
two this change depends on. Adapters for the other protocols (`litellm`,
`azure`, `codex`) do not carry it; the default model routes to `responses`.

Also touched: `Session.clear` drops the cursor and `Session.undo_last_turn`
pulls it back to the cut (`raven/session/manager.py:276-306`), beside the
same methods' handling of `last_consolidated`; their three callers
(`turn_path.py:2178`, `rpc/methods/session.py:987`, `:1020`) all save
afterwards and need no change.

Not touched: `owns_compaction`, the three guards, `consolidate_unconsolidated`,
`last_consolidated`, `_dispatch_backend_store`, every reader, `Session.fork`.

## Sizing

| Item | Size (estimate) | Depth | Why that depth |
|---|---|---|---|
| after-turn block + two helpers | `turn_path.py`, ~45 lines | deep | runs an LLM call on every install; a wrong cursor re-annotates or skips silently; a blocking await would stall every reply; a missing seed replays a session's whole history in one call |
| config field | `raven.py`, ~7 lines | shallow | additive, default-off, boot-time read |
| prompt line | `render.py`, ~2 lines | deep | shared text every backend-wired install renders; #440 was a wording accident on this exact block |
| tests | 3 files: `tests/test_agent_loop_episodic_annotate.py` (new, ~150 lines), `tests/test_segments.py` (~15 lines changed), `tests/integration/test_episodic_writer_real_llm.py` (new, ~60 lines) | - | see Acceptance |

Dependencies: none. Minimal set: the block, the helpers and the field are the
fix; the prompt line can ship separately, but without it an agent under a
backend never learns the log exists.

Perspective rows this change touches: V2 P1-P15 (new), V4 cursor (new
entity), V5 all four states (new), V8 rows F1-F8 (new), V9 D1-D8. No existing
state table changes: `last_consolidated` keeps its meaning and its one reader
(`undo_last_turn`, `raven/session/manager.py:282`).

## FMEA

| # | Part | Failure | Effect | Detected by | Recovery | Severity |
|---|---|---|---|---|---|---|
| F1 | annotator provider call | times out or errors (`consolidate_messages` returns False) | tail not written | warning log; A9 | cursor kept, next qualifying turn retries | low |
| F2 | annotator | returns lines that do not match `parse_episode_line` | readers skip them silently | none at write time (pre-existing property of the readers); no A | none; out of scope, noted for the `remember` change | low |
| F3 | cursor | undo leaves it past the end | would slice to empty and never fire | A5 | clamp before counting | medium |
| F4 | cursor | gateway dies between `append_history` and the metadata patch | file has the lines, cursor not advanced; next run re-annotates the same tail, duplicating lines | not tested; accepted in D5 | duplicates are harmless to grep and over-count a tag by one batch at most | low |
| F5 | another writer | `/new` or the eval adapter appends while this task appends | entries from two writers interleave in the file | none needed: each `append_history` is one `write` of one blank-line-terminated entry under `O_APPEND`, so entries do not tear; same-session `/new` is serialised by `get_lock` (C4) anyway; no A | none | low |
| F6 | task set | `_consolidation_tasks` is never awaited on shutdown (`main.py:650` is its only non-add/discard reference) | a task in flight at shutdown is cancelled; same as Step 4 today | not detected; no A | pre-existing; the cursor is patched only after a successful write, so a cancelled task loses the call, not the data | low |
| F7 | prompt line | the agent reads "read-only" as a hint and edits the file anyway | a hand-written line that may not parse | A13 is a render check only; behaviour is covered by the ask-mode gate on `edit_file` as in #440; no A | the `remember` change is the real fix | low |
| F8 | task body | `append_metadata_patch` or `maybe_refresh_hot_tags` raises | file may hold the lines with the cursor not advanced (same outcome as F4), or the profile refresh is skipped this round | `logger.exception`; A11 | next qualifying turn retries; nothing is re-raised into a turn | low |
| F9 | first sight | an upgraded install's long session, or a forked child carrying its parent's history | without a seed, one provider call would receive the whole history | A1, A16 | the seed (C10) | medium |

## Acceptance

Derived from Paths (P1-P15), Cursor states (4) and FMEA (F1, F3, F8, F9):
15 paths + 4 states + 4 failures = 23 rows; several collapse onto one item.
Test names marked "to be created" do not exist yet.

| # | Item | Source | Fails when | Test |
|---|---|---|---|---|
| A1 | On first sight of a session that already has messages, the cursor is set to `prev_len` and the annotator is not called for the history | P1, state "never seen", F9, C10 | the seed is missing and the whole history is annotated, or the seed lands at the current length and the first turn of every new session is skipped | `tests/test_agent_loop_episodic_annotate.py::test_first_sight_seeds_the_cursor_and_writes_nothing` (to be created) |
| A2 | After 5 user turns past the cursor, the annotator receives a tail whose first message is `user` and whose last message is the final message of the last turn; the file gains at least one line starting with `[YYYY-MM-DD HH:MM]`; the cursor equals `len(messages)` | P2, state "annotated through i", C2 | the slice starts at the wrong index, or the cursor is not patched | `::test_the_fifth_turn_hands_the_tail_to_the_annotator` (to be created), asserting on the fake consolidator's recorded argument; live: `tests/integration/test_episodic_writer_real_llm.py` (to be created) |
| A3 | With 3 user turns since the cursor, nothing is called | P3 | the count is of messages, not user turns | `::test_three_turns_since_the_cursor_call_nothing` (to be created) |
| A4 | With `annotate_every_turns = 0`, nothing is called after 20 user turns and the cursor is not even seeded | P4, C9 | the knob is not read, or the seed runs before the knob check | `::test_zero_turns_the_step_off` (to be created); reverse check: delete the `every <= 0` clause and this goes red |
| A5 | After undoing past the cursor, the next qualifying turns annotate from the cut; `last_consolidated` is unchanged throughout | P5, state "pulled back", F3, C1 | the cursor stays past the end and swallows the turns written after the undo until the count climbs back, or the wrong pointer is moved | `::test_undo_past_the_cursor_is_clamped` (to be created) |
| A6 | After a gateway restart the cursor is the value patched before | P6, C7 | the patch is not written, or `save` overwrote it | live only, part of the integration file above |
| A7 | The turn's reply is delivered before the annotator's provider call resolves | P7, C3 | the step is awaited inline | `::test_the_reply_does_not_wait_for_the_annotator` (to be created), consolidator stub with a held future |
| A8 | With a task holding the session lock, a second qualifying turn's task waits and then makes no provider call because the cursor moved | P8, state "locked", C4 | the tail is computed outside the lock, or no lock is taken | `::test_a_waiting_task_recomputes_and_does_nothing` (to be created) |
| A9 | When `consolidate_messages` returns False, the cursor does not move and a warning is logged, except for a turn larger than the whole budget, which the cursor moves past | P9, F1 | the cursor is advanced unconditionally, or the exemption covers an ordinary decline | `::test_a_declined_annotation_keeps_the_cursor`, `::test_a_refused_oversized_turn_moves_the_cursor_past_it`, `::test_an_ordinary_decline_still_keeps_the_cursor` |
| A10 | When `consolidate_messages` returns True having written zero lines, the cursor advances past the tail | P10 | the step inspects the file and refuses to move | `::test_zero_lines_still_advance_the_cursor` (to be created) |
| A11 | When `append_metadata_patch` raises inside the task, a traceback is logged and the turn that follows is unaffected | P11, F8 | the exception escapes the task or the next turn fails | `::test_a_failing_patch_is_logged_not_raised` (to be created) |
| A12 | The no-backend identity render still names `user.md` and the `Episodic log:` line exactly as on `main` | P12 | the no-backend branch was touched | `tests/test_segments.py::test_identity_without_a_backend_still_names_both_memory_files` (exists; it asserts both paths and the `Episodic log:` wording) |
| A13 | The backend identity render names `episodes.md` once, with "read-only", and still contains "never report it to the user as done or as failed" | P12 | the line is missing, or the #894 sentence was dropped | `tests/test_segments.py::test_identity_with_a_backend_names_the_log_read_only` (to be created); `test_identity_with_a_backend_names_no_memory_file` (exists) must change: it asserts `"user_memory" not in seg.text`, which this line breaks on purpose, so it becomes "names no profile file" |
| A14 | With a backend wired, `_dispatch_backend_store` receives the same slice it does on `main` after a turn | P13 | the new block disturbs the slice or ordering | `tests/test_agent_loop_backend_dispatch.py` existing dispatch tests stay green (exist) |
| A15 | A turn whose origin is `SENTINEL`, `CRON`, `HEARTBEAT` or `SUBAGENT` neither seeds nor counts nor annotates | P14 | the origin check admits more than `USER` | `::test_machine_turns_are_ignored` (to be created), parametrised over the four origins |
| A16 | A forked session whose parent had 12 messages is seeded at 12 on first sight and annotates only its own turns afterwards | P15, F9, C10 | the child inherits no cursor and no seed runs | `::test_a_forked_session_is_seeded_at_its_length` (to be created) |

Candidates for the hand-run guide: A2 and A6 on the live gateway. Everything
else is an agent-judged assertion. F2, F5, F6 and F7 carry no acceptance
item: F2, F6 and F7 are pre-existing properties this change does not alter,
and F5's safety follows from how `append_history` writes, not from new code.

## Cost

| Who pays | What |
|---|---|
| an install that sets the knob | one annotator LLM call per `annotate_every_turns` user turns, plus a profile refresh call whenever a tag crosses the hot threshold; an install with EverOS wired then runs two extractors over the same conversation |
| every other install | nothing: the default is `0`, which short-circuits before the seed |
| reversibility | back to `annotate_every_turns = 0` stops the step after a gateway reload; the file and cursor are left as they are |
| minimal set | the after-turn block, the helpers and the config field; the prompt line is a separate, smaller commit |

## Decisions

- **The knob ships at `0`, off.** The log has had no writer for the life of
  the project, so no install is waiting on one; turning it on everywhere would
  bill every user for a side provider call per cadence to fill a file they may
  never read. The owner chose cost over reach. Rejected: shipping at 5.
- **The cursor is a new metadata key, not `last_consolidated`.** The gate on
  `owns_compaction` exists to keep a second writer off the Curator's pointer;
  reusing that pointer would reintroduce the conflict the gate prevents, and
  `undo_last_turn` reads it as a floor. Rejected: reuse `last_consolidated`.
- **The cursor is patched, not saved.** A detached task that calls
  `sessions.save` with its own copy of the metadata can drop keys the next
  turn wrote; `append_metadata_patch` exists for exactly this writer.
  Rejected: `sessions.save` from the task.
- **The cadence unit is user turns, not messages or tokens.** The original
  design cut only at `role == "user"` boundaries (`pick_consolidation_boundary`,
  `consolidator.py:1690`); counting turns keeps that unit, and the step's
  placement after a turn makes the boundary free. Rejected: 20 messages (a
  tool-heavy turn is 30 messages, a chat turn is 2); a token budget (a light
  user never reaches it).
- **A session is seeded at the length before its first seen turn; history is
  not back-filled.** The original loop fed the annotator chunks bounded by a
  token budget; this step has no chunking, so an unseeded long session would go
  to the provider in one call. Seeding at `prev_len` rather than the current
  length lets a brand-new session count its first turn. Seeding also covers
  forks, which copy messages but not metadata. Rejected: chunked back-fill (a
  second cut rule, for a one-time event); seeding at the current length (the
  first turn of every session would never be annotated).
- **Truncation moves the cursor where it happens, not lazily.** Clamping to
  `len(messages)` at the next turn keeps the stale index on disk, and every
  turn written after an undo or a clear is swallowed until the count climbs
  past it; the first draft of this document said exactly that and the unit
  twin of A5 refused it. `Session.undo_last_turn` and `Session.clear` already
  own `last_consolidated` the same way. Rejected: the lazy clamp.
- **A crash between the file write and the metadata patch duplicates one tail
  (F4) and is accepted.** Making it atomic means writing the cursor into the
  same file under the same lock; duplicates cost nothing to grep and over-count
  a tag by one batch at most. Rejected: a two-phase write.
- **A refused turn larger than the whole bound advances the cursor anyway.**
  That turn is sent whole, because sending nothing would stall the cursor on
  it forever, and nothing records that it was sent. Keeping the cursor on the
  refusal therefore does not retry the call later; it rebuilds the same tail
  and sends the same oversized prompt on every qualifying turn after it, which
  is the self-reinforcing retry the bound was added to stop. An ordinary
  decline still keeps the cursor: that one may have been the provider rather
  than the size. Rejected: one rule for both, in either direction.
- **`True` with zero lines advances the cursor.** The annotator returning
  True with nothing kept means it read the tail and found nothing worth an
  episode (or dropped every candidate as process-only); that is a verdict, not
  a failure, and retrying the same tail would ask the same question again.
  Rejected: inspecting the file for growth before moving the cursor.
- **Only `Origin.USER` turns are annotated.** Sentinel, cron and heartbeat
  turns are machine-initiated, and two of the file's readers are Sentinel
  producers; writing those turns into the log would feed Sentinel its own
  output. Rejected: excluding only `SUBAGENT`, as Step 4 does.
- **No gate other than the knob, the origin and the count.** Native and
  plugin memory run side by side by the maintainer's decision; a `backend is
  None or sentinel.enabled` gate was proposed to avoid double extraction and
  withdrawn. Rejected: that gate.
- **The backend branch names the log, read-only.** Native memory was never
  designed for the model to write: the only policy text about it is the #894
  sentence, every writer is host-side, and `_PROTECTED_SUBTREES`
  (`raven/agent/workdir.py:23`) treats agent writes there as damage. Naming
  the log for reading restores the original intent; #440 came from "your own
  memory" plus a profile path, which this line does not repeat. Rejected:
  leaving the backend branch silent (the log would be written and invisible).

## Open

- Whether `maybe_refresh_hot_tags` should run on every successful annotation
  or only every k-th. It is cheap when no tag is hot; left as every time until
  cost shows.
- The read-only wording. Any phrasing that an agent reads as permission to
  edit reopens #440; the one above is a draft for the G1 read.
- Whether the ACP entrance deserves an origin of its own. Today it reaches
  `turn.send` as `Origin.USER`, so an ACP Raven sharing `~/.raven` mixes its
  parent's prompts into the reader's log, where two Sentinel producers read
  them as the reader's own activity. Not new -- `_dispatch_backend_store` is
  unconditional on origin the same way -- and the alternative, a deployment
  rule giving each ACP instance its own `RAVEN_HOME`, is already the correct
  setup for a separate agent.
