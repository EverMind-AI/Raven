# EverOS role configuration moves to raven, and takes effect when it is saved

Status: built; G1, G2 and G3 passed 2026-09-21, G4 self-review 2026-09-22
Date: 2026-09-21, amended 2026-09-22

## Terms used throughout

New names this change introduces, to be added to `CONTEXT.md` in the same batch
(AGENTS.md section 6).

| Term | Means |
|---|---|
| **EverOS role** | One of the four models EverOS talks to: `llm` (memory extraction), `embedding`, `rerank`, `multimodal`. Not the "Memory role" of `CONTEXT.md` Window Shrink, which is a message role. |
| **role pin** | raven's record of one EverOS role: `{model, provider}`, no credential. Lives in `plugins.config['everos-memory']`, except the embedding pin. |
| **embedding pin** | raven's top-level `embedding` block. Predates this change, serves knowledge bases too. _Avoid_: "pinned", which in `CONTEXT.md` is a Manifest marker. |
| **role slot** | The settings page control that edits one role. |
| **rerank protocol** | EverOS's `rerank.provider` -- which client implementation to build (`deepinfra` / `vllm` / `dashscope`), i.e. the request shape. Deliberately renamed here because it collides with raven's `provider`, which names a vendor. |

## Goal

Two things that turn out to be one change.

**A saved configuration must take effect.** Changing an EverOS role model from the
web settings page writes to disk and then does nothing, forever. Restarting raven does not
help: the EverOS server is spawned with `start_new_session=True`, `EverosBackend.stop()`
does not kill it, and the next `ensure_everos_server` adopts the healthy server it finds.
EverOS builds its LLM client in the API lifespan, so a running process keeps the models it
booted with. `gateway.reload` takes the same adoption path and is equally inert. The CLI
wizard already solves this (`_stop_for_reload`, added 2026-09-16); the web write path,
about a month older, never learned.

**One source of truth for what raven manages.** Today the embedding endpoint lives in
raven's own config while llm, rerank and multimodal live in `everos.toml` -- which raven
writes, api keys and all. Credentials sit on disk in two places, changing a vendor means
rewriting three base urls and three keys by hand, and raven edits a file that belongs to
whoever manages that root.

After this change raven records `{model, provider}` for all four roles in its own config and
hands them to the spawned server as `EVEROS_<SECTION>__*` environment variables. On a root
raven owns, raven emits all four every time: a role it holds is sent, a role it does not
hold is sent empty, which suppresses whatever the file says. `everos.toml` is never written
for those sections again and never read back for them.

Shapes rejected during exploration:

- **Only fix the restart.** Leaves two config sources and the credential duplication, and
  the restart would be written against whichever source wins -- work the move redoes.
- **Only move the config.** Does not fix the bug: environment variables are frozen at spawn
  and `load_settings()` is cached, so a changed value still needs the process restarted.
- **Stop writing `everos.toml` entirely.** Rejected by the owner: raven still creates the
  file from the template and owns `[api]`, the authority on where that root's server listens.
- **Report a restart failure through the plugin's `notify`.** The RPC stack wires
  `notify=lambda m: print(m, file=sys.stderr)`, so the settings page would never see it.
- **Emit environment variables only for roles raven holds.** Rejected after review: a role
  cleared in the UI would then fall back to the stale section left in the file, so "clear"
  would be a silent no-op, and an upgraded install would keep an old key in force.

## Non-goals

- **Not redesigning the onboarding wizard.** Its flow, prompts and probes stay; only the
  source it reads and writes changes.
- **Not touching EverOS itself.** It is a third-party package; this change adapts to its
  documented source order rather than altering it. In particular nothing reaches into
  `everos.component.llm.client._multimodal_client` or any other private global.
- **Not making `understand_media` follow a multimodal change live.** That tool runs inside
  raven's process and everos resolves its client from a cached module singleton with no
  injection point (`component/parser/_core.py` calls `get_multimodal_llm_client()` itself,
  and `enrich_content_items` takes no client). It picks the new model up at the next raven
  start; the child process that does memory extraction gets it immediately.
