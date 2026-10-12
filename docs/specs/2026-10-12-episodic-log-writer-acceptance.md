# Episodic log writer: acceptance cases

Companion to `2026-10-12-episodic-log-writer-design.md`. One row per case,
each pointing back at an acceptance item (A1-A16) there. "Real host" means
the agent drives the gateway the way a person does; a row marked "unit" is a
quick check during the work and does not count as acceptance on its own.
Test names are filled in after the code lands; "pending" means the file or
function does not exist yet. Line numbers are against `main` at `829eb28e1`.

## 0. Shared resources and the reset to run first

Every real-host case runs in its own `RAVEN_HOME` under the session
scratchpad, never in `~/.raven`. Before any case, and after any interruption:

```bash
# nothing of ours listening; ports are per home (see table)
for p in 18820 18821 18822 18823 18824 18825 18826 18827 18830 18835 18836; do lsof -nP -iTCP:$p -sTCP:LISTEN && echo "port $p busy"; done
# the control plane falls back to 8765 regardless of --port
lsof -nP -iTCP:8765 -sTCP:LISTEN
# no gateway of ours still alive: kill by pid only, after reading its args
ps -ww -o pid=,lstart=,args= -p "$(cat "$SCRATCH"/elw_*/gateway.pid 2>/dev/null | tr '\n' ' ')" 2>/dev/null
```

| Home | Purpose | `memory.backend` | Ports (rpc / page) | Extra config |
|---|---|---|---|---|
| `elw_native` | T1-T11, T12, T15 | `null`, `plugins.disabled: ["everos-memory"]` | 18820 / 18821 | `memory.annotate_every_turns: 3`; `gateway.heartbeat.enabled: false`; `skillForge.enabled: false`; `sentinel.enabled: false` |
| `elw_heartbeat` | T15 (live variant) | `null` | 18822 / 18823 | as above plus `gateway.heartbeat.enabled: true` with the shortest interval the schema allows |
| `elw_backend` | T13, T14 | `"everos"`, EverOS `owned: true` on 18830, `plugins.config.everos-memory.base_url` pointed at the EverOS-side logging proxy on 18836 | 18824 / 18825 | as `elw_native` for the rest; EverOS root under the home |
| `elw_baseline` | T12's reference capture | `null`, as `elw_native` | 18826 / 18827 | same config as `elw_native`; the gateway binary is `main` at `829eb28e1`, run from a checkout of that commit (`git worktree` or `PYTHONPATH`, the way #440's before/after was driven), confirmed at runtime by printing `render.__file__` |

All three route the agent's provider through a local logging proxy
(`providers.custom.apiBase = http://127.0.0.1:18835/v1`) that writes every
request body to `$SCRATCH/elw_proxy/<ts>.json` and forwards it unchanged to
the real provider, `Authorization` header included. The proxy is the layer
that owns two facts no other surface exposes: the exact system prompt that
went out (T12, T13) and the moment the annotator's request left (T7). The
`custom` provider is the generic OpenAI-compatible gateway
(`raven/providers/per_model_provider.py:26`), so the proxy needs no
protocol translation. The proxy is started with `run_in_background`, holds
a `trap ... EXIT`, and is killed by pid in the teardown of the last case.

The proxy has one control: a file `$SCRATCH/elw_proxy/mode` it re-reads on
every request. `pass` (default) forwards everything. The other modes apply
only to a request whose `tools[0].function.name` is `annotate_conversation`
and leave the agent's own requests untouched: `hold:10` sleeps ten seconds
before forwarding (T7, T8); `fail:500:once` answers one request with HTTP 500
and then resets itself to `pass` (T9); `empty:once` answers one request with
a well-formed `annotate_conversation` tool call whose `episode_summary` is
`[]`, then resets (T10). The case writes the mode before its sends and
asserts the file reads `pass` at its end. The proxy script ships with the
harness under `.work_context/episodic_log_writer/acceptance/` (pending);
it is test tooling, not product code, and no part of the design depends on
it. A second instance of the same script on 18836 fronts EverOS for
`elw_backend`, in `pass` mode only, so T14 can read what `/add` received.

