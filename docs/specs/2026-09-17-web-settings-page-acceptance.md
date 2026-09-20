# Web settings page - acceptance cases

Companion to `2026-09-17-web-settings-page-design.md`. Every case points back
at an acceptance item (A1..A55) of that document; a case that points at no A
tests the implementation, not the requirement, and does not belong here.

How the cases are run: by an agent driving the real host the way a person
would (a real `raven serve` on its own home and port, a real browser through
`playwright-cli`, real files on disk), then by a person for the five starred
cases in the table at the end. Cases marked *contract* call an RPC or a
Python function directly; they are for fast iteration and do not count as
acceptance.

Expected outputs are not written here. They are copied from the real run into
the acceptance report (the package handed over at G4); a value written from
understanding rather than from a run is what this document forbids.

## Evidence rings

Each case names the rings it collects (design of the rings:
`prove-it-works` section 11).

| Ring | What is kept |
|---|---|
| T trigger | the exact click or command, with the time |
| P process | the `web.log` lines produced during the step |
| E effect | before / after of the layer that owns the fact: a `config.json` path read with `jq`, a session file's last metadata line, a directory listing, a credential file's existence |
| V visible | a `playwright-cli snapshot` of the dialog after the step |
| R rerunnable | the command block someone else can paste |

The E ring is the verdict. When it is missing the case fails whatever the
other rings show.

## Chapter 0: the host

Run once per session; rerun after any interruption.

```bash
export RV="$(mktemp -d)/home"; mkdir -p "$RV"
export PORT=18899
lsof -nP -iTCP:$PORT -sTCP:LISTEN && { echo "port busy"; exit 1; }
```

