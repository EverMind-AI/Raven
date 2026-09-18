# Web settings page rebuilt on the v0.2 prototype - design

Status: draft for G1 (2026-09-17, revised after the clean self-review).
Branch `feat/ui_web_settings_page`, target `refactor/ui_web_architecture`.

Companion documents: acceptance cases in
`docs/specs/2026-09-17-web-settings-page-acceptance.md`, plan in
`docs/plans/2026-09-17-web-settings-page.md` (both written after G1). The
deviations ledger is a working file of the task, not committed; its entries
are quoted here by number (D1, ...). "S1", "S7c", "S8", "S10" are the step
numbers of the architecture note listed under Sources.

## Terms used throughout

- **live**: the running gateway reads the new value on the next turn or call.
  **reload-only**: the value is bound when the agent loop is built and
  applies after `gateway.reload` or a restart; `settings.set` returns
  `warning` for such a key and the page shows that text.
- **masked**: a secret reads back as bullets plus its last four characters
  (provider keys, header values); a plugin credential reads back only as set
  or not set. The page never sends a masked value as a new value.
- **connected**: what `model.options` reports as `on`: a key for key-shaped
  providers, an address for local ones, `authenticated` for OAuth ones. No
  connectivity probe is made; connected means written.

## Goal

Rebuild the web UI's settings dialog to the v0.2 prototype and fill the backend
gaps so that every enabled control on every page reads a real value and
writes one.

The v0.2 PRD ("raven web optimisation") lists the settings page as the one
*rebuild* among mostly visual iterations: "redesign the whole settings page,
its look and its logic". The front end is being re-architected on
`refactor/ui_web_architecture`; the settings island is the page a backend
engineer owns in that plan, with styling handed to a front-end engineer
afterwards. So the deliverable is a working page whose structure the front end
can take over without a second rewrite, not a pixel-perfect one.

Who this is for: the person running a Raven install from a browser (every
control does what it says); the front-end engineer who inherits the code (one
domain, the layout the architecture note prescribes); the onboarding and
composer owners, who share three RPCs (`model.options`, `model.save_key`,
`config.set` with key `model`) and one component (the model picker) with this
page.

The prototype has eight pages; the PRD's settings section has six rows with
one screenshot each (general, model, skills, tools, plugins, archive). Usage
and about have no PRD row; where the PRD is silent the prototype decides.
Of today's thirteen tabs, seven are dropped: permissions, memory, proactivity,
exec, channel, data, keyboard shortcuts. The kept pages lose code font and
motion (look) and the test-notification button (notify); memory's model roles
move into the model page.

Shapes rejected in the discussion round:

- Embedding the prototype's HTML document in an iframe, the way the prototype
  itself does. Fastest, but the takeover would rewrite it, and the dialog shell
  is React already.
- Settings as a routed page instead of a dialog. The prototype and the
  architecture branch both model it as a dialog.
- Sharing an "add provider" component with onboarding. Same RPCs, different
  owners; components stay separate.
- Keeping the current thirteen tabs and restyling them.

## Non-goals

- Anything outside the settings dialog: sidebar, onboarding, playbooks, cron,
  subagents, the composer. Two pieces land outside the settings domain and
  nothing else: `src/components/ModelPicker.tsx` and
  `features/model/vendors.ts`. Switching the composer to the new picker is the
  composer owner's change, not this one.
- An install or marketplace entry for skills and plugins on the settings
  pages. The PRD removes them from the sidebar and the prototype has none in
  settings; the gap is named under "Open questions" for the sidebar owner.
- Rebuilding the seven dropped tabs or the dropped controls of kept pages.
  Their backend keys stay writable through the RPCs that exist today.
- A hot path for the four reload-only keys (tool-iteration cap, context
  window, curator model, skill-gate model). They keep their semantics; the
  page says so.
- A "reload the gateway" action on the page. `gateway.reload` lives on the
  token-gated control dispatcher, not on `/rpc`.
- Backend guards for "disconnect a provider a role still uses" and "remove a
  model a role still uses". The page refuses and names the roles; the CLI
  keeps its behaviour.
- Restyling. Prototype CSS is copied with class prefixes; visual polish is the
  front-end engineer's pass.
- The `skillForge.evolveModel` role (D1: the field has no reader).
- Provider OAuth for vendors other than the three with a device flow today
  (OpenAI Codex, GitHub Copilot, MiniMax).
- Consuming `key_url` anywhere but the settings page; the TUI and onboarding
  may pick it up later.
- Connectivity probes on connect. Connected means the credential is written.
- Backfilling `.install-meta.json` for hub skills installed before this
  change (their detail shows no install line) and `archived: false` for
  sessions restored before this change (they are eligible for auto-archive
  like any other stale session).
- Pricing tables and the telemetry format. A call with no recorded cost stays
  "no price".