Driving a turn without a browser: `raven gateway --port <rpc> --page-port
<page>` from inside the home's workspace, then over WebSocket at
`http://127.0.0.1:<page>/rpc` with `Origin: http://127.0.0.1:<page>` and the
cookie `raven_session_<page>` (value: `cookie` in `<home>/serve.json`):
`session.create` -> `session_id`; `turn.subscribe {"session_key"}`;
`turn.send {"session_key", "content"}`. `message.complete` marks the reply.

Observation points used below, and where each fact lives:

| Fact | Owner | Read it with |
|---|---|---|
| lines written | the file | `cat <workspace>/user_memory/episodic/episodes.md` |
| the cursor | the session file | `grep '"_type": "metadata"' <workspace>/sessions/<slug>/<chat_id>.jsonl \| tail -1` (`raven/session/manager.py:754-760` writes that record; the last one wins on load, `:551`) |
| the reply moment | the event stream | `message.complete` timestamp from `turn.subscribe` |
| the annotator's request | the proxy | the capture file whose `tools[0].function.name == "annotate_conversation"` (`consolidator.py:1035` names it) |
| the system prompt | the proxy | the first `messages[0].content` of the turn's request |
| a declined or failed annotation | the gateway log | `<home>/gateway.log` (`raven/cli/gateway_commands.py:453-455`), the `episodic annotate` lines the change adds (pending) |
| the profile refresh ran | the offsets file | `<workspace>/user_memory/episodic/.consolidation_offsets.json` (`consolidator.py:1183-1187`) |
| `last_consolidated` untouched | the session file | the same metadata record's sibling field, or `session.status` |

## 1. Cases