- **Not taking over a user-managed root.** When `everos_owned()` is false raven neither
  writes the role config nor starts or stops the server, and the environment variables never
  reach it. Intended, not a gap.
- **Not moving the embedding pin.** It stays a top-level block: a global capability --
  knowledge bases embed with it too -- rather than something memory owns.
- **Not moving the runtime knobs.** `timeout_seconds`, `max_retries`, `batch_size`,
  `max_concurrent`, `max_concurrency`, `file_uri_allow_dirs`, `file_uri_max_bytes` stay in
  `everos.toml`, where the template and the operator set them.
- **Not merging the two reverse lookups.** The wizard's `_match_provider_by_url` and
  `provider_serving_at` stay separate; only the latter is widened.
- **Not changing the shape of `raven doctor --json`.** New findings are text, not new fields.
- **Not removing the "this endpoint is managed by exported variables" detection**
  (`embedding_is_env_managed`, `PROVENANCE_ENV`). An operator who exports `EVEROS_*` outranks
  raven, and the settings page keeps refusing a save it could not honour.
- **Not adding a CI gate script for one grep.** The architecture constraints are checked by
  pytest assertions in the existing suite.

## Constraints

Referenced by number from the plan and the deviations log; numbers do not change once used.

### Architecture

| # | Constraint | How to check |
|---|---|---|
| C1 | Ownership stays the plugin's judgement. Nothing under `raven/` decides, or re-derives, whether raven owns the root. | `tests/test_rpc_settings.py` asserts `"everos_owned" not in Path("raven").rglob("*.py") read` and that `settings.everos` gets `owned` from the plugin's `describe_roles()` |
| C2 | A role pin names a model and a provider and holds no credential. | `raven-plugin.toml` declares `llm` / `rerank` / `multimodal` as `object` in `[plugin.config_schema]` (otherwise every key logs "not in its config_schema"); a test asserts the written slice has no `api_key` / `base_url` key. A7 checks the file |
| C3 | No new runtime dependency, in the host or the plugin. | `git diff pyproject.toml plugins-dist/everos-memory/pyproject.toml uv.lock` adds nothing under `[project.dependencies]` |
| C4 | The installed `everos` package is unchanged. | `uv pip show everos` reports the same version before and after; no file under `site-packages/everos/` is modified (it is outside the repo, so `git diff` cannot say) |
| C5 | The host forwards; the plugin owns the keys. `console.py` holds no role field-name constants and does not assemble the slice. | `_EVEROS_FIELDS` and `_EVEROS_REQUIRED` are gone from `console.py`; it calls the plugin's `set_role()` / `describe_roles()` and nothing else |
| C6 | Every subprocess raven spawns receives a filtered environment, so binding `EVEROS_*` in-process does not hand credentials to a model-run command. | `raven/sandbox/direct_executor.py:_ENV_ALLOWLIST` (verified 2026-09-21: locale and runtime basics only, no `EVEROS_*`). The other spawn sites -- `agent/subagent/backends/cli_agent.py`, `agent/tools/media_gen.py`, `importer/scanners/hermes.py`, `utils/office.py` -- are enumerated and each either allowlists or is documented as inheriting |

### Design

