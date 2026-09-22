# EverOS role configuration - acceptance cases

Companion to `2026-09-21-everos-role-config-design.md`. Every case points back at an
acceptance item (A1..A22) of that document; a case that points at no A tests the
implementation, not the requirement, and does not belong here.

Every case is run by an agent through the entry point a person uses -- the settings page
driven by `playwright-cli`, the CLI wizard, a real gateway over a temporary `RAVEN_HOME`.
A row marked `contract` calls an interface directly and **does not constitute acceptance**;
it is there for a fast mid-development check.

Expected values in the command blocks are **copied from a real run**, never reasoned out.
Until a case has been run, its expectation column reads `(not yet run)`.

## Chapter 0 - shared resources, isolation, teardown

Run this before anything else, and again after any interruption.

**This machine runs several Claude sessions at once.** Ports, the `playwright-cli` browser
session and the `tmux` server are shared and fail silently. Every case below binds its own
port, drives its own named browser session, and kills only pids it recorded.

```bash
# 0.1 - is the port free, and whose is it if not
lsof -tnP -iTCP:18899 -sTCP:LISTEN            # expect: no output
lsof -tnP -iTCP:18901 -sTCP:LISTEN            # expect: no output  (everos under test)

# 0.2 - the operator's own services, to be left alone
lsof -tnP -iTCP:18791 -sTCP:LISTEN            # the operator's everos; record the pid, never kill it
lsof -tnP -iTCP:18792 -sTCP:LISTEN            # the operator's gateway, same

# 0.3 - no leftovers of ours
ls -d /private/tmp/claude-*/-Users-admin-Raven/*/scratchpad/acc7 2>/dev/null   # expect: no output
```

**Teardown, mandatory.** Every case that starts a gateway or an everos server records the
pid and kills **that pid**, never a pattern: `pkill -f "everos server start"` once killed
the operator's own EverOS.

```bash
kill "$GW_PID"; kill "$EVEROS_PID"
until ! nc -z 127.0.0.1 18899 2>/dev/null; do sleep 0.2; done      # drain, then the port is free
rm -rf "$ACC"                                                       # the temporary RAVEN_HOME
```

**The temporary home holds copies of the operator's real API keys.** After deleting it,
scan by content, not by remembered paths:

```bash
# with each real key from ~/.raven/config.json in turn
grep -rl "$KEY" /private/tmp/claude-*/-Users-admin-Raven/*/ 2>/dev/null   # expect: no output
```

### Fixture

`$ACC` is a temporary `RAVEN_HOME` seeded from the operator's config so the providers are
real and the keys work. Two shapes are needed:

| Name | Shape | Used by |
|---|---|---|
| `seed-new` | raven config already carries role blocks; `everos.toml` is the shipped template | chapters 1, 2, 3, 6 |
| `seed-legacy` | raven config has **no** role blocks; `everos.toml` carries `[llm]` `[embedding]` `[rerank]` `[multimodal]` with real values, including a `deepinfra` endpoint raven has no provider row for | chapter 4 |

`seed-legacy` is the shape measured on the author's machine (C20) and is the upgrade case.

**The recorder.** Several cases need to know which address and key the child actually used.
`ps` cannot answer: measured 2026-09-21 on this machine, none of `ps -Eww` / `ps -eww` /
`ps eww` prints another process's environment, and each exits 0 while printing nothing -- a
zero hit that reads like a passing check. (The first attempt at this measurement reported a
hit, which came from the probe script's own source text appearing in `ps` output. Use a
token generated at runtime.)

The address is therefore observed on the wire, which is the layer that owns it: a provider
row whose base url is a local listener logging the request line and the `Authorization`
header into `$ACC/recorder.log`. Record its pid; chapter 0's teardown kills it by pid. A
role pointed at that provider makes the child reveal, by calling it, both the resolved
address and the key -- neither of which raven wrote to any file.

---

## Chapter 1 - a save takes effect