| T <- A | Real host | Observation point, and why it cannot lie | Goes red when | Named things exist (command run) | Evidence to collect | ★ |
|---|---|---|---|---|---|---|
| T1 <- A1 | yes (`elw_native`) | Seed a session file by copying one with 12 messages from `~/.raven/workspace/sessions/` into the home's `sessions/<slug>/`, send one turn, read the last metadata record: `annotated_through` must be 12 (the length before that turn, not 14) and `episodes.md` must be empty. The number 12 is in no config and no prompt; only a seed read from the real message count produces it | no seed: the file gains lines from the copied history; or the seed lands at 14 and the turn just sent can never count | `sessions/<group>/<chat_id>.jsonl` layout: `grep -n 'chat_id}.jsonl' raven/session/manager.py` -> `:377`; `"_type": "metadata"` record: `sed -n 754,760p raven/session/manager.py`; `annotated_through`: pending | command, the copied file's line count, the metadata record, `wc -c episodes.md` (0), branch sha, home path | |
| T2 <- A2 | yes (`elw_native`) | With N=3: send three user turns whose content is unrelated to anything in the fixture (three distinct nouns chosen at run time). After the third `message.complete`, poll up to 60 s: `episodes.md` gains at least one line matching `^\[\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}\]` (`consolidator.py:243`), the cursor equals the session's message count, and the proxy holds exactly one `annotate_conversation` request whose `messages` start at a `user` message and end with the third reply. The run-time nouns cannot be guessed by a stale file or a cached response | the step never fires, fires on the wrong boundary, or the cursor is not patched | regex: `sed -n 243p consolidator.py`; `annotate_conversation`: `grep -n annotate_conversation consolidator.py` -> `:1035`; `message.complete`: memory recipe 2026-10-10 | the three sends with timestamps, the captured request body, `episodes.md` before/after, the metadata record, offsets file | ★ |
| T3 <- A3 | yes (`elw_native`) | Fresh session, N=3, send two turns, wait 60 s: no `annotate_conversation` request in the proxy, file unchanged, cursor unchanged | the count is of messages (two turns are four messages) | as T2 | proxy listing (zero matches), file size, metadata record | |
| T4 <- A4 | yes (`elw_native`, `annotate_every_turns: 0`) | Send six turns, wait 60 s: no `annotate_conversation` request, file unchanged, and no `annotated_through` key in any metadata record (the knob short-circuits before the seed) | the seed runs before the knob check, or the knob is not read | `annotate_every_turns`: pending (new field at `raven/config/raven.py:1161` region) | proxy listing, `grep -c annotated_through <jsonl>` (0), config excerpt | |
| T5 <- A5 | yes (`elw_native`) | After T2's state (cursor = m), call `session.undo` with `n: 2` (TUI `/undo`, `ui-tui/src/app/slash/commands/core.ts:902`): the metadata record the undo writes already shows `annotated_through` pulled back to the cut. Then send three turns: the next `annotate_conversation` request's first message is the first user message after the undo cut, not an earlier one; `last_consolidated` in the metadata record is 0 before and after | the cursor is left past the end (the undo's record still shows m, and the three turns after it send nothing), or the step moved `last_consolidated` | `session.undo`: `grep -n 'register("session.undo"' raven/rpc/methods/session.py` -> `:1494`; `undo_last_turn` floor: `sed -n 282,300p raven/session/manager.py` | undo response (`removed`), the two metadata records, the captured request's first message | |
| T6 <- A6 | yes (`elw_native`) | After T2: kill the gateway by pid, start it again on the same home, send one turn: the metadata record's `annotated_through` is still T2's value (not reseeded to the new length, not absent) | the patch was not appended, or a later `save` dropped the key | `append_metadata_patch`: `grep -n 'def append_metadata_patch' raven/session/manager.py` -> `:682`; gateway pid: `<home>/gateway.pid` written by our launcher (pending) | `ps` before kill, the two boot banners in `gateway.log`, the metadata record | ★ |
| T7 <- A7 | yes (`elw_native`) | Hold the proxy for 10 s on `annotate_conversation` requests only (the proxy sleeps before forwarding when the tool name matches). On the third turn, `message.complete` arrives before the proxy's forward timestamp for that request. Two clocks on one machine, both written by processes the step does not control | the step is awaited inline | proxy hold flag: pending (proxy script); `message.complete`: as T2 | the event timestamp, the proxy's receive/forward timestamps | |
| T8 <- A8 | yes (`elw_native`) | With the proxy holding 10 s, send turns 3 and 4 within that window: the proxy records exactly one `annotate_conversation` request, and after release the cursor equals the message count at the time the first task sliced (turn 3's end), then the next qualifying turn picks up from there | no lock (two requests), or the tail is sliced outside the lock (the second request includes turn 3 again) | `get_lock`: `grep -n 'def get_lock' consolidator.py` -> `:1663` | proxy listing (one match), both metadata records | |
| T9 <- A9 | yes (`elw_native`) | Make the proxy answer `annotate_conversation` with HTTP 500 once: the file is unchanged, the cursor is unchanged, `gateway.log` gains the warning line, and the next qualifying turn sends the same tail again (same first message) | the cursor advances on False | warning text: pending (until the text lands, the log clause is checked by the unit twin asserting `logger.warning`'s arguments; the live case then matches the exact line); `chat_with_retry` path: `grep -n chat_with_retry consolidator.py` -> `:1104` | proxy 500 record, log line, two captured requests' first messages | |
| T10 <- A10 | yes (`elw_native`) | Make the proxy answer with a well-formed tool call whose `episode_summary` is `[]`: the file is unchanged, the cursor advances to the message count, and the log carries a debug line with `0` written | the step inspects the file and refuses to move | `n_written` path: `sed -n 1120,1176p consolidator.py` (returns True on zero) | proxy response record, metadata record, log line | |
| T11 <- A11 | unit only, by decision | Monkeypatch `append_metadata_patch` to raise; the task logs a traceback and the next turn in the same test completes. A11 has no real-host case: the only live fault that reaches this `except` is a write failure on the session file, and making that file unwritable also breaks the turn's own `save`, so the live observation would not isolate the branch. This narrows A11 to a unit check and needs the maintainer's word at G2 | the exception reaches the turn | pending test; `logger.exception` site: pending | pytest output | |
| T12 <- A12 | yes (`elw_native`) | The system prompt captured by the proxy for any turn contains both `user_memory/profile/user.md` and the `Episodic log:` line with `(grep-searchable)`, and, after replacing the home's absolute path with `<HOME>` in both captures (the render embeds `home_path`, `render.py:396`, `:423-424`, `:457-458`, so two homes never match literally), byte-for-byte equal to the same block captured from `elw_baseline` | the no-backend branch was touched; also red if the normalisation is skipped, which is the reverse check that the diff is live | current text: `sed -n 420,426p raven/context_engine/segments/render.py`; path embedding: `grep -n 'home_path = ' render.py` -> `:396` | both captures, the normalised pair, `diff` of the identity block (empty) | |
| T13 <- A13 | yes (`elw_backend`) | The system prompt captured for a turn names `episodes.md` exactly once, on a line containing `read-only`, and still contains `never report it to the user as done or as failed`; it does not contain `profile/user.md` | the line is missing, the #894 sentence was dropped, or the profile path crept back | #894 sentence: `sed -n 413,419p render.py`; read-only line: pending | the capture, `grep -c` for each of the three strings | ★ |
| T14 <- A14 | yes (`elw_backend`) | For one turn, the EverOS-side proxy captures exactly one `POST /api/v2/memory/add`, and its body's `messages` (`backend.py:389`) are, message for message, the entries that turn appended to the session file (the jsonl lines after the previous `"_type": "metadata"` record). Same run, two owners of the same fact, so model nondeterminism cannot split them; `_dispatch_backend_store` is unchanged, so the only way they diverge is ordering damage from the new block | the new block disturbs the slice (a missing or extra message, or a second `/add`) | endpoint and body: `grep -n 'memory/add' -B 6 plugins-dist/everos-memory/raven_everos/backend.py` -> `:389` `"messages": payload_messages`; plugin endpoint key: `base_url` in `raven_everos/config.py` | the captured `/add` body, the jsonl slice, the field-by-field comparison | |
| T15 <- A15 | yes (`elw_heartbeat`) + unit | Live: with heartbeat on and N=1, let three heartbeat turns run (count them in `gateway.log`), send no user turn: no `annotate_conversation` request, no `annotated_through` key. Unit: call the helper with each of `SENTINEL`, `CRON`, `HEARTBEAT`, `SUBAGENT` and assert no consolidator call; drive a whole turn with `_req(text, origin=...)` (`tests/test_agent_loop_run_emit.py:156`) and `_stub_edges(loop)` (`:165`), which already take the origin | the origin check admits more than `USER` | `Origin` members: `sed -n 9,16p raven/spine/turn.py`; heartbeat switch: `grep -n heartbeat raven/config/raven.py \| head -3`; heartbeat turns in the log: pending (until the text lands, count them by the `turn.subscribe` events on the heartbeat session instead) | log excerpt with the heartbeat turns, proxy listing, metadata records | |
| T16 <- A16 | yes (`elw_native`) | After T2 on session S (m messages), call `session.branch` (TUI `/branch`, alias `/fork`, `ui-tui/src/app/slash/commands/session.ts:356-358`) to get S2, send one turn on S2: S2's first metadata record with the key reads `annotated_through = m` (the fork length), and after three more turns on S2 the captured request's first message is S2's first own user message | the child inherits no cursor and no seed runs, so the parent's history is in the request | `session.branch`: `grep -n 'register("session.branch"' raven/rpc/methods/session.py` -> `:1497`; fork drops metadata: `sed -n 866,890p raven/session/manager.py` | branch response (child key), S2's metadata records, the captured request | |

Every case starts by asserting its home has no gateway listening on its
ports and no `annotate_conversation` capture newer than the case start, and
ends by killing the gateway it started (by pid from `gateway.pid`) and
removing its capture files. T13 and T14 additionally stop the owned EverOS and the EverOS-side proxy by
pid; T12 stops the `elw_baseline` gateway by pid.

## 2. Unit checks during the work (not acceptance)

| Check | What it pins | File |
|---|---|---|
| each T1-T10 as a pytest with a fake consolidator recording its arguments | the helper's arithmetic and the lock, without a provider | pending (`tests/test_agent_loop_episodic_annotate.py`, to be created) |
| T11 | exception containment | same file |
| T12, T13 as render tests | the two prompt branches | `tests/test_segments.py` (exists; `test_identity_with_a_backend_names_no_memory_file` at `:1`-region asserts `"user_memory" not in seg.text` and must change) |
| reverse check for A4 | delete the `every <= 0` clause: T4's unit twin goes red | same file |

Each unit twin is made red once before it is trusted: remove the guard it
claims to cover, run, confirm red on the asserted symptom, restore, `git
diff` clean.

## 3. Hand-run (★)

| T | Why a person | What they do |
|---|---|---|
| T2 | the main journey; the lines' wording is the product | read the lines the annotator wrote for three turns they typed themselves and say whether each line is a fair one-line summary with sensible tags |
| T6 | irreversible if wrong (a lost cursor replays history) | kill and restart the gateway themselves, send one message, open the session file and find `annotated_through` |
| T13 | wording that an agent might read as permission | read the captured system prompt once and say whether the read-only line could be read as "you may edit" |