| # | Constraint | How to check |
|---|---|---|
| C7 | raven keeps creating `everos.toml` from the template and keeps owning `[api]`; `_child_env()` keeps deleting every `EVEROS_API__*`. Where a server listens is the file's decision. | A13 |
| C8 | raven writes none of `[llm]`, `[embedding]`, `[rerank]`, `[multimodal]` in `everos.toml`, and `set_everos_section` can no longer address them. | `WRITABLE_SECTIONS == ("api",)`; a test asserts `set_everos_section("llm", ...)` raises `KeyError`; A7 |
| C9 | On a root raven owns, every role raven manages is emitted on each spawn -- held ones with values, unheld ones empty. A role an operator exported for themselves is skipped whole, values and blanks alike: raven cannot edit a shell, and blanking what somebody else set would be the one thing worse than ignoring it. Amended 2026-09-22 per D2; the original wording said all four unconditionally. | A9 |
| C10 | The runtime knobs in `everos.toml` survive a model change. | A8, and the spike in `.work_context/everos_role_config_moves_to_raven/spikes/env_merge.py` |
| C11 | The spawn precheck runs before the running server is stopped, so a restart that cannot succeed leaves the old server up. | A5 |
| C12 | Every restart outcome reaches the page, success included. | A4, A6 |
| C13 | On a user-managed root raven neither writes the role config nor starts or stops the server. | A12, A14 |
| C14 | Migration runs automatically, without a command, and is the plugin's code throughout. Scheduled from `EverosBackend.start()` rather than raven's config migrations: the host may know this plugin only through the plugin contract, and a branch in `loader.py` calling into it is the host importing the plugin, which `tests/test_plugin_boundary.py` refuses. Amended 2026-09-22 per D10. | A10; `tests/test_everos_backend.py` covers the scheduling |
| C15 | The gate `everos_role_configured(section)` reads raven's config and answers: a model, a provider, and that provider resolving to a usable credential -- the same rule `api_key_set` reports. | A11, A18 |

### Assumptions

| # | Assumption | How to check |
|---|---|---|
| C16 | EverOS resolves settings as `init_args > env_vars > everos.toml > default.toml`. | Verified 2026-09-21: `everos/config/settings.py` module docstring and `settings_customise_sources` |
| C17 | Environment variables merge **per key**: sending `EVEROS_RERANK__MODEL` leaves `timeout_seconds` from the file intact. | Verified 2026-09-21 by `spikes/env_merge.py` (re-runnable): `timeout_seconds=99.0`, `batch_size=77`, `provider='vllm'`, `max_concurrency=11` all survived |
| C18 | An **empty** environment value suppresses the file's value, so a cleared role is really cleared. | Verified 2026-09-21 by the same spike: with `EVEROS_RERANK__MODEL=""` the factory raises `Rerank model is not configured`, while `timeout_seconds=99.0` still survives |
| C19 | The `memory.health` push reaches the settings page. | Verified statically 2026-09-21: `organ_glue._emit_mcp_event` -> `rpc/bootstrap.py:_mcp_event` -> `send_frame`, documented there as "broadcast rather than conversation-scoped"; received by `ui-web/src/app/install.ts` (`NOTIFICATION_METHODS`, not the ACP `SIDE_CHANNEL_METHODS`) and rendered by `state/banner.ts`. A4 confirms it on a real host |
| C20 | **Falsified.** The reverse lookup does *not* name a provider for every value raven wrote. | Measured 2026-09-21 on the author's machine: `provider_serving_at` returns `None` for `[embedding]` (`api.deepinfra.com/v1/openai`) and `[rerank]` (`.../v1/inference`), because raven carries no `deepinfra` provider row at all -- the wizard has its own vendor table and lets a key be typed for a vendor raven does not know. Two of four roles. The design accounts for this (see Migration) rather than assuming it away |
| C21 | LiteLLM knows `deepinfra`, `vllm` and `dashscope`, so raven can create a real provider row for a vendor it carries no spec for. | Verified 2026-09-21: `_litellm_knows` returns True for all three; `_provider_schema_cls` documents this path ("mistral and xai are supported that way") |
| C22 | The wizard's vendor table is the correct mapping from a vendor to the rerank protocol and rerank base url. | Verified 2026-09-21 for DeepInfra against this machine's wizard-written `[rerank]`. **`siliconflow` -> `vllm` and `dashscope` -> `dashscope` are unverified**; A15 covers one of them |

## Acceptance list

★ marks a candidate for the human-run table; the final set is fixed in the acceptance
document.