- Seed `$RV/config.json` with two connected key providers (copied from the
  operator's own config with `jq '.providers'`), no EverOS section, no MCP
  servers, `language: zh`. Seed nothing else; each case seeds what it needs.
- Build the page the served process reads (`ui-web/`):
  `npm ci && npm run build && python3 build.py`.
- Start the gateway from a neutral directory, never from the repository (the
  gateway inherits the shell's cwd):
  `(cd "$(mktemp -d)" && RAVEN_HOME="$RV" raven serve --port $PORT & echo $! > "$RV/serve.pid")`.
  The start goes under the shell's `trap ... EXIT` and a hard lifetime cap;
  the shape is the POSIX watchdog in the machine rules.
- Confirm the port holder is this process, not another session's:
  `lsof -tiTCP:$PORT -sTCP:LISTEN` equals `cat "$RV/serve.pid"`.
- Sign in through the token, never through `raven web` (it opens the
  operator's own browser):

```bash
TOKEN="$(jq -r .token "$RV/serve.json")"
AUTH="http://127.0.0.1:$PORT/auth/nonce"
NONCE="$(curl -s -X POST -H "X-Raven-Token: $TOKEN" "$AUTH" | jq -r .nonce)"
playwright-cli goto "http://127.0.0.1:$PORT/auth#$NONCE"
```

- Open the dialog: click the rail's settings button, then the section in
  `#snavList`. Every case below starts from a freshly opened dialog.
- Before each write case: `cp "$RV/config.json" "$RV/before.json"`; after:
  `diff <(jq -S . "$RV/before.json") <(jq -S . "$RV/config.json")` is the E
  ring for config-backed cases.
- Teardown: `kill "$(cat "$RV/serve.pid")"`, wait until the port is free
  (`until ! nc -z 127.0.0.1 $PORT; do sleep 0.2; done`), remove the temp home.
  The operator's own gateway on 18792 is never touched.

Driver notes: `playwright-cli snapshot` for the accessibility tree,
`--boxes` or a screenshot for geometry; transient states (toasts, the
"connecting" chip) are captured with an injected `MutationObserver` through
`playwright-cli eval`, not by polling screenshots. Scripts run under bash, not
zsh (zsh does not split `$P` into words).

## Cases

Columns: case and the A it verifies; whether the real host is driven; steps
(seed, then drive); what is read at the owning layer and why that reading
cannot be faked; when the case is red; rings collected; star.

### General

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T1.1 <- A1 | yes | General > language > English. Then send the probe "Say hello in one word" in the composer. | `jq .language "$RV/config.json"` reads `en` (the gateway's own file); the snapshot of every settings section has no CJK text (`grep -P "[\x{4e00}-\x{9fff}]"` over the snapshots is empty); the reply in the transcript file is English. The file is what the next process reads, so a repaint alone cannot pass it. | any section snapshot still holds CJK; `language` unchanged; the probe is answered in Chinese | T P E V R | |
| T1.2 <- A1 | yes | From T1.1, switch back to Chinese. | `language` reads `zh`; snapshots hold CJK again. | either side fails | T E V R | |
| T1.3 <- A2 | yes | General > theme > dark; reload the tab; then system. | `playwright-cli eval "document.documentElement.dataset.theme"` reads `dark` on the page root (the dialog is inside that root, so page and dialog cannot disagree); after reload still `dark`; after "system" the attribute is absent and the computed body background follows `prefers-color-scheme` (emulate both). | attribute missing after reload; dialog and page differ; "system" pins a theme | T E V R | |
| T1.4 <- A3 | yes | Inject `window.Notification = class { constructor(t,o){ window.__n.push([t,o]) } }` with `window.__n=[]`; switch notifications off; send a turn; switch on; send another. | `window.__n.length` is 0 after the first turn and 1 after the second. The hook is the only path to the OS notification, so the count cannot be produced by the page's own state. | a notification fires while off, or none fires while on | T E R | |

### Usage

Seed: five telemetry files `$RV/telemetry/usage-<date>.jsonl` for the last
five days with rows in the real schema (version 2, one model with cost, one
model with `cost_usd: null`, cache reads on two days, cache writes on one
day, `tool_call` rows for two tools). Keep the seed script; the numbers below
are computed from it with `jq`, never by hand.

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T2.1 <- A4 | yes | Usage > "7 days"; then custom with `from` = day -3, `to` = day -1. | Tiles, bars, model rows and tool rows in the snapshot equal `jq` aggregates over exactly the seeded files in the window (the files are the gateway's only source; a stale widget shows a number the window's files cannot produce). | any widget keeps the previous window's number; the custom range includes day -4 or day 0 | T E V R | |
| T2.2 <- A5 | yes | Custom range over seven days when only five files exist. | Seven bars in the snapshot, two with height 0 and a "0.00" title. | a gap instead of a zero bar; six or eight bars | T V R | |
| T2.3 <- A6 | yes | "30 days". | Calls, cost, output, hit in the tiles equal `jq` sums over all seeded files; hit = cache_read / (input + cache_read) to one decimal. | any tile differs from the jq value | T E V R | |
| T2.4 <- A7 | yes | "30 days". | The `cost_usd: null` model shows "no price" and no dollar figure; the "cache write" column exists (one seeded day wrote cache). Repeat with the cache-write rows removed from the seed and the gateway restarted: the column is gone. | "$0.00" for the priceless model; the column present with no cache writes in the seed | T E V R | |
| T2.5 <- A8 | yes | "30 days". | Tool rows equal the distinct `name` values of the seeded `tool_call` rows with their counts; a tool absent from the seed is absent from the table. | a missing or extra tool row; a wrong count | T E V R | |
| T2.6 <- A4 | contract | `settings.usage` with `from` older than 90 days; with `from > to`; with `from`/`to` plus `days`. | Clamped range in the reply; refusal; `days` ignored. | any of the three misbehaves | - | |

### Model

Seed: the two key providers from chapter 0; the host's real network for
`model.fetch_models` (DeepSeek's `/models` answers without a key on the
registry's default base).

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T3.1 <- A9 | yes | Model page. | The two providers, their ids and model counts equal `jq '.providers \| to_entries[] \| select(.value.apiKey != "")'` over `config.json`; no third row. Then set one provider's `apiKey` to `""` on disk, restart, reopen: that row is gone. | a row for a provider the file does not hold connected; a connected one missing | T E V R | |
| T3.2 <- A10 | yes | Add provider > Moonshot > the key field shows a "get a key" link > paste a key > connect. Later, on the detail page, change only the address and save. | `jq .providers.moonshot.apiKey` equals the pasted key (a value the seed never held); the field reads back as bullets plus the last four; `web.log` for the later save carries no `settings.set`/`save_key` frame whose value starts with a bullet. | key not on disk; readback shows more than four trailing characters; a bulleted value is sent | T P E V R | *1 |
| T3.3 <- A11 | yes | Add provider > Ollama > leave the address empty > connect; then fill `http://127.0.0.1:11434` > connect. | First attempt: `config.json` unchanged (`diff` empty) and the field is focused; second: `jq .providers.ollama_chat.apiBase` equals the address and the row is connected. | a write on the empty attempt; no row after the filled one | T E V R | |
| T3.4 <- A12 | yes | Add provider > Azure OpenAI > key, address, deployment `gpt-4o-eu`, API version `2024-10-21` > connect. | `jq .providers.azure_openai` holds all four; reopening the detail shows the deployment and version. | any of the four missing on disk or in the readback | T E V R | |
| T3.5 <- A13 | yes, person | Add provider > MiniMax Global > authorise in browser, with a real MiniMax account. | The dialog shows a URL and a code; a new tab opens on that URL through `lib/openUrl` (the page's only opener); after completing the code, `model.options` (read through the page: the provider row turns connected) and the token file `raven/providers/minimax_oauth.py` `token_path("global")` exists. Nothing appears on the gateway's console. | the code is only in `web.log`/console; the row never turns connected while the token file exists | T P E V R | *3 |
| T3.6 <- A13 | contract | `model.oauth_login` against a fake device endpoint; let the code expire. | Reply carries `verification_uri`, `user_code`, `expires_in`; after expiry `model.options` still reports not authenticated and no poller task is alive. | missing fields; a poller outliving the code | - | |
| T3.7 <- A14 | yes | Set the chat role to DeepSeek and the curator role to Moonshot (T3.11); on DeepSeek's detail press disconnect; then clear the curator, set it to "follow the chat model", and press disconnect on Moonshot. | First: `config.json` unchanged and the message names "chat" (and any follower); second: Moonshot's `apiKey` is gone and the row is gone (following roles count through chat, so Moonshot has no user). | a disconnect that writes while a role uses it; a refusal when no role does | T E V R | |
| T3.8 <- A15 | yes | DeepSeek detail > models > add: the vendor list loads; tick two, type `my-model`, add. Then on Ollama (no list endpoint) open add. Then remove a model the chat role uses. | `jq .providers.deepseek.models` gains exactly the three, in one `web.log` write frame (`model.add_models`); Ollama's sheet shows the "no list, type an id" note; the removal is refused with "chat" in the message and the list unchanged. | three separate writes; the sheet spinning on Ollama; a used model removed | T P E V R | |
| T3.9 <- A16 | yes | Moonshot detail > address > `https://api.moonshot.cn/v1` > save. | `jq .providers.moonshot.apiBase` equals the new address; `apiKey` unchanged; no key was sent (`web.log` frame is `model.set_fields`). | refused for a missing key; the key changed | T P E V R | |
| T3.10 <- A17 | yes | Moonshot detail > headers > add `X-App: alpha`; add `X-Env: beta`; remove `X-App`. | After the first add `jq .providers.moonshot.extraHeaders` is `{"X-App":"alpha"}`; after the second both; after the removal only `X-Env`; the frames carry one header each (patch), and the list shows names with bulleted values ending in the last four characters. | a frame carrying both headers on the second add; a bulleted value in a frame; the wrong header removed | T P E V R | |
| T3.11 <- A18 | yes | DeepSeek detail > display names > model `deepseek-v4-flash`, label "Flash", description "cheap"; open the chat role's picker; then clear the label. | `jq .providers.deepseek.modelOverlay["deepseek-v4-flash"]` holds label and description; the picker row shows "Flash" with "cheap" as its second line; after clearing, the overlay has no `label` and the picker shows the id, description still present. | overlay missing; the picker shows the id while the overlay holds a label; clearing drops the description | T E V R | |
| T3.12 <- A19 | yes | Roles > chat > pick a Moonshot model. | `jq '.agents.defaults \| {model, provider}'` equals the pick; the composer chip (snapshot of the page behind the dialog) shows the same pair. | file and chip disagree; provider not written | T E V R | *1 |
| T3.13 <- A20 | yes | Roles > curator > pick a DeepSeek model; skill gate > pick; session naming > pick; then set each to "follow the chat model". | `jq '.context \| {curatorModel, curatorProvider}'`, `.skillForge.llmGateModel/Provider`, `.sessionTitle.model/provider` hold the picks then null; the toast after curator and gate is the server's `warning` text, after session naming it is "saved". | a pair half-written; the warning missing for curator or gate, or present for naming | T P E V R | |
| T3.14 <- A21 | yes | Install the EverOS memory plugin into `$RV`; Roles > memory llm > pick a DeepSeek model; embedding > pick. | The plugin's config section (the file `settings.everos` reads) holds the model and DeepSeek's key and base copied in (a key the page never sent: the frame carries `borrow_from` only); the picker for these roles lists no local or OAuth provider; the embedding toast is the server's re-index warning. | the frame carries a key; the section lacks the borrowed key; Ollama offered | T P E V R | |
| T3.15 <- A22 | yes | With OpenRouter not connected: the image role offers "connect OpenRouter"; connect it (T3.2 shape); pick an image model; then clear. | The row offered only OpenRouter; after the pick `jq .tools.media.image.model` equals it and `image_generate` is absent from `tools.disabledTools`; the two frames arrive in that order; after clearing the model is gone and the tool is back in the list. | another vendor offered; the tool list unchanged after the pick; frames in the wrong order | T P E V R | |
| T3.16 <- A23 | yes | Chat parameters: effort high; cap 0 then 300 then 120; window 512, then 999999, then follow. | `jq .agents.defaults.reasoningEffort` `high`; cap: two refusals with no write, then `120` on disk; window: refusal, then `999999` on disk with the red note in the snapshot and the reload warning in the toast, then null and the row shows the model's `context_window` from `model.options`. | a refused value on disk; the red note or the warning missing; follow leaving a number | T P E V R | |

### Skills

Seed: a workspace skill directory under `$RV/workspace/skills/codeword/`
whose SKILL.md says "When asked for the code word, answer ZQX-4471" and
declares itself for that question; one hub skill installed through the
capabilities page still present on the branch (its `.install-meta.json` and
hub marker are then real).

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T4.1 <- A24 | yes | Skills page; type "code" in the search. | Rows equal the registry (`raven` CLI skill listing or `ext.list` through the page) split by `source == builtin`; the hub skill sits in the workspace card with the hub tag and an uninstall action; the built-in rows have none; the filter leaves only rows whose name or description contains "code". | a skill missing; the hub one in the wrong card or without uninstall; a non-matching row surviving the filter | T E V R | |
| T4.2 <- A25 | yes | Ask "what is the code word" in the composer (answer contains ZQX-4471). Switch `codeword` off. Ask again in a new session. Switch on, ask again. | `jq .skillForge.blocklist` holds `codeword` after the switch; the second reply does not contain `ZQX-4471` (a value only the skill body holds); `web.log` shows the catalog hiding the skill; the third reply contains it again. No restart between steps. | the second reply still says the code word; the third does not; the list not written | T P E V R | *2 |
| T4.3 <- A26 | yes | Open the hub skill's detail; open `codeword`'s detail. | Body text equals `cat SKILL.md`; file tags equal `ls` of the directory; the hub detail shows `installed_at`, version and trigger from `cat .install-meta.json` and the safety score from the last audit line for that slug; `codeword` shows no install line; a skill listed in `context.pinnedSkillIds` shows "injected every turn". | any field differs from the file; an install line on `codeword` | T E V R | |
| T4.4 <- A27 | yes | On `codeword`'s detail click a file tag; on a built-in skill's detail click one. Then call `skills.manage open` with `file: ../../config.json`. | `web.log` records the opener call with the absolute path inside the skill directory for both; the traversal is refused with no opener call. | the built-in open refused; the traversal opened | T P E R | |
| T4.5 <- A28 | yes | Hub skill detail > uninstall > cancel; again > confirm. | After cancel the directory exists; after confirm `ls` of the directory fails and the row is gone. | removed on cancel; present after confirm | T E V R | |

### Tools

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T5.1 <- A29 | yes + pytest | Tools page. | Every tool name from `ext.list` (through the page's fixture check or the CLI) appears exactly once across the eight cards, minus `tool_search`/`tool_call`; the counter equals switchable rows on / total; `tests/test_settings_tool_groups.py` diffs `toolGroups.json` against the registry. | a tool in two cards or none; counter off; the pytest red | T E V R | |
| T5.2 <- A30 | yes | Switch `exec` off; ask the agent to run `date` in a new session; switch on; ask again. Then switch on `deep_research` with no key. | `jq .tools.disabledTools` holds `exec`; the first reply's transcript has no `exec` tool call and says the tool is unavailable; the second has one; for deep research the list no longer holds it, its panel is open and the badge stays. | an `exec` call while disabled; the panel closed; the badge gone without a key | T P E V R | |
| T5.3 <- A31 | yes | `web_search` panel: vendor Tavily, key `tvly-test-1`, save; clear. `deep_research`: key, save; clear. | `jq .tools.web.search.provider` `tavily` and `.tools.web.providers.tavily.apiKey` the key, badge gone; after clear the key is `""` and the badge back; same for `.tools.deepResearch.apiKey`. | badge state disagrees with the file | T E V R | |
| T5.4 <- A32 | yes | In the Tools page pick an image model through the row's pill; open the Model page. | The image role row shows the same model; `jq .tools.media.image.model` matches. | the two pages disagree | T E V R | |
| T5.5 <- A33 | yes | Tool search card. | Both rows are `aria-disabled`, have no switch role in the snapshot, carry the built-in note; the counter's total equals the switchable rows (T5.1). | a working switch; the two counted | T V R | |

### Plugins

Seed MCP servers in `config.json`: `ctx7` (streamable HTTP, public
context7), `dead` (`http://127.0.0.1:9/mcp`, connection refused), and one
installed from the catalog with an API key form (`github`, key `ghp_seed`).
For the retry case a local stub MCP server is started on a free port only
when the case calls for it and killed at its end.

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T6.1 <- A34 | yes | Plugins page after the gateway has connected. | Rows equal `jq '.tools.mcpServers \| keys'`; `ctx7` connected, `dead` failed with a retry button, `github` connected or needs setup per its key; switch `ctx7` off: its chip disappears; counter equals connected rows / total. | a row missing; a chip on a switched-off server; counter off | T E V R | |
| T6.2 <- A35 | yes | Switch `ctx7` off; ask the agent to use a context7 tool; switch on; ask again. Switch `github` on with an empty key. | `jq .tools.mcpServers.ctx7.enabled` false then true; the first transcript has no `mcp_ctx7_*` call and the second has one; for `github` `enabled` is true on disk, the panel is open, and `web.log` shows no connect attempt for it. | a tool call while off; a connect attempt without a credential | T P E V R | |
| T6.3 <- A36 | yes | With `dead` failed, start the stub MCP server on port 9's replacement (edit the URL to the stub's port first, restart, let it fail once with the stub down), then bring the stub up and press retry. | `web.log` shows the manager's explicit connect for `dead` and the chip turns connected; `enabled` on disk never changed during the step; no gateway restart (`serve.pid` unchanged, process start time unchanged). | the chip stays failed with the stub up; `enabled` flipped; the pid changed | T P E V R | |
| T6.4 <- A37 | yes | `github` panel: paste `ghp_new`, save; then clear. | `jq .tools.mcpServers.github.headers` (or `.env`, whichever the template names) holds `ghp_new` (a value the seed never held) and the readback says only "set"; after clear the value is `""` and the chip reads needs setup; the manager reconnects (`web.log`). | the old key still on disk; the readback shows characters of the key; no reconnect | T P E V R | *5 |
| T6.5 <- A38 | yes, person | Install an OAuth catalog server (asana) into `$RV`; authorise; revoke. | A new tab opens through `lib/openUrl`; after consent the credential file at `raven/mcp/oauth.py` `credentials_path("asana")` exists and the chip is connected; after revoke the file is gone and the chip reads needs setup. | no file after consent; the file surviving revoke | T P E V R | |
| T6.6 <- A39 | yes | Add a hand-written server `local` (stdio, `npx -y @playwright/mcp@latest`) to `config.json`, restart. | Its row shows switch, chip and retry; opening it shows no credential panel; `ext.list` (through the page) reports `auth: none`. | a credential panel on a ledgerless server | T E V R | |

### Archive

Seed 500 session files under `$RV/workspace/sessions/web/` with a metadata
record each: 300 with `last_message_at` 40 days ago, 10 of those `pinned`,
200 dated yesterday; three more archived by hand (`archived: true`) with
titles "arch-1..3".

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T7.1 <- A41 | yes | Archive page; restore "arch-1"; delete "arch-2" (confirm). | The list shows arch-1..3 with their `updated_at`; after restore `arch-1`'s file has a last metadata record with `archived: false` and the rail lists it at its `updated_at` position; after delete `arch-2`'s file is gone. | a listed session missing; restore not persisted; the file surviving delete | T E V R | |
| T7.2 <- A42 | yes | Archive > auto-archive on; reopen the rail (a `session.list`). Then restore one auto-archived session and list again. | `jq .sessions.autoArchiveAfterDays` is 30; exactly 290 files (300 stale minus 10 pinned) gained one appended record `{"archived": true, "archivedBy": "auto"}` (`tail -1` of each); the 200 fresh and the 10 pinned gained none; the restored one holds `archived: false` and is not re-archived by the second list. | a pinned or fresh file archived; a stale one missed; the restored one re-archived | T E V R | *4 |
| T7.3 <- A43 | yes | Auto-archive off; list again with the seed restored. | `autoArchiveAfterDays` is null; no file gained a record. | a record appended while off | T E V R | |
| T7.4 <- A44 | yes + pytest | The T7.2 first list, timed; then the second list. | Appended records: exactly one per stale unpinned file on the first list, zero on the second (`wc -l` before / after per file); wall-clock of both `session.list` frames recorded from `web.log` timestamps into the report; `tests/test_rpc_session.py::test_auto_archive_never_loads_transcripts` asserts with a spy that `_load` is never called by the pass. | more than one record on any file; any record on the second list; the spy sees a load | T P E R | |

### About

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T8.1 <- A46 | yes | Save a reload-only key (T3.16's cap) and a live key (T1.1's language). | The toast text after the cap equals the `warning` string in the `settings.set` reply frame (the E ring: the frame is what the server said, the toast only repeats it); after language it is the "saved" string. | "saved" after the cap; the warning after language | T P E V R | |
| T8.2 <- A47 | yes | Prime the update cache `raven.updates.update_notice.read_cache` reads with `latest_version: "0.0.1"`; About > check. | The row shows the real latest release (not `0.0.1`), proving the cache was bypassed; `web.log` shows the `system.version` frame with `check: true`; when the running build is older than that release the row offers "upgrade" and pressing it opens the existing upgrade shade (snapshot). | the row shows `0.0.1`; no upgrade offer when behind | T P E V R | |
| T8.3 <- A48 | yes | About page; press copy on the config path. | The two rows equal `jq -r .agents.defaults.workspace "$RV/config.json"` and `$RV/config.json`; `playwright-cli eval "navigator.clipboard.readText()"` equals the path exactly. | a path differs; clipboard content differs | T E V R | |

### Cross-cutting

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T9.1 <- A49 | yes (offline) | Open `dist/index.html?stub=1` from disk with no gateway; visit all eight pages; press one write on each. | Each page mounts (snapshot non-empty, no console error in `playwright-cli` console log) and shows the fixture values; every write shows the offline note and no exception reaches the console. | an empty page; a thrown error; a write pretending success | T V R | |
| T9.2a <- A50 | yes | Open the dialog in Chinese, then in English. | `#snavList` in the snapshot has exactly eight entries in both languages and none named for permissions, proactivity, exec, channel, data or shortcuts; the eight are the prototype's, in order. A hidden-but-present page would still leave its nav button or its keys somewhere the command check catches; the snapshot catches a shell that still draws it. | a ninth entry; a dropped name; wrong order | T V R | |
| T9.3a <- A51 | yes | Visit all eight pages in Chinese, then in English. | No snapshot contains a string matching `gui\.settings\.` (a missing locale shows as the raw key) and no page shows English while `language` is `zh` or Chinese while it is `en`. | a raw key visible; a mixed-language page | T V R | |
| T9.2 <- A50 | command | The C11 script; `test -e` on the four deleted files; the component grep. | All four paths absent; the grep empty; every remaining `gui.set.*` key referenced outside the settings directory. | any path exists; a hit; an unreferenced key | R | |
| T9.3 <- A51 | command | The C5 locale script; the literal-text grep. | Every `gui.settings.*` key has `en` and `zh`; the grep is empty. | a missing locale; a literal | R | |
| T9.4 <- A52 | command | `npm run type-check && npm test && npm run build && python3 build.py` in `ui-web/`. | Exit 0 for all four; `git log` of the branch explains any golden or snapshot change in the commit body. | any non-zero exit; an unexplained golden change | R | |
| T9.5 <- A53 | command | For each new or changed RPC: revert its handler body (`git stash` is not used; a scripted patch), run its test, restore, `git diff` clean. | Each test red on the revert, in the assertion it names, and green after restore. | a test green on the revert, or red in an import/fixture error | R | |
| T9.6 <- A54 | command | The C8 diff command. | Empty output; `main.tsx` diff at most three lines. | any line | R | |
| T9.7 <- A55 | yes | A fresh `$RV` with no provider; open the served page. | The onboarding flow appears, connects a provider through `model.save_key`, picks a model through `config.set`, and the main page opens; `config.json` holds the pair. | onboarding broken by the changed `model.options` shape | T E V R | |
| T9.8 <- A56 | yes | Write a `github` stanza by hand into `config.json` (a catalog name with no ledger entry), restart; Plugins > `github` > type a token > update; then clear. | The toast reads the server's sentence ("not installed from the catalog; edit the config file instead") and carries no `config_validation_error`; the same call over the socket answers with `data` holding both `field` and `detail`. | a toast showing the code; a refusal frame whose `data` has no `detail` | T E V R | |

A52, A53 and A54 are gate items: their subject is the branch, not the running page, so they have command cases only. Their link to the host is chapter 0's build step, which serves the very branch the gates ran on.

## Contract-level cases (not acceptance)

One pytest per backend row of the design's "Backend changes" table, named
in that table's Tests column; one vitest per module of the settings domain
(`<module>.test.tsx`); the fixture test of C6; the store-shape test of C3.
These are the fast loop during implementation and the base for the mutation
check T9.5.

## Starred cases: the manual pass

At most five. A person runs these after the agent's pass, on the same host
setup, and writes what they saw next to what the report recorded.

| * | Case | Why a person | Steps for the person |
|---|---|---|---|
| 1 | T3.2 + T3.12 | The main journey: connect a provider, make it the chat model. Everything else assumes this works. | Open settings > Model > add provider > pick a vendor you hold a key for > paste > connect. Open its detail: the key shows bullets and four characters. Roles > chat > pick a model of that vendor. Close the dialog: the composer chip shows that model. Send one message and read the reply. |
| 2 | T4.2 | Live effect of a switch on the next turn, no restart; the one behaviour this design reversed a prior ruling for. | Ask "what is the code word". Skills > switch `codeword` off. New session, ask again: no code word. Switch on, ask again: the code word is back. |
| 3 | T3.5 | Real vendor OAuth with a real account; credentials leave the machine; cannot be automated honestly. | Model > add provider > MiniMax Global > authorise in browser. A tab opens with a code shown in the dialog; complete it in the vendor's page. Back in Raven the row is connected. |
| 4 | T7.2 | Data-affecting automation over the person's own sessions; irreversible in feel even though restore exists. | With the seeded sessions: Archive > auto-archive on. Open the rail: the stale ones are gone, the pinned ones stay. Restore one from the archive page; reopen the rail twice: it stays. |
| 5 | T6.4 | Rewriting a stored credential in place; a wrong write here leaks or breaks a real integration. | Plugins > `github` > paste a new token > save. The row reconnects; the field shows only "set". Clear: the row reads needs setup. |

## Red flags for whoever runs this

- A case whose E ring is a snapshot: the verdict must come from the file, the
  transcript or the credential store, not from the dialog.
- A number in the report that was typed, not copied.
- A case run against the operator's own gateway on 18792.
- A "connecting" or toast observation taken by screenshot polling instead of
  the injected observer.
- A skipped case marked green.