| T | A | Real host | Observation point, and why it cannot lie | When it goes red | Evidence |
|---|---|---|---|---|---|
| **T1.1** | A1 ★ | yes | The everos server's own log line `llm_client_built model=<...>` for the process running after the save. **raven does not write that line** -- everos writes it when it builds the client, so it can only name the model the process actually booted with. Cross-checked against the child's pid, which changed. | The save leaves the old process up (today's bug), or restarts a process that boots with the old model | trigger: the click; process: the log line; effect: old pid != new pid; visible: screenshot of the slot; rerun: the command block |
| **T1.2** | A2 | yes | Per section, at the layer that has the fact. **multimodal**: `multimodal_llm_client_built model=<...>` (verified to exist). **embedding / rerank**: everos logs nothing on their success path (verified -- `component/{embedding,rerank}/accessor.py` only warn on failure), so the new model is read **off the wire**: point the role at the recorder and take the model from the request body. Plus `understand_media` still reports the old multimodal model until raven restarts -- the non-goal, asserted rather than assumed. | A section does not reach the child; or `understand_media` silently follows, which would mean the private singleton was touched (a non-goal) | multimodal: the log line; embedding/rerank: the recorded request; plus the tool's output across a raven restart |
| **T1.3** | A3 | yes | The wizard's own progress line, then the new pid. | The wizard exits without restarting, leaving its own freshly written config inert | trigger: the wizard run; effect: pid before != after |
| **T1.4** | A19 | yes | Change only the provider, to one whose address is the recorder. The recorded request line carries the **new** host and its `Authorization` header the new key. Read on the wire because that is where the pair is used, and **no file on disk carries it** -- it can only have been resolved at spawn. | Changing the provider leaves the old address or key: the resolution is cached, or the model was stored with its endpoint | effect: the recorded request line and header, before and after |
| **T1.5** | A21 | yes | Two saves within a second, with the everos pid sampled every 200ms throughout. The restarts must not **overlap** -- each chain's begin and end are logged with an id, and one chain's interval may not contain another's -- and the survivor must have booted with the **second** save's model. Amended 2026-09-22 per D18: this read "exactly two pids", which assumed the queued save would be absorbed into the running chain. It cannot be. A save that arrives after a chain has read the configuration needs a chain of its own, or the older configuration stays in force -- so a third pid is the correct outcome, and what A21 asks is that the two chains never run at the same time. | Two chains run concurrently (their logged intervals interleave), or the first chain's value wins | effect: the pid samples; the survivor's `llm_client_built` line |

---

## Chapter 2 - failure is reported

| T | A | Real host | Observation point, and why it cannot lie | When it goes red | Evidence |
|---|---|---|---|---|---|
| **T2.1** | A5 ★ | yes | Remove the llm provider's credential, then save. **The everos pid must be unchanged** and a memory write must still land. The pid is the operating system's, not ours; an unchanged pid is proof the stop never happened. | The precheck runs after the stop, or not at all -- then the pid changes and memory is gone | trigger: the save; effect: pid before == after, plus one memory write that lands; visible: the banner text |
| **T2.2** | A4 | yes | The `memory.health` frame on the socket with `ok:false` and a reason, and the banner rendering it. Read the **frame**, not the banner alone: the banner could be painted by an unrelated earlier fault. | A restart fails and the page is never told -- today's silence | process: the frame, captured on the socket; visible: the screenshot |
| **T2.3** | A6 | yes | Two frames in order, `{ok:false}` then `{ok:true, error:null}`, and the banner gone after the second. | Success is not pushed, so the banner stays up forever after any one failure |  the two frames plus before/after screenshots |
| **T2.4** | A22 | contract | With no agent loop, `settings.everosSet` returns `applied:false` with a reason **synchronously**, and no child is spawned. `_safe_loop` returns None only when the factory is absent or raises, which a running gateway does not produce -- a defensive branch with no real-host trigger, marked `contract` rather than dressed up as one. | The RPC backgrounds a restart it cannot report on, so the failure disappears | the RPC reply; effect: pid unchanged |
| **T2.5** | A4 | contract | `stop_pid` returning `STILL_DRAINING` must not fall through to the spawn. Driven by holding the server busy. | The chain spawns anyway, `ensure_everos_server` adopts the old server, and success is reported for a configuration that never took | the call's outcome; the pid |

---

## Chapter 3 - the boundary between the file and the environment

| T | A | Real host | Observation point, and why it cannot lie | When it goes red | Evidence |
|---|---|---|---|---|---|
| **T3.1** | A7 | yes | `sha256` of `everos.toml` before and after a save of each of the four roles. **The filesystem owns this fact**; a hash that did not move is proof raven did not write. | raven still writes any of the four sections | the two hashes, and the `[api]` section still present |
| **T3.2** | A8 | yes | Hand-set `timeout_seconds = 99.0` in `[rerank]`, change the rerank model, then read the running server's effective settings. 99.0 is a value **nothing in raven can produce** -- it exists only in that file. | The environment overrides the whole section rather than per key (C17 falsified in production) | the file line; the server's effective value |
| **T3.3** | A9 | yes | Clear the rerank slot, then read `/health`: `capabilities.rerank` must be false **while `everos.toml` still carries a full `[rerank]` with a working key**. The server computes that flag from its own resolved settings, so false can only mean the empty value really suppressed the file. | The empty value is not emitted, so the stale file section revives and "clear" is a no-op | effect: the `/health` body; the file still holding the old section, shown by `sha256` plus a `grep` |
| **T3.4** | A13 | yes | Export `EVEROS_API__PORT=19999` into the gateway's environment, then let it spawn. The server must bind the port `[api]` names, not 19999. | `_child_env()` stops deleting `EVEROS_API__*` and the bind splits from the health probe | `lsof` on both ports |
| **T3.5** | A20 | contract | A test asserting no file under `raven/` mentions `everos_owned`. | The host learns to judge ownership, which C1 forbids | the test output |

---

## Chapter 4 - migration

Runs against `seed-legacy`, which is the shape measured on the author's machine: two of the
four roles point at a vendor raven carries no provider row for.

| T | A | Real host | Observation point, and why it cannot lie | When it goes red | Evidence |
|---|---|---|---|---|---|
| **T4.1** | A10 ★ | yes | Start the gateway once on `seed-legacy`. Afterwards raven's config carries the four role blocks with the same models, `everos.toml` is **byte-identical** (sha256), and memory still works end to end. | Migration does not run, or runs and drops a role -- for `llm` that silently turns long-term memory off | before/after config; the two hashes; one memory write and recall |
| **T4.2** | A11 | yes | The `deepinfra` rows: after migration `providers.deepinfra` exists, carries the key that used to sit in `everos.toml`, and the embedding and rerank roles resolve through it. **The key's value is the proof** -- it is a string nothing in this flow could invent. | Migration records the role unset because raven had no row, which on this fixture loses embedding and rerank | before/after `providers`; the resolved env for both roles |
| **T4.3** | A12 | yes | Point `[llm]` at an address no provider answers at and no vendor table names. All three of: the migration notice, `raven doctor`, and the role slot must say so. **Three separate outputs** -- one of them going quiet is the failure this case exists for. | Any of the three is silent, which is exactly the "silently unconfigured" the design forbids | the three captured outputs |
| **T4.4** | A10 | yes | Run the migration twice. The second run must change nothing (same config bytes) and must not create a duplicate provider row. | The migration is not idempotent and re-runs on every load | config sha256 after each run |

---

## Chapter 5 - a user-managed root

| T | A | Real host | Observation point, and why it cannot lie | When it goes red | Evidence |
|---|---|---|---|---|---|
| **T5.1** | A14 ★ | yes | With `owned: false` in the slice: the four role slots render disabled; a forced RPC write is refused with a message **naming the root path**; and the everos pid is unchanged after both a save attempt and a gateway start. The path in the message is read from the config, so it cannot be a canned string. | The guard moves out of the write primitive, so a write lands in raven's config and silently never reaches that server | visible: screenshot of the disabled slots; the refusal text; effect: pid unchanged |
| **T5.2** | A14 | yes | On the same root, a gateway start must not spawn. `lsof` on the everos port before and after shows the same pid. | raven spawns against a root it does not own, taking the OME jobstore lock that is not its to take | the two pid readings |

---

## Chapter 6 - rerank, and the read contract

| T | A | Real host | Observation point, and why it cannot lie | When it goes red | Evidence |
|---|---|---|---|---|---|
| **T6.1** | A15 | yes | Configure rerank against SiliconFlow from the settings page, then read the request line the client issues. Observed at the **request**, not in raven: only a real call produces a URL. Weak path: the child's `EVEROS_RERANK__PROVIDER=vllm`. Strong path: a local listener on the rerank base url recording the path. | The protocol is not emitted, so everos falls back to the file's `deepinfra` and issues `POST {base}/{model}` | effect: the recorded request line; weak: the env entry |
| **T6.2** | A16 | yes | Configure rerank against DeepInfra, then make it issue one call. The path it reaches must be the inference endpoint, not the chat one -- the two differ only in the vendor table, so the right one can only come from reading it. | The web path borrows the chat base url, as it does today | effect: the request line, from the recorder or from the vendor's own error text naming the path |
| **T6.3** | A17 | yes | The rerank slot's candidate list next to the vendor table's `supports`. OpenAI and DeepSeek carry keys and must **not** appear. | The filter still asks only "has a key" | visible: the list; the table row |
| **T6.4** | A18 | contract | `settings.everos` against a provider with a key and one without: `api_key_set` follows the credential, and no `provider` field carries `deepinfra` / `vllm` / `dashscope` any more. | The field keeps EverOS's rerank protocol under a name that now means a vendor | the two responses |
| **T6.5** | A18 | yes | The card renders the stored provider without reverse-looking-up a base url -- checked with a provider whose `apiBase` is null, which the old code rendered blank. (This case once asked for a second half on the page, a "key set" indicator; the page renders no such element, so that half moved to T6.6, where it is observable.) | The page still guesses the vendor from the address | visible: the rendered provider name |
| **T6.6** | A18 | yes | `api_key_set` read off the **live socket** -- `settings.everos` over `ws://127.0.0.1:<page>/rpc` through a running gateway -- before and after the page's own `model.disconnect` on that provider, with no edit to any role. The running server is what computes the flag, so only a live reading can catch it computing the wrong one. The same two readings also show `rerank.provider` holding a vendor and never `deepinfra` / `vllm` / `dashscope`. | The flag reports a field raven no longer stores, or the rerank protocol still travels as `provider` | the two responses off the socket, and the role blocks on disk unchanged between them |

---

## Coverage against A1..A22

| A | Cases | A | Cases |
|---|---|---|---|
| A1 | T1.1 | A12 | T4.3 |
| A2 | T1.2 | A13 | T3.4 |
| A3 | T1.3 | A14 | T5.1, T5.2 |
| A4 | T2.2, T2.5 | A15 | T6.1 |
| A5 | T2.1 | A16 | T6.2 |
| A6 | T2.3 | A17 | T6.3 |
| A7 | T3.1 | A18 | T6.4, T6.5, T6.6 |
| A8 | T3.2 | A19 | T1.4 |
| A9 | T3.3 | A20 | T3.5 |
| A10 | T4.1, T4.4 | A21 | T1.5 |
| A11 | T4.2 | A22 | T2.4 |

Every A has at least one case. Four are `contract`, and they are not equivalent:

- **T2.5** (A4) and **T6.4** (A18) each have a real-host case for the same A, so the A is
  accepted on the real host and the contract case only shortens the loop. For A18 that case
  is **T6.6**, which reads `api_key_set` off the live socket: T6.5 covers only the rendered
  provider name, and `Roles.tsx` never reads `api_key_set`, so without T6.6 a running
  gateway could report the flag wrongly with nothing watching.
- **T3.5** (A20) and **T2.4** (A22) are exceptions, stated rather than hidden. A20 is a
  static property of the source tree, for which a repository-wide check *is* the owning
  layer. A22 is a defensive branch a running gateway cannot be driven into. Neither is
  accepted on a real host, and the verdict must say so rather than counting them as passes.

## The star table - run by a person

Four cases. Attention is the scarcest thing here, so a case earns a star only by being one
a machine cannot judge, or one where being wrong cannot be taken back. T2.2 was a star and
lost it: its automated form reads the frame off the socket and screenshots the banner, which
is stronger evidence than a person glancing at the same banner.

| # | Case | Why a person | What to do | What you should see |
|---|---|---|---|---|
| 1 | **T1.1** | The whole point of the change: a saved model actually being used. If only this one works, the change was worth making. | Open settings, change the memory extraction model, wait a few seconds, send a message worth remembering. | The banner reports the restart, and the everos log names the model you picked. |
| 2 | **T2.1** | Getting this wrong destroys memory, and the destruction is silent. A machine can check the pid; only a person can be sure the reasoning was not circular. | Remove the llm provider's key, then save a role change. | The old service keeps running. Memory still works. The banner says why the restart did not happen. |
| 3 | **T4.1** | It runs once, on other people's machines, with no undo. | Start a gateway on the legacy fixture. | Everything still works, and `everos.toml` is untouched. |
| 4 | **T5.1** | Raven writing to, or restarting, a server someone else manages is not reversible from raven's side. | Mark the root user-managed; try to change a role. | The slots are disabled; forcing it names the root path; their server is untouched. |

## Notes for whoever runs these

- **Every case rebuilds its own state and cleans up after itself.** Do not assume the
  previous case left things tidy.
- **Kill by pid.** The teardown block in chapter 0 is the only sanctioned shape.
- **A zero-hit grep means suspect the criterion first.** Four criteria in the previous task's
  acceptance named selectors and fields that did not exist; the code was fine every time.
- **The expectations here are copied from real runs.** Any row still reading
  `(not yet run)` has not been executed and does not count towards anything.