| # | The result | Evidence |
|---|---|---|
| A1 | Changing the memory extraction model on the settings page takes effect without restarting raven. ★ | The everos server log's `llm_client_built model=...` line for the restarted process names the new model |
| A2 | The same holds for the embedding and rerank slots. For multimodal it holds for memory extraction; `understand_media` in raven's process follows at the next raven start. | Same log line per section; for `understand_media`, the tool's error or output before and after a raven restart |
| A3 | The CLI wizard applies a configuration change before it exits, saying it is restarting the service. | The wizard's own output line, and the new pid |
| A4 | A restart that fails reaches the page: a `memory.health` push with `ok:false` and a reason, rendered by the standing banner. ★ | The frame on the socket, plus a screenshot of the banner |
| A5 | When the precheck fails the running server is **not** stopped and memory keeps working. ★ | Constructed by removing the llm provider's credential (the inotify branch is Linux-only and is not exercised on macOS); pid unchanged before and after; a memory write still lands |
| A6 | A failed restart's banner clears once a later restart succeeds. | Two frames in order: `{ok:false,...}` then `{ok:true,error:null}`; screenshot after |
| A7 | After a save, `everos.toml` holds no value raven wrote for the four sections, and its remaining content has no effect on them. | `diff` of the file before and after (unchanged), plus A9 |
| A8 | After a model change the runtime knobs in `everos.toml` still apply. | A non-default `timeout_seconds` set by hand is still in force, read from the running server's settings |
| A9 | Clearing a role really clears it: the section left in `everos.toml` does not revive. | The child's environment shows `EVEROS_<SECTION>__MODEL=`; the capability reports unavailable |
| A10 | An install upgrading from the previous version keeps working with no user action. ★ | Before: values only in `everos.toml`. After first load: role pins in raven's config, the same models in force, `everos.toml` byte-identical |
| A11 | A vendor raven has no provider row for is migrated by creating that row from the wizard's vendor table and moving the key into it. | `providers.deepinfra` exists afterwards and serves the role; the role is usable without the user adding anything |
| A12 | When even that fails, the role reads as unset and is reported in all three of: the migration notice, `raven doctor`, and the role slot. Nothing is silent. | The three messages, captured |
| A13 | `[api]` still decides the listen address: an inherited `EVEROS_API__PORT` does not move the server. | Exported variable plus the port the server actually binds |
| A14 | On a user-managed root the role slots are not editable, a write is refused by name, and raven never spawns or stops that server. | The refusal message naming the root; a screenshot of the disabled slots; the pid is untouched |
| A15 | Rerank configured from the settings page against SiliconFlow issues `POST {base}/rerank`. | The everos server log or a local proxy showing the request line |
| A16 | Rerank configured from the settings page against DeepInfra uses the inference base url, not the chat one. | The stored provider and the resolved base url in the child's environment |
| A17 | The rerank slot does not offer providers that cannot rerank. | The slot's candidate list next to the vendor table's `supports` |
| A18 | `settings.everos` reports `api_key_set` as "this provider has a usable credential", and the `provider` it reports for a role is the vendor, never the rerank protocol. (The protocol is still spelled `deepinfra` / `vllm` / `dashscope` where it belongs -- EverOS's own `rerank.provider` field and the `EVEROS_RERANK__PROVIDER` variable that sets it. What this item forbids is that word reaching the page as a role's vendor.) | The RPC response against a provider with and without a key |
| A19 | Changing the provider for a role is one edit; the address and key follow at the next spawn. | The child's environment before and after |
| A20 | Nothing under `raven/` calls or re-implements `everos_owned()`. | The C1 test |
| A21 | Two saves in quick succession end at the final configuration and never overlap. | Each restart chain logs a start and an end with an id; the intervals do not overlap and the last configuration is in force |
| A22 | A restart requested when no agent loop exists is refused synchronously rather than run in the background, because there would be nowhere to report its outcome. | `settings.everosSet` returns `applied:false` with a reason |

## Design

### Change map