- Implementing `model.fetch_models` for vendors without a list endpoint.
- Widening `fs.open`. Skill files open through a new `skills.manage` action.
- Renames outside `features/settings/` (the conventions branch's S7c pass).

## Constraints

Each constraint says how it is checked. A constraint with no check does not
belong here. `BASE` below is `$(git merge-base HEAD refactor/ui_web_architecture)`.

### Architecture

| # | Constraint | How to check |
|---|---|---|
| C1 | `features/settings/` follows the domain skeleton of the architecture note: `types.ts`, `source.ts`, `store.ts`, `SettingsApp.tsx`, `wire.ts` (today `chrome.ts`), plus `styles.css` and `manifest.ts` when their mechanisms land. Every `gateway().call` for this domain lives in `source.ts`, including the version check and the session-scoped permission write that `chrome.ts` makes today. | `grep -rln "gateway()" ui-web/src/features/settings --include=*.ts --include=*.tsx \| grep -v "\.test\."` prints only `source.ts`. The `import-direction` gate of `refactor/ui_web_conventions` once it lands. |
| C2 | Cross-domain reach is through another domain's `source.ts` or `types.ts` only. No `islands.*`, no assignment onto `window`, no import from `src/shell/`. Shared components live in `src/components/`; URLs are opened through `src/lib/openUrl.ts`. | `grep -rnE "from '\.\./[a-z]+/(store\|[A-Z])" ui-web/src/features/settings` prints nothing; `grep -rnE "islands\.\|window\.[a-zA-Z_]+ *=\|from '\.\./\.\./shell" ui-web/src/features/settings` prints nothing. Conventions gate when it lands. |
| C3 | Stores expose `get() / set(next) / subscribe(fn) / _resetForTests()`; components read through `useSyncExternalStore`; the only way to force a repaint is the store's `redraw()` (a `set` of the same snapshot). None of the retired names appear. | `store.test.ts` asserts the four names exist; `grep -rnE "getState\|snapshot(\|draw(\|langRedraw\|forceUpdate" ui-web/src/features/settings` prints nothing. Store-shape gate when it lands. |
| C4 | Every class the island introduces is `settings-` prefixed and every DOM id it owns is `settings-` prefixed. Rules go into `src/styles/page.css` until the per-domain `styles.css` import (S8) exists, then move as one block. | `grep -rhoE "className=\"[^\"]+\"" ui-web/src/features/settings \| tr ' ' '\n'` yields only `settings-` names and names in the shared vocabulary of `scripts/check-class-namespace.mjs`; `grep -rhoE "id=\"[^\"]+\"" ui-web/src/features/settings` yields only `settings-` names. `node scripts/check-class-namespace.mjs` once the settings entry is registered (C8 exemption). |
| C5 | All text is `t(key)` with keys under `gui.settings.<leaf>` in `i18n/messages.json`, both locales filled. No `T()`, no `lang.text()`. | `grep -rnE "\bT(\|lang\.text(" ui-web/src/features/settings` prints nothing; a one-line script asserts every `gui.settings.*` key has `en` and `zh`; `make check-source-language`; the `i18n-keys` gate when it lands. |
| C6 | Contract first: a new or changed RPC field is edited in `rpc-schema/openrpc.json`, then `npm run gen`, then `source.ts`, `store.ts`, component. Every method the settings source calls has a reply in `rpc/fixtures/settings.ts`, typed with `: Type`, never `as`. | `npm run gen:check`; `tests/test_rpc_schema_match.py`; a fixture test walks the method names `source.ts` calls and asserts the fixture answers each; `grep -n " as " ui-web/src/rpc/fixtures/settings.ts` prints nothing. |
| C7 | Backend writes go through `atomic_update` on the existing config file and the existing session files. No new store, no new file format; a new writer sits in the module that owns that file next to the existing ones (`update_providers.py`, `update.py`, `session/manager.py`). | Review of the diff against this list; `git diff BASE..HEAD --stat -- raven` touches no new directory; the pytest per method exercises the writer through the RPC. |
| C8 | Not modified: `App.tsx` (the `SettingsModal` shell: `#setVeil #setModal #snav #snavList #spanels`), `state/settings.ts`, `state/lang`, `state/toast.ts`, `chrome/`, `app/`, `lib/notifications.ts`, the look store, the gate scripts. Two exemptions: `main.tsx` changes only the settings island's import and render lines; `scripts/check-class-namespace.mjs` gains the settings entry if the conventions branch has not made it a ratchet by then. | `git diff BASE..HEAD --stat -- ui-web/src/App.tsx ui-web/src/state/settings.ts ui-web/src/state/lang ui-web/src/state/toast.ts ui-web/src/chrome ui-web/src/app ui-web/src/lib/notifications.ts ui-web/scripts/gates` is empty; the diff of `main.tsx` is at most three lines; the diff of `check-class-namespace.mjs`, if any, is the one table entry. |

### Design constraints

| # | Constraint | How to check |
|---|---|---|
| C9 | A control never pretends. A reload-only key returns `warning` from `settings.set`, and the toast shows that text instead of "saved". | A46 |
| C10 | Secrets never round-trip (see "masked" above). | A10, A17, A37 |
| C11 | The old page goes, it does not hide. `SettingsPage.tsx`, its test, its snapshot and `ImageModelPicker.tsx` are deleted; no nav entry or component of the dropped pages remains in the bundle; every `gui.set.*` key left in `i18n/messages.json` is referenced by a file outside `ui-web/src/features/settings/` (in `ui-web/src` or `ui-tui/src`). | A50; a one-line script over the remaining `gui.set.*` keys and `grep -rl` outside the settings directory. |
| C12 | Behaviour behind the dropped pages is untouched on the backend: `permissions.mode`, channel, exec and memory keys stay in the `settings.set` whitelist and keep their tests. | `tests/test_rpc_settings.py` passes with no test removed. |
| C13 | Skill on/off applies on the next turn without a restart. | A25 |

### Assumptions

| # | Assumption | Status |
|---|---|---|
| C14 | Image, speech and video generation only run against OpenRouter, so the three media roles offer OpenRouter alone. | Verified 2026-09-17: `MediaGenConfig` docstring "OpenRouter is the only backend"; `raven/agent/tools/media_gen.py` `_OpenRouterMediaTool` posts `{base}/chat/completions` with `modalities` and `{base}/videos`; key falls back to `providers.openrouter.apiKey`. |
| C15 | Listing sessions already reads every session file's metadata, and saving metadata appends one record. A lazy auto-archive pass adds one appended line per stale session and nothing otherwise. | Verified 2026-09-17: `raven/session/manager.py` `list_sessions` globs `*/*.jsonl`; `save()` docstring "appends a fresh metadata record". The pass must not go through `get_or_create`, which loads the transcript. |
| C16 | An MCP server installed from the catalog is named by its catalog entry id and recorded in the install ledger, so its form template can be fetched again to rewrite a credential. | Verified 2026-09-17: `raven/market/install.py` `servers[entry_id] = _build_mcp_config(contrib, form)`; `read_ledger(entry_id)`. Hand-written servers have no ledger and get no credential panel. |
| C17 | Each of the three provider OAuth device flows can be started without a console and yields a verification URL and a user code that the page can show. | **Unverified.** `raven/providers/minimax_oauth.py` `_login_locked` exposes `verification_uri` and `user_code` behind a `print_fn`; the Codex flow is LiteLLM's driver, which prints the URL; Copilot is a device flow in `raven/cli/provider_commands.py`. Spike S1 in the plan before the RPC is designed in detail. |
| C18 | `gateway.reload` is not reachable from the page. | Verified 2026-09-17: registered on the control dispatcher in `raven/cli/gateway_commands.py`, token-minted. |
| C19 | `.install-meta.json` next to a hub-installed skill carries `installed_at`, `version`, `trigger`, `source`; the audit log carries `score_safety`. Today's triggers are `use_skill` and `auto_inject`. | Verified 2026-09-17: `raven/skill_hub/audit.py` `write_install_meta` / `record_install`; call sites in `agent/tools/skill_hub.py` and `context_engine/segments/skills.py`. The `skillhub.install` RPC path does not stamp today (fixed by this change). |
| C20 | `refactor/ui_web_conventions` lands while the backend batch is being built (estimated three to four days), so the rename mapping is applied once and the `page.css` hash gate is gone before the first front-end commit. | Unverified (owner's estimate: one to two days). Fallback if it has not landed when the front end is ready: the page is written to the note's shapes anyway, the `page.css` hash is updated in the same commit with the reason in its body, and the conventions author's S7c pass renames whatever remains. |
| C21 | `ext.list` already returns per skill `name`, `description`, `source`, `always`, `hub`, `hub_id`, and per MCP server `name`, `enabled`, `state`, `error`; `model.options` already carries `context_window` per model. | Verified 2026-09-17: `raven/rpc/methods/console.py` `ext_list`; `ui-web/src/components/ModelTags.tsx` `ModelTagFacts.context_window`. |

## Acceptance

A1 to A48 are checked by an agent on a real host: a real `raven serve`, a
real browser, real config on disk. A49 to A55 are checked by the commands
they name. Items marked with a star are candidates for the manual pass and
are also run by the agent; the final star table is in the acceptance-cases
document.

Numbering is stable once referenced. A withdrawn item keeps its number.

### General

- A1. Language: switching to English repaints every string on every settings page in English, `config.get` for `language` reads `en`, and the reply to the fixed probe "Say hello in one word" arrives in English; switching back repaints in Chinese. Fails if any settings string stays in the other language or the probe is answered in the old one.
- A2. Theme: choosing "dark" turns the whole page dark, survives a reload of the browser tab, and "system" follows the OS. Fails if the dialog and the page behind it disagree.
- A3. Notifications: with the switch off, a finished turn in a background tab raises no browser notification; on, it does.

### Usage

- A4. Range: "7 days" (today and the six days before) changes the tiles, the chart, the model table and the tool table to that window; a custom range with `from` and `to` returns exactly the days between them, inclusive. Fails if any of the tiles, the chart or either table keeps the previous range.
- A5. Daily bars: one bar per day in the range; a day with no telemetry file is a zero bar, not a gap.
- A6. Tiles: calls, estimated cost, output tokens and cache hit match the same numbers computed from the telemetry files by hand for the same range; hit = cache read / (input + cache read).
- A7. Model table: a model whose calls carry no cost shows "no price" rather than $0.00; the "cache write" column appears only when any call in the range wrote cache.
- A8. Tool table lists every tool called in the range with its count; a tool never called does not appear.

### Model

- A9. Provider list shows every connected provider (see "connected") with its display name, id and configured-model count, and a "subscription" tag on OAuth providers. Fails if a provider `model.options` reports as connected is missing or one it reports as not connected is listed.
- A10. Add provider, API key: the key field carries a "get a key" link when the vendor has a `key_url`; pasting a key and "connect" writes it, the provider appears as connected, the key reads back masked. Fails if a masked value is ever sent back on a later save.
- A11. Add provider, local: vLLM or Ollama with an address and no key connects; an address left empty is refused before any write.
- A12. Add provider, Azure: deployment name and API version written by `model.set_fields` land in the `azureOpenai` section and are read back. (*)
- A13. Add provider, OAuth: "authorise in browser" starts the device flow, the page shows the verification URL and user code and opens the URL in a new browser tab through `lib/openUrl`, and when the flow completes the provider turns connected without a page reload. If the code expires unfinished the page says so. Fails if the code is only printed on the gateway's console. (*, depends on C17)
- A14. Provider detail, disconnect: a provider no role uses disconnects and drops from the list; a provider a role uses (a role following the chat model counts through the chat role) is refused with the role names in the message and nothing is written.
- A15. Provider detail, models: "add" fetches the vendor's model list when it has one, lets several be ticked plus one typed id, and one click adds them all in one config write; a vendor without a list endpoint says so and takes a typed id. Removing a model a role uses is refused with the role's name.
- A16. Provider detail, address: changing the service address of a connected provider persists without re-entering the key. Fails if the write is refused for a missing key.
- A17. Provider detail, headers: adding a custom header sends only that header and persists it under `extraHeaders`; the list shows each name and its masked value; removing one sends only its name and removes only that one.
- A18. Provider detail, display name: naming a model "Sonnet 4.5" with a description shows the label as the model's name and the description as its secondary line in the picker and in the role rows; clearing the label reverts to the id. (*)
- A19. Roles, chat: picking a model from another connected provider writes `agents.defaults.model` and `.provider`; the composer's default chip shows the new pair.
- A20. Roles, curator / skill gate / session naming: each writes its model and provider pair; "follow the chat model" clears both to null. `settings.set` returns a `warning` for curator and gate (reload-only) and none for session naming (live).
- A21. Roles, memory (EverOS llm / embedding / rerank / multimodal): picking a provider and model calls `settings.everosSet` with `borrow_from`, and the section reads back with that model; a provider without a key of its own (OAuth, local) is not offered. Embedding shows the re-index warning the server returns.
- A22. Roles, media: image, speech and video offer OpenRouter only; when it is not connected the row offers "connect OpenRouter"; picking a model writes `tools.media.<kind>` first and then removes the tool from `tools.disabledTools`; clearing writes null and then adds the tool back. If the second write fails its error is shown and the first stays.
- A23. Chat parameters: reasoning effort is written to `agents.defaults.reasoningEffort` (an existing key; timing unchanged); a tool-iteration cap outside 1-200 is refused; a context window below 1024 is refused, one above the model's window (from `model.options`) saves and the row shows a red note computed by the page while the toast shows the server's reload warning; "follow the model" writes null and the row shows the model's window.

### Skills

- A24. List: every SKILL.md the registry sees appears under "built-in" (`source == builtin`) or "workspace" (every other source, hub-installed ones included) with its description; the search box filters by name and description as you type; a hub-installed skill shows the uninstall action, others do not.
- A25. Toggle: switching a skill off writes `skillForge.blocklist`, and a turn started right after does not receive that skill (no restart); switching it back on restores it on the next turn. (*)
- A26. Detail: opening a skill shows its SKILL.md body rendered, its file list, its origin tag, "injected every turn" when `always` is set, and for a hub-installed skill the install time, the version, the trigger (`use_skill` shown as "in conversation", `auto_inject` as "automatic", `rpc` as "from a client") and the safety score.
- A27. Detail, open file: with the gateway and the browser on the same machine (the precondition `fs.open` has today) the file opens in the system's default application for both a workspace skill and a built-in skill; a path outside the skill's directory is refused.
- A28. Uninstall: confirming removes the skill directory and the row; declining leaves both.

### Tools

- A29. Groups: every switchable tool `ext.list` reports appears in exactly one of the eight groups; the counter "on N / total M" counts switchable tools only. A Python test diffs the group table (`toolGroups.json`) against the tool registry.
- A30. Toggle: switching a tool off writes `tools.disabledTools` and the next turn cannot call it; switching on a tool that lacks its key or model writes it on, opens its panel, and keeps the "needs setup" badge until the key or model is saved.
- A31. Web search / fetch: choosing a vendor and saving its key clears the "needs setup" badge; clearing the key brings it back. Deep research behaves the same with its key.
- A32. Media tools: the model control in the row is the same control as the roles page; a change in one shows in the other.
- A33. Tool search group: `tool_search` and `tool_call` are shown greyed with no working switch and a note that they are built-in; they are excluded from the counter.

### Plugins

- A34. List: every configured MCP server appears with its switch and, when the switch is on, a status chip: connected, connecting, needs setup (OAuth not authorised or key empty), or failed with a retry action; a server switched off shows no chip. Counter "connected N / total M" matches.
- A35. Toggle: off disconnects the server on the running gateway (its tools vanish from the next turn); on reconnects it; a server turned on that still needs a credential is written as enabled, opens its panel and makes no connection attempt until the credential is saved.
- A36. Retry: a server in the failed state reconnects when retried, without toggling and without a gateway restart. (*)
- A37. Credential: for a catalog-installed server that takes an API key, pasting a new key rewrites the header or env the template names and reconnects; "clear" empties it and the chip turns to "needs setup". The key reads back only as set / not set.
- A38. OAuth: "authorise in browser" opens the provider's page in a new browser tab through `lib/openUrl` and the chip turns connected when the token lands; "revoke" deletes the token and the chip turns to "needs setup".
- A39. A hand-written server (no ledger entry) shows switch, status and retry but no credential panel.
- A40. Withdrawn before G1 (merged into A34).

### Archive

- A41. List: archived sessions appear with title and `updated_at`; restoring one makes it reappear in the sidebar list at its `updated_at` position; deleting one removes its file.
- A42. Auto-archive on: a session whose last message (`last_message_at`, falling back to `updated_at`, the same value `session.list` reports) is older than 30 days and that is not pinned is archived on the next session list; a pinned one is not; a session the user restored is never archived by the automatic pass again. (*)
- A43. Auto-archive off: nothing is archived automatically; the switch writes null.
- A44. Cost of the pass: with 500 sessions on disk, the first `session.list` after enabling loads zero transcripts and appends exactly one metadata record per stale session; the next list appends none. Wall-clock time is recorded in the acceptance report, not asserted.
- A45. Withdrawn before G1 (merged into A41).

### About

- A46. Warning toast: saving a reload-only key shows the server's warning text instead of "saved"; saving a live key shows "saved". (Also checks C9.)
- A47. Version and update: the page shows the running version; "check" calls `system.version` with `check: true` and the gateway log records one release lookup for it; when a newer version exists the row offers "upgrade", which hands off to the existing upgrade flow.
- A48. Paths: config file and storage location match `settings.get` and `agents.defaults.workspace`; copy puts the exact path on the clipboard.

### Cross-cutting

- A49. Offline page: `?stub=1` opens every settings page against fixtures with no gateway; every page mounts with no console error and shows the fixture values, and every write is refused with the offline note rather than throwing.
- A50. Old page gone: `SettingsPage.tsx`, `SettingsPage.test.tsx`, its snapshot and `ImageModelPicker.tsx` do not exist; `grep -rn "PermPage\|ProactPage\|ExecPage\|ChannelPage\|DataPage\|KeysPage" ui-web/src` prints nothing; C11's key check passes.
- A51. Both locales: every `gui.settings.*` key has `en` and `zh` (the C5 script); `grep -rnE ">[A-Za-z][^<{]*<" ui-web/src/features/settings --include=*.tsx` prints nothing (no literal JSX text).
- A52. Gates: `npm run type-check && npm test && npm run build && python3 build.py` pass on the branch; any golden or snapshot change is explained in the commit body.
- A53. Backend suite: every new or changed RPC has a test that fails when its handler is reverted (mutation check per method).
- A54. Shell untouched: C8's diff check is empty.
- A55. Onboarding still works: the first-run flow connects a provider and picks a model with the changed `model.options` and `model.save_key`.

## Design

### Reading order

At mount the source performs four reads (`settings.get`, `model.options`,
`ext.list`, `settings.everos`) into one snapshot the store holds; pages
render from it. A page with data of its own (usage, archive, skill detail,
version) reads through the source into its slice of the store when it opens.
Out: a control calls one source function, the source calls one RPC, reloads
the read that changed, toasts (the server's `warning` if any), and the store's
`set` repaints. The island writes to the DOM only through React and the two
portals; `grep -rnE "document\.\|querySelector" ui-web/src/features/settings`
prints only the portal targets.

### Backend changes

| Method | Change | Writer / reader | Timing | Tests |
|---|---|---|---|---|
| `settings.set` | Whitelist adds: `agents.defaults.maxToolIterations` (int 1-200), `agents.defaults.contextWindowTokens` (int >= 1024 or null), the model/provider pairs `context.curatorModel` / `context.curatorProvider`, `sessionTitle.model` / `sessionTitle.provider`, `skillForge.llmGateModel` / `skillForge.llmGateProvider` (each str or null), `tools.media.speech` and `tools.media.video` (the selection object `tools.media.image` already takes), `sessions.autoArchiveAfterDays` (int >= 1 or null). Raw-list path adds `skillForge.blocklist`. Returns `warning` for the reload-only keys. | `_SETTINGS_SIMPLE_KEYS`, `_write_raw_key` | cap, window, curator, gate: reload-only. Others: live. | one test per key: write, read back, refusal on bad value; `warning` present exactly for the reload-only set |
| `settings.usage` | Adds `from` / `to` (ISO dates, inclusive). `from` earlier than 90 days back is clamped to 90 days back; `from > to` is refused; when `from`/`to` are present `days` is ignored. Result adds `daily: [{date, calls, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd}]`, one entry per day in the range, zeros for days without a file. Model and tool tables are computed over the same range. | telemetry files, one per day | n/a | fixture telemetry over 5 days: range slicing, clamp, refusal, zero days, per-model split |
| `session.list` | Adds `archived: true` to return only archived sessions (default unchanged). Before filtering, when `sessions.autoArchiveAfterDays` is set, each unpinned session whose `last_message_at` (fallback `updated_at`) is older than that and whose metadata has no `archived` key gets one appended metadata record `{archived: true, archivedBy: "auto"}` through a manager helper that appends without loading the transcript. | session manager | live | 500-file fixture: zero transcript loads, one append per stale session, pinned exempt, restored (`archived: false`) exempt |
| `session.archive` | Restore writes `archived: false` instead of popping the key, so the auto pass leaves a restored session alone for good. | session manager | live | restore then list with auto-archive on |
| `skills.manage inspect` | Result adds `body` (SKILL.md text), `files` (names under the skill dir), `always`, `hub`, `hub_id`, `install: {installed_at, version, trigger, source, score_safety} \| null` (null when no `.install-meta.json`). | registry path, `.install-meta.json`, audit jsonl | n/a | built-in, workspace and hub-installed fixtures; missing meta |
| `skills.manage open` | New action: `{action: "open", query: <name>, file: <relative>}` resolves under the skill's own directory and opens with the system opener `fs.open` uses; anything outside is refused. | same opener as `fs.open` | n/a | inside / outside / built-in path |
| `skillhub.install` | Stamps `.install-meta.json` with `trigger: "rpc"` (third call site of `write_install_meta`), so a skill installed from any client shows an install line. | audit | n/a | file present after install |
| skill blocklist | `LocalSkillCatalog` reads the blocklist through a live reader (`live.skill_blocklist(live)`) on every `is_blocked` and pool build instead of freezing it at construction. Reverses row 10 of the hot-reload design for this one key (Decisions). | `raven/config/live.py`, `memory_engine/skill_forge/catalog.py` | live | flip the list on disk between two turns |
| `model.options` | Each provider adds `key_url` from a new `ProviderSpec.key_url` in the registry (14 vendors filled from the prototype's table, empty for the rest). | `raven/providers/registry.py` | n/a | field present, empty for vendors without one |
| `model.set_fields` | New: `{slug, fields}` patches non-key fields through `set_provider_fields`: `api_base`, `deployment`, `api_version`, and `extra_headers` as a patch `{name: value \| null}` merged server-side (null deletes). Refuses `api_key`; returns previous values masked. | `set_provider_fields`, `_redact_headers` | live for new calls | each field; key refused; header add / delete leaves the others; masked reply |
| `model.add_model` | Adds optional `description`; empty-string `label` or `description` deletes that overlay field. | `add_provider_model` overlay merge | n/a | set, clear, re-add keeps the other field |
| `model.add_models` | New: `{slug, models: [id...]}` appends all in one atomic config write; ids already present are skipped; returns the provider row. | new writer beside `add_provider_model` | n/a | N ids, duplicates skipped, one write |
| `model.oauth_login` | New: `{slug}` for the three device-flow vendors; starts the flow, returns `{verification_uri, user_code, expires_in}` at once, polls in the background until the token lands or the device code expires; on expiry the attempt is dropped and `model.options` keeps reporting not authenticated. Exact shape after spike S1 (C17). | provider login modules | live | fake device endpoint: URL and code returned, token lands, options flips; expiry drops the attempt |
| `ext.list` | MCP rows add `auth: "oauth" \| "apikey" \| "none"` and `credentialed: bool` (OAuth: `has_stored_tokens`; apikey: the templated header or env is non-empty). Servers without a ledger entry report `auth: "none"`. | ledger, catalog entry, `raven/mcp/oauth.py` | n/a | three server shapes |
| `plug.retry` | New: `{name}` asks the MCP manager to connect that one server explicitly (the manager's `connect()`), since a re-sync skips servers parked in error. | `raven/mcp/manager.py` | live | errored fake server reconnects |
| `plug.revoke` | New: `{name}` deletes the server's OAuth credential file and disconnects it. | `delete_credentials` | live | token file gone, state needs setup |
| `plug.configure` | New: `{name, form}` re-renders the catalog template for the ledger's entry with the new form and rewrites only the templated fields; empty string clears. Refuses a server without a ledger entry. | `_build_mcp_config` | live (reconnects) | key rewritten in place, clear, no-ledger refusal |

Existing methods the page calls unchanged: `settings.get`, `settings.everos`,
`settings.everosSet`, `config.get`, `config.set` (keys `model` and
`permissions.mode`), `model.save_key`, `model.disconnect`,
`model.fetch_models`, `model.remove_model`, `session.delete`, `plug.toggle`,
`plug.auth`, `skillhub.remove`, `system.version`.

Config additions: `sessions: { autoArchiveAfterDays: int | null }` as a new
`SessionsConfig` next to `SessionTitleConfig` in `raven/config/raven.py`;
`ProviderSpec.key_url: str = ""` in the registry. Both go through the existing
camelCase alias generator.

Every new field and method is declared in `rpc-schema/openrpc.json` and
mirrored in `raven/rpc/models.py` before any client code (C6).

### Frontend

Domain skeleton (architecture note section 2), built beside the old files and
switched over in one commit (the `main.tsx` import and render lines); the old
files are deleted in the commit after.

```
ui-web/src/features/settings/
  types.ts            wire shapes and UI shapes; SettingsSnapshot, SettingsSource
  source.ts           every gateway().call of the domain: load(), one function per write, the version check
  store.ts            get / set / subscribe / _resetForTests / redraw; the open section, drawers, drafts
  SettingsApp.tsx     root; subscribes to state/lang; portals the nav into #snavList, panels into #spanels
  wire.ts             boot wiring (today chrome.ts): injects the source and the update actions the About page calls
  fields.tsx          the form primitives: card, row, switch, segmented pick, text field, key field, stepper, chip, kv list
  toolGroups.json     the eight tool groups by tool name; read by Tools.tsx and by the Python parity test
  pages/General.tsx  Usage.tsx  Skills.tsx  Tools.tsx  Plugins.tsx  Archive.tsx  About.tsx
  providers/Providers.tsx     the provider list and the inline "add provider" block
  providers/ProviderDetail.tsx connection card, models card with the vendor list sheet, advanced card
  providers/Roles.tsx         the eleven role rows and the chat parameters fold
  <module>.test.tsx           one per module; multi-aspect: <module>.<aspect>.test.tsx
ui-web/src/components/ModelPicker.tsx   the two-column popover (providers left, models right, typed id at the bottom); props only
ui-web/src/features/model/vendors.ts    the 43-entry vendor mark table the architecture note names (a mark is the glyph and colour shown beside a provider name, today registered inside components/ProviderMark.tsx); imported by components/ProviderMark.tsx and re-exported through features/model/types.ts for the settings domain
ui-web/src/rpc/fixtures/settings.ts     a reply for every method above
i18n/messages.json                      gui.settings.* in en and zh
ui-web/src/styles/page.css              the settings- block, copied from the prototype's stylesheet with prefixes; moves to features/settings/styles.css at S8
```

Deleted: `SettingsPage.tsx`, `SettingsPage.test.tsx`,
`__snapshots__/SettingsPage.test.tsx.snap`, `ImageModelPicker.tsx`, the
`gui.set.*` keys nothing outside the settings directory references.

The nav has eight entries in prototype order: general, usage, model, skills,
tools, plugins, archive, about. The shell owns the veil, the modal, the nav
column and the panel column (C8); the island renders into them.

The model picker is one component with no store of its own. Settings opens
it for a role with `{providers, current, onPick, onAddTyped}` built from its
snapshot; the composer will open it from `features/model/source.ts` the same
way when its owner switches over. The About page reaches `askUpgrade` and the
version check through `wire.ts` injection, as the update check does today,
not by importing `app/`.

### Page contracts

Each row: what the control shows, what it writes, what refuses.

**General.** Language: segmented zh / en -> `settings.set language`; the store
repaints and the agent replies in the new language. Theme: segmented system /
light / dark -> the existing look store (browser storage). Notifications:
switch -> the existing notifications module.

**Usage.** Range: today / 7 / 30 / 90 / custom (two date inputs, clamped by
the page to 90 days back, `from <= to`) -> `settings.usage {from, to}`. Tiles
from `llm.total`; bars from `daily[].cost_usd`; model table from
`llm.models[]` (uncached input = `input_tokens`, hit = cache read / (input +
cache read), "no price" when `cost_usd` is null); tool table from
`tools.counts`. No session filter.

**Model.** Provider list from `model.options`: connected providers only, with
`kind == oauth` tagged "subscription". Add provider: a select grouped by auth
shape (key / gateway / oauth / local) listing the not-connected vendors; key
vendors take a key (`model.save_key`) with a "get a key" link from `key_url`,
gateway vendors a key and an address, local vendors an address alone, Azure a
key, an address, a deployment and an API version (`save_key` then
`set_fields`), OAuth vendors "authorise in browser" (`model.oauth_login`, the
page opens the URL through `lib/openUrl`, then polls `model.options`).
Provider detail: connection card (re-enter key / re-authorise / change address
/ disconnect); models card (configured list with remove, "add" opening the
sheet: `model.fetch_models` list with checkboxes, a typed id, "add N" ->
`model.add_models`); advanced card (address override and header patches ->
`model.set_fields`; display names -> `model.add_model` with `label` /
`description`). Roles card: eleven rows (chat, curator, skill gate, session
naming, EverOS llm / embedding / rerank / multimodal, image, speech, video);
each shows model and provider or "follows the chat model"; the pill opens the
model picker filtered to the providers that role may use (media: OpenRouter;
EverOS: providers with a key of their own; others: all connected). Writes:
chat -> `config.set model {value, provider}`; curator, gate, naming ->
`settings.set` on the pair; EverOS -> `settings.everosSet {section, fields:
{model}, borrow_from}`; media -> `settings.set tools.media.<kind>` then
`tools.disabledTools`. The clear button writes null (EverOS: `clear: true`).
Chat parameters fold: reasoning effort, tool-iteration cap (stepper 1-200),
context window follow / fixed. Refusals: disconnect or remove while a role
uses it (page-side, names the roles; roles following chat count through chat);
empty key or address before any write.

**Skills.** Search box filters `ext.list` skills by name and description.
Two cards: built-in (`source == builtin`) and workspace (every other source,
hub-installed skills included, marked with the hub tag); each row name,
description, switch -> `settings.set skillForge.blocklist`. Row click opens
the detail from `skills.manage inspect`: breadcrumb, origin tag, version for
hub skills, "injected every turn" when `always`, uninstall for hub skills
(`skillhub.remove`), file tags each opening through `skills.manage open`,
install metadata line, rendered body.

**Tools.** Eight cards from `toolGroups.json`; each row name, "needs setup"
badge, switch -> `settings.set tools.disabledTools`. Rows with a panel: web
search (vendor select + vendor key), web fetch (same), deep research (key),
image / speech / video (the role pill), understand media (the multimodal role
pill). Tool search group greyed and outside the counter. The "needs setup"
badge is computed from the snapshot: web tools from the chosen vendor's key,
deep research from its key, media tools from their role, understand media
from the multimodal section.

**Plugins.** One card from `ext.list` MCP rows; each row name, status chip
(only while the switch is on), switch -> `plug.toggle`. Chip: connected;
connecting (a toggle or retry in flight); needs setup (`auth != none &&
!credentialed`); failed with a retry button -> `plug.retry`. Row click opens
the panel: OAuth -> authorise / re-authorise (`plug.auth`, URL opened through
`lib/openUrl`) and revoke (`plug.revoke`); apikey -> key field, save / clear
(`plug.configure`); none -> no panel.

**Archive.** Card of archived sessions from `session.list {archived: true}`
with title and `updated_at`, restore (`session.archive {archived: false}`) and
delete (`session.delete`, confirmed). Auto-archive switch ->
`settings.set sessions.autoArchiveAfterDays` with 30 or null; label
"sessions with no new message for 30 days".

**About.** Version from `system.version`; check -> `system.version
{check: true}`; when newer, "upgrade" calls the injected `askUpgrade`. Config
path and storage location with copy buttons.

### Change map

| Where | Today | After |
|---|---|---|
| `raven/rpc/methods/console.py` | `settings.set` whitelist of 20 keys; `settings.usage {days}`; `ext.list` mcp rows without auth | plus 11 keys and one raw list; `from`/`to`/`daily`; `auth` and `credentialed` |
| `raven/rpc/methods/model.py` | options, save_key, disconnect, fetch_models, add_model, remove_model, endpoints | plus `set_fields`, `add_models`, `oauth_login`; `add_model.description`; options carries `key_url` |
| `raven/rpc/methods/plughub.py` | search, detail, install, remove, toggle, auth | plus `retry`, `revoke`, `configure` |
| `raven/rpc/methods/skills.py` | list, inspect (4 fields), search, browse, install | inspect with body, files, install meta; `open` |
| `raven/rpc/methods/session.py` | list hides archived; archive pops the key on restore | `archived: true` filter; lazy auto pass; restore writes false |
| `raven/memory_engine/skill_forge/catalog.py` | blocklist frozen at construction | read through a live reader each time |
| `raven/config/raven.py`, `raven/providers/registry.py` | - | `SessionsConfig`; `ProviderSpec.key_url` |
| `rpc-schema/openrpc.json`, `raven/rpc/models.py`, `ui-web/src/rpc/generated.ts` | 178 methods | plus 6 methods; 5 changed results (`settings.set`, `settings.usage`, `skills.manage`, `model.options`, `ext.list`); 3 changed parameter sets (`settings.usage`, `session.list`, `model.add_model`) |
| `ui-web/src/features/settings/` | one 2880-line page, 13 tabs, `chrome.ts` reaching `islands.*` and `../model/store` | the skeleton above, 8 pages, source-only RPC, no cross-domain store import |
| `ui-web/src/main.tsx` | imports `SettingsApp` from `SettingsPage`, `chrome` | imports from `SettingsApp`, `wire` (three lines) |
| `ui-web/src/components/ModelPicker.tsx`, `ui-web/src/features/model/vendors.ts` | - | new shared popover; vendor mark table |
| `ui-web/src/rpc/fixtures/settings.ts` | 12 methods answered | every method the source calls |
| `i18n/messages.json` | 302 `gui.set.*` keys | `gui.settings.*` for the new page; the `gui.set.*` keys other files reference stay |

## Cost

Who pays: this change's author for the backend batch, the front-end rewrite,
fixtures, tests and the manual pass; the architecture-branch owner for two
reviews outside the settings domain (`components/ModelPicker.tsx`,
`features/model/vendors.ts`) and for the rename pass if the conventions
branch lands after this one; the composer owner for adopting the shared
picker later.

Reversibility: backend changes are additive (new methods, optional fields,
new whitelist entries, one new config section) and revert cleanly one commit
at a time. The blocklist live-read changes a construction-time freeze into a
per-call read; reverting it restores the freeze. Deleting the old page is a
git revert away but leaves the new page as the only one, so the revert has to
take both commits.

Fallback if the front end is abandoned: the backend batch stands on its own
(every method has tests and fixtures) and could be merged alone. Within the
planned single PR the old page can only be deleted once all eight pages
exist, so the PR is not mergeable before the last page.

Known cost not taken: a hot path for the four reload-only keys (a live reader
in the agent loop's wiring for cap and window; re-binding the curator and gate
pins); an OAuth flow for vendors beyond the three; a "reload gateway" action
on the page (would need the control channel exposed to `/rpc`).

## Decisions

- **Dialog, not a page.** The prototype and `state/settings.ts` agree; a page would touch the rail and the router, which are not this change's.
- **React components on the architecture branch, not the prototype's iframe.** The iframe would have to be rewritten by the front-end takeover; the shell is React already. Rejected: `srcdoc` embedding.
- **Domain skeleton per the architecture note, not per-page files under a flat directory.** The note is the standard the conventions branch will gate; the pages live inside the domain as components. Rejected: `pages/` + `parts.tsx` at the domain root without `source.ts`-only RPC.
- **Prototype CSS copied with a `settings-` prefix on every class.** The class-namespace gate demands the prefix; copying keeps the styling pass a pure edit of one block. Rejected: translating the prototype into the current class vocabulary.
- **All text through `t(key)` under `gui.settings.*`; orphaned `gui.set.*` keys deleted.** The i18n-keys gate will check namespace ownership; leaving dead keys makes deleted pages look present. Rejected: reusing the old namespace.
- **Scalars through the `settings.set` whitelist, lists through its raw-list path, structured operations through dedicated methods.** One writer per shape, matching how the existing keys are done. Rejected: one `settings.set` that hides structured operations behind dotted keys.
- **`skillForge.evolveModel` is not offered.** No reader in the host or in `plugins-dist` (D1). Rejected: exposing a knob that does nothing.
- **Session naming keeps its existing model and provider fields; no config field is added, the two keys join the whitelist.** `SessionTitleConfig.provider` exists and `turn.py` reads it.
- **Media roles offer OpenRouter only.** C14. Rejected: listing every provider and failing at generation time.
- **Auto-archive is built, lazily, on the last message time.** The PRD marks it optional; the owner asked for it. Lazy inside `session.list` because listing already scans every file and metadata saves append (C15); no scheduler. "Last message" rather than "last opened" because only the former is recorded; the label says so. Pinned sessions are exempt; a restored session writes `archived: false` and is exempt for good. Rejected: a daily timer; a new "last opened" record.
- **The skill blocklist becomes live, reversing row 10 of the hot-reload design for this key.** A settings switch that silently needs a restart is worse than either option; the tool switch beside it is live already; a name filter is not the composition-level change row 10 was protecting. Rejected: a "restart to apply" toast.
- **Reload-only keys say so.** `settings.set` returns `warning` for the tool-iteration cap, the context window, the curator and gate models; the toast shows it (C9). Rejected: making them live in this change; a reload button (C18).
- **Provider fields other than the key go through one `model.set_fields`, headers as a patch.** Three needs (Azure fields, address-only change, headers) hit the same wall: `save_key` demands a key the page only holds masked; a whole-map header write would send masked values back (C10). Rejected: growing `save_key` a field at a time; whole-map header replacement.
- **Display name and description through `model.add_model`, empty string clears.** One optional parameter and one convention; no new method.
- **Batch add through a new `model.add_models`.** OpenRouter lists many models; one atomic write instead of N. Rejected: N calls from the page.
- **Disconnect and remove guards on the page, not the backend.** The backend would have to resolve eleven roles across six config sections (`agents.defaults`, `context`, `skillForge`, `sessionTitle`, `tools.media`, the memory plugin's) to protect a CLI path this change does not touch.
- **`key_url` lives in the provider registry.** One table, usable by the TUI and onboarding later. Rejected: a front-end table.
- **Provider OAuth from the page through `model.oauth_login`; the page opens the browser through `lib/openUrl`.** The gateway may run on another machine; the browser tab is where the person is. Shape fixed after spike S1 (C17). Rejected: keeping the CLI command on the page; the server calling `webbrowser.open`.
- **Skill files open on the gateway host, like `fs.open`.** The same-host precondition already holds for the workspace page; a download path would be a new mechanism. Rejected: streaming the file to the browser.
- **Retry is its own RPC.** A re-sync skips servers parked in error by design (`connect()` is the explicit retry); toggling is not a retry. Rejected: `reload.mcp`, toggle off and on.
- **Plugin credentials editable after install through `plug.configure`; revoke through `plug.revoke`.** The ledger names the catalog entry (C16), so the template can be re-rendered. Rejected: uninstall and reinstall.
- **Skill list from `ext.list`, detail from an extended `skills.manage inspect`, file open through `skills.manage open`.** `ext.list` already carries the list fields; `fs.open` cannot reach a built-in skill's directory.
- **RPC-driven skill installs are stamped with `trigger: "rpc"`.** Any client's install then shows an install line; the settings page itself has no install entry. Rejected: a `settings` trigger for an entry that does not exist.
- **Tool groups are a front-end JSON table with no "other" bucket, pinned by a Python test against the tool registry.** Every tool is built-in; a fixture-based test would not see a new backend tool, a registry diff does.
- **`tool_search` and `tool_call` are shown greyed, not switchable, and left out of the counter.** Built-in meta tools; the owner's call. `tools.toolSearch.enabled` is not whitelisted.
- **One shared `ModelPicker` in `src/components/`, props only; the vendor mark table in `features/model/vendors.ts` reached through `features/model/types.ts`.** Cross-domain reach is `source.ts` / `types.ts` only; a component both domains render belongs in `components/`. Rejected: settings importing `features/model/`'s component or store.
- **About reuses the existing upgrade flow through `wire.ts` injection.** `app/updates.ts` already runs it; a second state machine would be deleted at takeover; a feature importing `app/` would break the layer order.
- **One PR to the architecture branch, backend commits first.** The RPCs have one caller; landing them on `main` first would add uncalled interfaces there. Rejected: two PRs; a PR to `main`.
- **Backend work starts now; the first front-end commit waits for `refactor/ui_web_conventions` for as long as the backend batch takes.** The rename mapping is applied once and the page.css hash gate is gone by then; the fallback is C20's. Rejected: waiting indefinitely; starting the front end on the old gates.
- **No whole-design alternatives round.** The owner judged the implementation settled after five grill rounds.

## Open questions

Assumptions the execution will confirm; each is a likely row of the
deviations ledger.

1. C17: whether each OAuth device flow can hand the page a URL and a code without a console. Spike S1 decides the `model.oauth_login` shape; if a flow cannot, that vendor keeps the CLI instruction and the item is recorded.
2. C20: whether `refactor/ui_web_conventions` lands before the first front-end commit, and the exact rename mapping it brings.
3. The exact `warning` wording for reload-only keys: "applies after the next gateway reload" alone, or naming the CLI command.
4. Whether the shared vocabulary of `check-class-namespace` accepts the prototype's generic class names once prefixed, or the gate wants them registered (C8 exemption).
5. Whether mounting the new island changes either boot DOM golden that `build.py` compares (284 nodes for the fixture boot, 285 for the live boot, as of the architecture tip); a change is explained in the commit body, not silenced.
6. Where `SessionsConfig` belongs if the config owners prefer `raven/config/schema.py` over `raven/config/raven.py`.
7. The install entry gap: with skills, plugins and the knowledge base removed from the sidebar and no marketplace in settings, nothing installs a new skill or plugin from the web UI. Named for the sidebar owner; not built here.

## Sources

- PRD: "raven web optimisation" memo on Tanka, `https://web.tanka.ai/docs/6aaa0ec8a62b281770c47c39` (Weixiang Chen, updated 2026-09-17), section 3 "settings" (six rows, six screenshots), section 1 "sidebar" (skills, plugins and knowledge base leave the sidebar), section 2 "onboarding". Login required.
- Prototype: the claude.ai artifact `https://claude.ai/artifact/DVhkghoDZucJPmzsCvPwQx` (title "Raven sub-agents"); the settings page is a separate document embedded in it as `SETTINGS_B64` (title "Raven settings prototype", eight pages). That embedded document is the mockup for this design; the PRD's six settings screenshots are captures of it. It is not committed (repository rule on HTML assets); the artifact link is the shared pointer.
- Architecture note "alignment for the settings rewrite" from the `refactor/ui_web_architecture` owner (2026-09-17), circulated by its author and to become `ui-web/CONTRIBUTING.md` at its step S10. Its step numbers: S1 deletes the `page.css` hash gate, S7c is the rename pass, S8 lands the per-domain `styles.css` import.
- Hot-reload design (internal working notes of the 2026-09-03 hot-reload change, not in the repository), row 10 of its landing table: "plugin code / channels / skillforge config: not hot; generation swap". This design reverses that row for the skill blocklist only.