| Layer | Now | After |
|---|---|---|
| Store | `everos.toml` `[llm]` `[rerank]` `[multimodal]` hold model, base url and api key, written by raven. The embedding pin is a top-level block in raven's config; an install that predates it has the endpoint only in `everos.toml`. | Role blocks `llm` / `rerank` / `multimodal` in `plugins.config['everos-memory']`, each `{model, provider}`. The embedding pin stays where it is. No credential in either. |
| Deliver | Only embedding travels, as `EVEROS_EMBEDDING__*` built at spawn from raven's pin. | `everos_env()` resolves all four at spawn, emitting every role -- held ones with values, unheld ones empty. |
| In-process | `understand_media` reads `[multimodal]` from `everos.toml` through everos's cached settings. | The plugin binds the same variables into raven's own environment at `start()`, as `configure_embedding_env` already does. Live changes need a raven restart (non-goal). |
| Gate | `everos_role_configured(section)` reads `everos.toml` and asks for model AND api_key. **Eight call sites** (four direct, four through `onboard._everos_role_configured`). | Same function, reading raven's config, asking for model AND provider AND a resolving credential. All eight follow. |
| Read | `settings.everos` reports the file's sections; `provider` there is EverOS's rerank protocol; the page reverse-looks-up a vendor from a base url. | Reports the role pins plus `owned`; `provider` is the vendor, the rerank protocol is not returned at all. The page stops guessing. |
| Write | `console.py` holds the field allowlist and writes through `set_everos_section`. | `console.py` forwards to the plugin's `set_role()`; the ownership guard stays inside it. |
| Frontend | `Roles.tsx` reverse-looks-up a provider from `base_url` (:97-103), filters candidates by "has a key" only (:117), has no disabled state. | Reads the stored provider; filters by the vendor table's `supports`; disables the slots when `owned` is false. `generated.ts` regenerated. |
| Apply | Nothing on the web path; the wizard stops and restarts its own server. | `restart_for_config_change()` in `server.py`; the wizard awaits it, the RPC backgrounds it. |
| Migrate | `raven doctor --fix` offers to move the embedding endpoint. | A versioned migration in `config/loader.py` calls the plugin to move all four on first load; doctor stays as a manual remedy. |

### The restart

```python
# server.py, beside stop_pid and lock_holder
async def restart_for_config_change(root, base_url, *, on_result) -> None:
    """Apply a configuration that was just written.

    Order is load-bearing: the precheck runs before the stop, so a restart that
    cannot succeed leaves the old server serving. The window where memory is gone
    and nothing will bring it back is what this ordering removes.

    Every exit reports through ``on_result``. A stop that does not reach STOPPED
    must not fall through to the spawn: ``ensure_everos_server`` would find the
    old server answering and adopt it, reporting success for a configuration that
    never took -- the very bug this function exists to remove, arriving by a
    different door.
    """
    # 1. precheck: _require_llm_configured + _inotify_gate    (a file read; /proc on Linux)
    #      fails      -> on_result(ok=False, reason); the old server keeps serving
    # 2. lock_holder(root)
    #      nothing     -> on_result(ok=True); the next start reads the new values
    # 3. stop_pid(pid)                                        (up to 35s while it drains)
    #      NOT_OURS / SIGNAL_FAILED / STILL_DRAINING -> on_result(ok=False, that reason)
    #      STOPPED                                   -> continue
    # 4. ensure_everos_server(base_url)                       (~2s cold start)
    #      -> on_result(ok=True) or on_result(ok=False, reason)
```

The wizard awaits it and narrates, as it does today. The RPC hands it to a task and returns
immediately; `on_result` emits `memory.health`, `{ok:true, error:null}` included, because the
banner is only cleared by a success frame. A second save while one is in flight sets a flag
rather than starting a second chain, and the in-flight chain runs once more when it finishes.
When `_safe_loop` cannot produce a loop there is no way to emit, so the RPC refuses
synchronously (A22) instead of running blind.

The running `EverosBackend` needs no change: it talks HTTP to an address, not to a pid. Its
`self._proc` points at the dead child, read only by `_state_from_child()` on a refused probe,
producing `FAILED` -- not terminal, so the next probe returns it to `READY`. (The reason a
running backend does not respawn is that spawning happens only in `start()`; `_may_spawn()`
guards nothing today and is deleted with `_SPAWNABLE_STATES`.)

### The environment

```python
# raven_everos/config.py
def everos_env() -> dict[str, str]:
    """The binding all four roles earn, from raven's own config.

    Read fresh at every spawn rather than bound once: a value written a second ago
    must reach the next child without restarting raven. A role raven does not hold
    is emitted empty -- suppressing whatever the file says is what makes clearing a
    role in the UI mean anything, since raven no longer edits that file.
    """
```

`_child_env()` calls it where it calls `host_embedding_env()` today. `EverosBackend.start()`
also binds it into raven's own environment, so `understand_media` can resolve a client.

Two opposite policies then live in `everos.toml`, both deliberate:

- `[api]` -- **the file wins**, and `_child_env()` deletes every `EVEROS_API__*`. Where the
  server listens has to be spelled the same way in the bind and in the health probe; an
  inherited variable is what once split them, spawning a second instance that died on the OME
  jobstore lock.
- the four roles -- **the environment wins**, on a root raven owns. What model raven manages
  is raven's to say.

They answer different questions: where this root's server lives belongs to the root; which
model it talks to belongs to raven.

### Migration

Scheduled from the versioned chain in `config/loader.py` (`notify=True`, like its neighbours)
and **implemented in the plugin** -- the host passes the raw config and gets it back mutated,
so nothing under `raven/` resolves the EverOS root or judges ownership (C1, C14). Skipped
entirely when the slice says the root is not raven's.

The embedding half is the existing `adopt_legacy_endpoint`, moved from `doctor --fix` onto
this chain. The embedding move was *offered* rather than applied because the old path kept
working; here it does not -- an unmigrated `llm` reads as unconfigured, which turns long-term
memory off.

Per role, in order:

1. **Name the vendor.** `provider_serving_at`, widened: full base url, then host, then api
   key. The host step is what catches DeepInfra rerank, whose section deliberately holds a
   different path from the chat endpoint.
2. **Create the vendor if raven has no row for it.** Measured (C20), this is the common case,
   not the edge: the wizard carries its own vendor table and lets a key be typed for a vendor
   raven does not know, so on the author's machine two of four roles resolve to nothing today.
   The name comes from the wizard's vendor table, LiteLLM validates it (C21), and the key
   moves into the new provider row -- which is what actually delivers "credentials in one
   place".
3. **Otherwise record the role as unset** and say so three ways (A12).

### The three rerank defects

All three come from the web path not knowing what the wizard's vendor table knows. Lifting
the table out of `onboard.py` so both paths read it fixes them together: the rerank protocol,
DeepInfra's separate rerank base url, and which vendors can rerank at all -- the slot's filter
today asks only whether a provider has a key.

### Sites that must change with C8

`WRITABLE_SECTIONS` shrinks to `("api",)`. The `EverosNotConfiguredError` text in
`server.py` ("[llm] in {everos.toml} needs both model and api_key") points at the wrong file
afterwards. The module docstring of `raven_everos/config.py` calls itself "the ONLY write path
for llm / embedding / rerank / multimodal" and stops being true.

## Cost

**Who pays.** Anyone upgrading pays one automatic migration on first config load. Where the
vendor is unknown to raven it also gains a provider row it did not create by hand -- a real,
manageable row, not a shadow entry. Anyone whose `everos.toml` points somewhere nothing can
name pays a manual pick, announced three ways.

**Who else pays.** `understand_media` stops following a multimodal change within the session;
it picks the new model up at the next raven start. Memory extraction, which is the reason the
setting exists, follows immediately.

**Reversible.** `everos.toml` is read and never written, so reverting the code leaves a
working file behind -- for an install whose sections were still filled. The one-way part is
the migration's provider rows and the keys moved into them; a downgrade would read the old
sections, which are still there.

**Half of it is not worth shipping.** Restart without the move leaves two sources and gets
rewritten by the move. Move without restart does not fix the bug. The rerank fixes could ship
alone but land in the function the move rewrites.

**The expensive part is the gate.** `everos_role_configured` decides whether this install has
long-term memory and eight call sites share it. Reading the wrong source, or reading it with
the wrong rule, turns memory off silently, and no existing test goes red for it. It is also on
`raven doctor`'s path, which advertises itself as a fast command -- the new rule resolves a
provider credential per call.

## Decisions

**The four roles travel by environment variable, sourced from raven's config.** EverOS
resolves `env > everos.toml`, so this needs no cooperation from it. Rejected: keeping the file
as the source and merely restarting, which leaves credentials duplicated and a vendor change a
three-file edit.

**All four are emitted on every spawn, unheld ones empty.** An empty value suppresses the file
(C18), which is what makes "clear this role" mean something now that raven no longer edits the
file, and what stops an upgraded install's stale section from reviving. Rejected: emitting only
held roles, which the owner had approved and then withdrew once the no-op was shown. The
consequence is accepted explicitly: on a root raven owns, a hand-written role section in
`everos.toml` has no effect.

**raven keeps creating `everos.toml` and keeps writing `[api]`.** Only the four sections are
given up. Rejected: touching nothing, which leaves a fresh install with no config file and no
port authority.

**Role blocks live in `plugins.config['everos-memory']` and hold `{model, provider}`.** The
slice already holds this plugin's `base_url` and root, and `PluginsConfig.config` makes each
plugin responsible for its own keys. Rejected: a top-level `llm` block (collides with raven's
chat model) and `memory.llm` (`memory.backend` can be another backend entirely, which does not
read these -- those backends land with PR #434).

**Migration creates a provider row for a vendor raven does not carry.** Measured, this is the
common case rather than an edge, and it is the step that actually achieves one credential
store. LiteLLM validates the name, so the row is a normal manageable provider. Rejected:
recording the role unset (which on the author's machine loses embedding and rerank on upgrade,
with no way to re-pick them because the vendor is not offered) and carrying the bare endpoint
into the role pin (a credential in a place the design says holds none).

**The restart is one function with two waits, and every exit reports.** The wizard awaits it
because it has a flow to hold; the RPC backgrounds it because a save must not block for 35
seconds. A stop that does not reach `STOPPED` must not fall through to the spawn, or
`ensure_everos_server` adopts the old server and reports success for a configuration that
never took.

**Failure and success are both reported by a `memory.health` push.** The RPC stack's `notify`
prints to stderr. `memory.health` is in `NOTIFICATION_METHODS`, handled in `install.ts`,
rendered by the standing banner -- and that banner is only cleared by an `ok:true` frame, so
success has to be pushed too.

**The precheck runs before the stop.** The one genuinely bad state is stopped-and-not-started:
spawning happens only in `EverosBackend.start()`, so a running backend will not recover, and
memory stays gone until the next gateway start.

**Migration is automatic and lives in the plugin.** The old path dies with this change, so
offering it is not an option; and the host must not learn to resolve the EverOS root or judge
ownership, so `loader.py` schedules and the plugin acts.

**The ownership guard stays in the plugin's write primitive.** Its docstring gives the reason
-- enforced there so a new caller cannot opt out -- and this change would have been that new
caller. The host forwards; `settings.everos` gains `owned` so the page disables the slots
rather than letting a person discover the refusal by hitting it.

**`borrow_from` disappears into `provider`.** Today a role save names a lender and the
server copies its key into `everos.toml`. `lend_provider_credentials` documents why it
copies rather than references: "this file is read by EverOS as well as by raven, and a field
only raven resolves is a field EverOS reads as an endpoint it cannot reach". That premise is
precisely what this change removes -- raven's config is no longer read by EverOS, the
environment carries resolved values instead -- so the alternative that docstring names
becomes the right one, along with the benefit it names for it: the pin "would follow a later
key change on its own", which is A19. The wire parameter is renamed, not deleted; the page
already sends the provider's name.

**`api_key_set` changes meaning to "this provider has a usable credential", and EverOS's
rerank protocol is no longer returned as `provider`.** raven's blocks hold no key, so the old
reading cannot be computed; and `provider` would otherwise name two different things in one
field across the change. The protocol is derived from the vendor table now, not chosen by the
user, so nothing needs it on the wire.

**`understand_media` follows a multimodal change only after a raven restart.** everos resolves
the client from a cached module singleton with no injection point, and reaching into that
private global is a non-goal. Binding the variables into raven's own process is enough for it
to work at all, which is the requirement; following live is not.

## Open questions

- Which module owns `everos_env()` -- the plugin's `config.py`, or a new module beside it.
- The migration's version constant and the `from_version` threshold.
- Whether the widened reverse lookup should weight the api-key match differently when two
  provider rows share one key.
