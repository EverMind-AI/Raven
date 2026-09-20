# Settings model section - acceptance cases

Companion to `2026-09-20-web-settings-model-section-design.md`. Every case
points back at an acceptance item (A1..A38) of that document; a case that
points at no A tests the implementation, not the requirement, and does not
belong here. Where a prior case already verifies an unchanged behaviour it is
cited from `2026-09-17-web-settings-page-acceptance.md` as "prior T3.9" and
not restated.

How the cases are run: by an agent driving the real host the way a person
would (a real `raven serve` on its own home and port, a real browser through
`playwright-cli`, real files on disk), then by a person for the five starred
cases in the table at the end. Cases marked *contract* call a Python function
in-process or run a pytest; they are for fast iteration and do not count as
acceptance. Cases marked *command* run the command their A names; their
subject is the branch, not the running page.

Expected outputs are not written here. They are copied from the real run into
the acceptance report (the package handed over at G4); a value written from
understanding rather than from a run is what this document forbids.

## Evidence rings

| Ring | What is kept |
|---|---|
| T trigger | the exact click or command, with the time |
| P process | the `web.log` RPC frames produced during the step (method name and redacted params) |
| E effect | before / after of the layer that owns the fact: a `config.json` path read with `jq`; an RPC reply obtained in-process from the same home; a DOM attribute the page's own state writes, read with `playwright-cli eval` |
| V visible | a `playwright-cli snapshot` (accessibility tree) after the step; `--boxes` where geometry is the fact |
| R rerunnable | the command block someone else can paste |

The E ring is the verdict. For a fact the page owns (which section is
open, where a popover sits) E is a DOM read, never a screenshot; for a fact
the config owns it is the file; for a fact the classifier owns (a model's
kind, a provider's gateway flag) it is the RPC reply obtained in-process.

## Chapter 0: the host

Run once per session; rerun after any interruption. Every command needed is
in this chapter; the task's working directory keeps them as scripts for
convenience, but nothing below depends on those files.

```bash
export RV="$(mktemp -d)/home"; mkdir -p "$RV"
export PORT=18899
lsof -nP -iTCP:$PORT -sTCP:LISTEN && { echo "port busy"; exit 1; }
```

- Prerequisite: the person running this holds working API keys for DeepSeek,
  OpenRouter and Gemini in their own `~/.raven/config.json` (the prior run's
  host had the same three). Nothing else is taken from that file.
- Seed `$RV/config.json` from those three sections with their `models`,
  overlays, headers and protocol overrides stripped and the table below put
  in their place -- the operator's own lists must not leak into any count:

```bash
jq -n --argjson p "$(jq '.providers | {deepseek, openrouter, gemini}
    | with_entries(.value |= del(.models, .modelOverlay, .extraHeaders, .modelProtocols))' ~/.raven/config.json)" '
  {language: "zh",
   providers: ($p
     | .deepseek.models   = ["deepseek-v4-pro", "deepseek-v4-flash"]
     | .openrouter.models = ["anthropic/claude-sonnet-4-5", "openai/text-embedding-3-small",
                             "BAAI/bge-reranker-v2-m3", "google/gemini-2.5-flash-image"]
     | .gemini.models     = ["gemini-2.5-flash"]),
   agents: {defaults: {model: "deepseek-v4-pro", provider: "deepseek"}}}' > "$RV/config.json"
```

  No MCP servers, no EverOS section (the EverOS roles are offered because the
  plugin is installed in the venv, not because a section exists). Keys are
  copied, so the home is deleted at teardown. Why these models:

  | Provider | `models` | Why |
  |---|---|---|
  | deepseek | `deepseek-v4-pro`, `deepseek-v4-flash` | two text models; the chat role's provider |
  | openrouter | `anthropic/claude-sonnet-4-5`, `openai/text-embedding-3-small`, `BAAI/bge-reranker-v2-m3`, `google/gemini-2.5-flash-image` | one of each kind the slots filter on: text, embedding and reranker by the name patterns (the registry carries no embedding or rerank rows for OpenRouter), image by the registry row |
  | gemini | `gemini-2.5-flash` | a third connected provider with a text model |

  Roles: the chat pair the transform sets; every other role unset. Keep the
  transform as `seed.sh`; every count below is computed from the seed and the
  RPC replies, never by hand.
- Build the page the served process reads: in `ui-web/`,
  `npm ci && npm run build && python3 build.py`.
- Start the gateway from a neutral directory, never from the repository:
  `(cd "$(mktemp -d)" && RAVEN_HOME="$RV" raven serve --port $PORT & echo $! > "$RV/serve.pid")`,
  under the shell's `trap ... EXIT` and the POSIX watchdog of the machine
  rules; confirm `lsof -tiTCP:$PORT -sTCP:LISTEN` equals `cat "$RV/serve.pid"`.
- Sign in through the token, never through `raven web`:

```bash
TOKEN="$(jq -r .token "$RV/serve.json")"
NONCE="$(curl -s -X POST -H "X-Raven-Token: $TOKEN" "http://127.0.0.1:$PORT/auth/nonce" | jq -r .nonce)"
playwright-cli open "http://127.0.0.1:$PORT/auth#$NONCE"
```

- The in-process RPC reader, for the facts the classifier owns. It reads the
  same `$RV` config the gateway reads and runs the same `kind_of`, so its
  reply is the owning layer for `kind` and `gateway`, not a second opinion:

```bash
RPC()  { RAVEN_HOME="$RV" uv run --quiet python -c "import asyncio,json; from raven.rpc.methods.model import $1 as f; print(json.dumps(asyncio.run(f($2))))"; }
RPC2() { RAVEN_HOME="$RV" uv run --quiet python -c "import asyncio,json; from $1 import $2 as f; print(json.dumps(asyncio.run(f($3))))"; }
RPC model_options '{}'            > "$RV/options.json"
RPC model_fetch_models '{"slug":"openrouter"}' > "$RV/fetch_openrouter.json"
# a section another module owns, e.g. the EverOS roles (raven/rpc/methods/console.py):
RPC2 raven.rpc.methods.console settings_everos '{}'
```

  Both helpers run from the repository so `uv run` finds the project; the
  gateway itself is started from a neutral directory (below).

- Open the dialog: click the rail's settings button, then the entry in
  `#snavList`. Every case below starts from a freshly opened dialog unless it
  says "from T...".
- Before each write case: `cp "$RV/config.json" "$RV/before.json"`; after:
  `diff <(jq -S . "$RV/before.json") <(jq -S . "$RV/config.json")` is the E
  ring for config-backed cases.
- Teardown: `kill "$(cat "$RV/serve.pid")"`, wait until the port is free
  (`until ! nc -z 127.0.0.1 $PORT; do sleep 0.2; done`), remove the temp
  home (it holds copies of real keys). The operator's own gateway on 18792 is
  never touched.

Driver notes: `playwright-cli snapshot` for the accessibility tree,
`playwright-cli eval` for DOM attributes and bounding boxes; the RPC frames
come from `$RV/web.log`. Scripts run under bash, not zsh.

## Cases

Columns: case and the A it verifies; whether the real host is driven; steps
(seed, then drive); what is read at the owning layer and why that reading
cannot be faked; when the case is red; rings collected; star.

### Navigation

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T1.1 <- A1 | yes | Open settings. Click "Model providers", then "Model settings". | `#snavList` snapshot lists nine entries with the two new ones third and fourth; after each click `playwright-cli eval "document.querySelector('.settings-panel').dataset.section"` reads `provider` then `model` -- the attribute is written from the store's tab, so it cannot say one page while another is drawn. The `provider` panel contains `.settings-tp`; the `model` panel does not. | an entry missing or misordered; `model` still drawing a provider list | T E V R | |
| T1.2 <- A2 | yes | Model settings. Then a seed variant with openrouter's `apiKey` removed: the image slot; click its button. Then a seed variant with no provider key at all: the chat slot; click its button. | Snapshot: the roles card and the chat-parameters fold, no `.settings-tp`. First variant: the image slot shows the "connect OpenRouter" button and the click lands on `dataset.section == 'provider'` with openrouter selected in the right pane. Second: every slot shows the no-provider text and the click lands on `provider`. In the unmodified seed every slot opens the picker, the speech slot included although its one allowed provider has no audio model (T4.3). | a slot with a connected provider shows an empty state instead of the picker; the button lands elsewhere | T E V R | |

### Model providers, left column

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T2.1 <- A3 | yes | Model providers. | Row count in the snapshot equals `jq '.providers \| length' "$RV/options.json"` (fifty-five on the stock registry; `hosted_vllm` and `custom` present by name); the first three rows are the seeded providers with a dot, in registry order; the rest follow registry order (`jq -r '.providers[].slug'` with `authenticated` sorted first is the expected sequence); `eval` counts `.settings-tp-tile` elements with an image or svg child = rows minus one, the one lettered tile being `custom`'s; the row for `deepseek` carries the default badge and no other row does. | count differs from the reply; a registry provider missing; badge elsewhere; a marked provider drawn as letters | T E V R | * |
| T2.2 <- A4 | yes | Type `moon` in the search box; then `zzz`; then clear. | Rows equal the reply's providers whose `name` or `slug` contains the term (`jq` filter) -- one; then none, and the snapshot shows the `zh` value of `gui.model.prov_no_match` read from `i18n/messages.json` at run time -- the string, not the key: a missing locale falls back to the raw key and must read as red; then the full count again. | a row outside the filter; the empty text absent; clearing not restoring | T E V R | |
| T2.3 <- A5 | yes | Open the filter menu; choose each of the six in turn. | For each choice the set of row slugs (from `eval` over `.settings-tp-row [data-id]`) equals the `jq` set over `options.json`: connected = `authenticated`; gateway = `gateway == true`; browser auth = `auth_type == "oauth"`; local = `auth_type == "local"`; direct = `gateway == false and auth_type in ("key","endpoint")`; all = every slug. The reply is the owning layer for `gateway`, which the page cannot derive. | any set differs by one slug | T E V R | |
| T2.4 <- A6 | yes | Model providers, fresh open. Click the OpenRouter row. Then a seed variant with `agents.defaults.model` and `.provider` removed; reopen. | First open: the right pane's head reads DeepSeek (the default badge's row); after the click it reads OpenRouter and `dataset.section` is still `provider`. Variant: the right pane holds the one-line choose-a-provider prompt and the left column is unchanged (same row count). | the right pane opens on another provider; the click navigates away; the variant shows a provider anyway | T E V R | |

### Model providers, right column

Seed additions per case are named in the steps. The vendor list cases use
the host's real network (OpenRouter's `/models` answers without a key; the
seeded key is sent).

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T3.1 <- A7 | yes | Click, in turn, deepseek, moonshot, minimax_global, ollama_chat. | Each head shows the mark and the name; a link only when `options.json` carries `homepage` or `key_url` for that slug; the status line reads connected / needs an API key / needs authorisation / needs an address, matching `authenticated` and `auth_type` in the reply. | a status line disagreeing with the reply; a link for a provider the reply gives none | T E V R | |
| T3.2 <- A8 | yes | Moonshot: paste a key, press the eye, press it again, connect. Reopen the row. | `eval` on the key input's `type` reads `text` then `password`; after connect `jq .providers.moonshot.apiKey` equals the pasted string and the status reads connected; on reopen the field is empty with the "update" placeholder, and no frame in `web.log` after the connect carries the stored key (T3.3 checks the address save). | the stored key appears in any frame or field; the eye does not flip the type | T P E V R | |
| T3.3 <- A9 | yes | From T3.2 (Moonshot connected). OpenRouter: the address field is in the body. Moonshot: open Advanced, set the address to `https://api.moonshot.cn/v1`, save; press reset. | `eval`: openrouter's address input is not inside the Advanced fold; moonshot's is. The save's frame is `model.set_fields` with no `api_key` -- the stored key never returns to the wire; after save `jq .providers.moonshot.apiBase` equals the address and `apiKey` is unchanged (`diff` shows one path); after reset `apiBase` is gone or equals the registry default and the button disappears. | the field in the wrong place; the key changed; reset writing something else | T P E V R | |
| T3.4 <- A10 | yes | Azure OpenAI: key, address, deployment `gpt-4o-eu`, API version `2024-10-21`, connect. | `jq .providers.azure_openai` holds all four (prior T3.4's check); the two Azure fields sit under the address in the body. | any field missing | T E V R | |
| T3.5 <- A11 | yes | MiniMax Global: read the pane; press "authorise in browser"; wait for the code. | Snapshot: the prototype's layout -- head, an authorisation section, no key field, no CLI instruction; after the press one `model.oauth_login` frame, and the device code and URL drawn in the pane (the flow starts without an account; only completing it needs one). | the CLI text appears; no code within the reply's `expires_in` | T P V R | |
| T3.5b <- A11 | yes, person | From T3.5: complete the flow with a real MiniMax account; then disconnect. | The row turns connected without a reload (prior T3.5's check); the footer reads "disconnect authorisation" and disconnecting clears it. The report states "ran" or "skipped: no account" on this line -- a blank is a failure of the report, not a pass. | connected never appears; the footer worded for a key provider | T P E V R | |
| T3.6 <- A12 | yes | Ollama: fill `http://127.0.0.1:11434`, connect. | No key field in the snapshot; `jq .providers.ollama_chat.apiBase` equals the address (prior T3.3's second half). | a key field drawn; no write | T E V R | |
| T3.7 <- A13 | yes | DeepSeek: the model tags. Remove `deepseek-v4-flash`. Then try to remove `deepseek-v4-pro`. | Tags equal `jq .providers.deepseek.models`; after the first removal the array lost that id (one `model.remove_model` frame); the second is refused with a toast naming the chat role and `diff` is empty. | a removal the role uses goes through; a tag not in the config | T P E V R | |
| T3.8 <- A14 | yes | OpenRouter: press "add model". Wait for rows. Click `anthropic/claude-opus-5`; click it again. Then Ollama: press "add model". | `eval` boxes: the popover's top is at or below the button's bottom. Row count equals `jq '.models \| length' "$RV/fetch_openrouter.json"`; rows the reply tags show `.model-tags`. First click: one `model.add_model` frame and `jq .providers.openrouter.models` gains the id, the tag appears; second: one `model.remove_model` frame and it is gone. Ollama: the no-list text and a typed-id row. | a batch button instead of a per-click write; counts differ; the popover above the button with room below | T P E V R | * |
| T3.9 <- A15 | yes | From T3.8, before any click: read the kind row. Type `gemini`; read it again. Choose the image tab. | Tab labels and counts equal `jq` counts of `.models[].kind` over `fetch_openrouter.json` (all, then per kind present; a kind with zero rows has no tab); after typing, the counts are over the rows whose id or label contains `gemini`; with the image tab chosen every visible row's id has `kind == "image"` in the reply. The reply is the classifier's output, so a count that agrees with it is a count the page did not invent. | any count off by one; a tab for an absent kind; a text row under the image tab | T E V R | * |
| T3.10 <- A16 | yes | From T3.8: fold and unfold the `mistralai` group; press its "add group". Then DeepSeek's popover. | Groups in the snapshot equal the distinct `vendor/` prefixes of the reply, rows without a prefix in one trailing unlabelled group; "add group" produces one `model.add_models` frame whose `models` equal the group's unadded ids and `jq` shows them all; DeepSeek's popover has no group heads. | two frames for one group; a prefixed row outside its group; a group head on a flat list | T P E V R | |
| T3.11 <- A17 | yes | First, for each id below, `jq -e '.models[] \| select(.id == "<id>")' "$RV/fetch_openrouter.json"` exits non-zero and the id is not in the seed (so the typed row, not a listed row, is what appears). OpenRouter popover: type `my-team/bge-reranker-custom`; read the chip; click the chip six times; set it to embedding; add. Then type `my-text-model`, leave the chip, add. | Chip starts on the reranker label (the name pattern's guess, matching `kind_of(inferred_tags(id), ())` in-process); six clicks return it to the start through six distinct labels; after the first add `jq '.providers.openrouter.modelOverlay["my-team/bge-reranker-custom"]'` names the embedding capability and the vector output; after the second `jq '.providers.openrouter.modelOverlay["my-text-model"]'` is null and the id is in `models`. | a text typed id writes an overlay; a stated kind writes none; the chip's start disagrees with the in-process guess; an absence check passing (the id was listed, so no typed row was exercised) | T P E V R | |
| T3.12 <- A18 | yes | OpenRouter popover: type `qwen`; read the footer; press "Add all (k)". | k equals the shown rows not in `configured`; one `model.add_models` frame whose `models` equal exactly those ids; `jq` shows them; the footer's "showing n" equals the visible row count and "added m" equals `.providers.openrouter.models \| length`. | more than one frame; an already-added id in the frame; footer numbers off | T P E V R | |
| T3.13 <- A19 | yes | OpenRouter popover: type `mistralai/mistral-large`, Enter. Add every shown row for `deepseek/`, then type `deepseek/` and Enter. Type `zzz-not-a-model`, Enter. | First Enter: one `model.add_model` frame for the first shown unadded row. Second: no frame (every shown row is added, nothing typed beyond the prefix that matches rows). Third: one `model.add_model` frame for the typed id. No `model.remove_model` frame anywhere in the case. | Enter removes; Enter adds when nothing is addable | T P E R | |
| T3.14 <- A20 | yes | Headers and display names: prior T3.10 and T3.11 verbatim, from the Advanced fold. | As prior. | as prior | T P E V R | |
| T3.15 <- A21 | yes | Disconnect: prior T3.7 verbatim, from the footer. | As prior; the footer's wording matches the shape (key / oauth / local). | as prior | T P E V R | |

### Roles and the picker

The picker's owning layer for "which models" is `options.json`:
`.providers[] .model_labels[<id>].kind` (absent means text). Every case below
computes its expected list from that file and the seed's `models`.

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T4.1 <- A22 | yes | Model settings: click the embedding slot's pill. | `eval`: exactly one `.mpick` exists and `document.querySelector('.mpick').closest('.smodal')` is null (body-level, the same element the composer opens); its provider column lists the three connected key providers (the EverOS rule) with counts 0 / 1 / 0; openrouter's model column lists `openai/text-embedding-3-small` and nothing else; deepseek's column shows the `gui.picker.empty_kind` text and the typed-id row. | a connected key provider missing from the column; a text model in the list; deepseek's column blank instead of the empty-kind text; a second picker element | T E V R | |
| T4.2 <- A23 | yes | The embedding slot (T4.1), then the rerank slot. Then the T1.2 seed variant. | Embedding: one row, the embedding model. Rerank: one row, `BAAI/bge-reranker-v2-m3`. `anthropic/claude-sonnet-4-5`, configured on the same provider, appears in neither. Variant: the embedding slot still opens the picker; openrouter's column shows the empty-kind text and the typed-id row, no model. | a text model in either slot; the variant shows an empty state instead of the picker, or a blank column | T E V R | * |
| T4.3 <- A24 | yes | The image slot, then the speech slot. | Image: provider column is openrouter only; rows are the models whose `kind` is `image` in `options.json` -- `google/gemini-2.5-flash-image`. Speech: openrouter is listed with count 0; its column shows the empty-kind text and the typed-id row. | another provider; a non-image row; speech showing an empty state, or a blank column | T E V R | |
| T4.4 <- A25 | yes | The chat slot; select openrouter in its column. | Provider column: deepseek, openrouter, gemini (each has a text model); openrouter's rows: `anthropic/claude-sonnet-4-5` only -- the embedding, reranker and image models are absent. Repeat for curator, session naming, gate, multimodal: same lists. | an embedding or image model listed for a text role | T E V R | |
| T4.5 <- A26 | yes | Chat slot: pick `gemini-2.5-flash`. Curator slot: pick `deepseek-v4-flash`. Embedding slot: pick the embedding model. Image slot: pick the image model. | `jq .agents.defaults` reads gemini's pair and `eval` on `#modelChip` reads the new model; `jq .context.curatorModel/.curatorProvider` reads deepseek's pair; the embedding pick produces a `settings.everosSet` frame with `borrow_from: openrouter` and, read back in-process afterwards, `RPC2 raven.rpc.methods.console settings_everos '{}'` returns the embedding section holding that model (the frame alone would look the same had the write silently failed); the image pick writes `tools.media.image` then removes `image_generate` from `tools.disabledTools` (prior T3.13's check). | any write missing or in the wrong order | T P E V R | |
| T4.6 <- A27 | yes | Chat slot: type `deepseek-v4-flash-vision-exp` (in DeepSeek's catalogue, not configured); read the chip; choose the row. | The typed row reads "use ... add to DeepSeek" with the chip on the text label (the slot's kind; here the name guess would also say text -- T4.6b is the slot whose kind differs from the guess, and T4.7 the composer's opening, where the guess is the start); frames in order: `model.add_model` then the chat write; `jq .providers.deepseek.models` gains the id and `agents.defaults.model` equals it. | the chat write before the add; the chip on another kind | T P E V R | |
| T4.6b <- A27 | yes | Embedding slot: type `my-embedder` (a name the patterns miss); read the chip; choose. | The chip starts on embedding (the slot's kind), not text (the guess); the `model.add_model` frame states the embedding capability; the model then appears in the embedding slot's list on reopen. | the chip on text; the add without stated tags; the model absent from the slot afterwards | T P E V R | |
| T4.7 <- A28 | yes | Close settings. Click the composer's `#modelChip`. Select openrouter. Type `gemini-2.5-flash-lite`, choose the typed row. | The same `.mpick` element (one in the document); provider column lists deepseek, openrouter, gemini; openrouter's rows exclude the embedding, reranker and image models; a seed variant where gemini's `models` is replaced by `[ "gemini-embedding-001" ]` (embedding by name) keeps gemini in the column with count 0 and the empty-kind text in its model column. The typed row reads "use ... add to Gemini" with the chip on text; choosing it produces `model.add_model` then the session switch frame, and `#modelChip` reads the new model. | an embedding model offered for chat; gemini missing from the column, or its column blank; no typed row | T P E V R | * |
| T4.7b <- A28 | yes | Seed variant: `agents.defaults.model = deepseek-v4-flash-vision-exp`, deepseek's `models` unchanged (the id is not in it -- the state onboarding and the CLI leave behind). Click `#modelChip`; select deepseek. | Deepseek's column lists `deepseek-v4-flash-vision-exp` first, marked as current, above its two configured models; the chip names it; `jq '.providers.deepseek.models \| length'` is still 2 (listing wrote nothing). | the current model in no column; nothing marked; a write | T E V R | |
| T4.8 <- A29 | yes | Resize the window to 1200x700. Open the chat slot's picker (top of the card). Then `RPC model_add_model '{"slug":"openrouter","model":"openai/gpt-audio"}'` -- the registry files that id under `audio`, so the speech slot, the last row of the card, now offers it -- reopen the dialog and open the speech slot's picker. Scroll `.spanels` by 200px. Resize to 1200x900 with a picker open. Then the composer chip's picker. | Boxes via `eval`: for the top slot the picker's top >= the pill's bottom; for the bottom slot the picker's bottom <= the pill's top (flipped) and its bottom <= viewport height - 12; after the scroll `.mpick` is gone; after the resize `.mpick` is still present and its top has moved with the pill; the composer's picker bottom <= the composer card's top. | a picker below a pill with no room below; a picker outliving the scroll; the composer picker below the card | T E V R | * |
| T4.9 <- A30 | yes | Open the chat slot's picker; click the roles card's heading. Reopen; click the veil at (4, 4). | First: `.mpick` gone and `#setVeil` still `data-open="true"`. Second: `.mpick` gone and `data-open="false"`. | the dialog closing on the inside click; the picker surviving the veil click | T E V R | |
| T4.10 <- A31 | yes | Chat slot picker (current `deepseek-v4-pro`): the protocol control; set it to Anthropic. | The control is drawn for the current model only; one `model.set_protocol` frame; `jq '.providers.deepseek.modelProtocols["deepseek-v4-pro"]'` reads `anthropic`. | no control; the frame or the write missing | T P E V R | |
| T4.11 <- A32 | yes | Serve `ui-web/dist` read-only (`python3 -m http.server` from that directory, prior T9.1's workaround) and open `index.html?stub=1`; open settings; both pages; open a slot's picker. | Both panels render (`dataset.section` flips), the provider column has the fixture rows, `.mpick` opens with fixture models; `playwright-cli console` shows zero errors. | any console error; a blank panel | T E V R | |

### Commands and gates

| T <- A | Host | Steps | Observe (owning layer) | Red when | Rings | * |
|---|---|---|---|---|---|---|
| T5.1 <- A33 | command | `uv run pytest tests/test_rpc_model.py tests/test_provider_registry_data.py -q`. Then the mutation: comment out the `"gateway":` line in `_build_provider_entry` and the `"kind":` line in `_model_labels`, run again, restore, `git diff` clean. | Green; then two named failures on the assertions for `gateway` and `kind`, not on setup. The id-to-kind table test passes on both sides (`kinds.test.ts` in T5.3). | the mutated run stays green; a failure that is not the assertion | R | |
| T5.2 <- A34 | command | `npm run gen:check` in `ui-web/`; `grep -n " as " ui-web/src/rpc/fixtures/model.ts`; `grep -c "gateway:" ui-web/src/rpc/fixtures/model.ts`. | Exit 0; empty; a count equal to the fixture's provider rows. | drift; an `as`; a row without `gateway` | R | |
| T5.3 <- A35 | command | `npm test` in `ui-web/`; `test ! -e ui-web/src/components/ModelPicker.tsx ui-web/src/components/ModelPicker.test.tsx`; `git diff BASE..HEAD -- ui-web/src/features/model/ModelPicker.test.tsx \| grep -c "^-.*it('"`. | Every gate green; both paths absent; the deleted-case count is 0. | a red gate; a file present; a deleted case | R | |
| T5.4 <- A36 | command | `make check-source-language`; the A36 Python one-liner. | Exit 0; `[]`. | any CJK outside a zone; a key with one locale | R | |
| T5.4b <- A36 | yes | Model providers and Model settings in `zh` (the seed's language); then General > language > English; both pages again. | No snapshot contains a string matching `gui\.` (a missing locale renders the raw key); the two nav labels and the filter menu's six entries equal the `zh` values in `i18n/messages.json` for their keys, then the `en` values. | a raw key on screen; a label in the other language | T E V R | |
| T5.5 <- A37 | yes | A fresh `$RV` with no provider; open the served page; follow the onboarding: DeepSeek, paste a key, pick a model. | `jq .providers.deepseek.apiKey` set and `jq .agents.defaults.model` set (prior T9.7's check); the main page opens. | the flow fails to list providers or to write | T P E V R | |
| T5.6 <- A38 | command | `grep -cE "^\*\*(Kind\|Offer\|Gateway provider\|Provider catalogue)\*\*:" ui-web/CONTEXT.md`. | `4`; each entry, read, names the code it comes from. | fewer than four; an entry with no code reference | R | |

A33, A34, A35 and A38 are gate items: their subject is the branch, not the
running page, so they have command cases only; their link to the host is
chapter 0's build step, which serves the very branch the gates ran on. A36
has both a command case and a host case.

## Constraint re-checks (not acceptance)

The five "How to check" commands of C3, C4, C6, C7 and C9 in the design,
run verbatim at G4. They verify constraints, not acceptance items, so they
are not cases; their output goes into the report beside the gate items.

## Contract-level cases (not acceptance)

One pytest per backend row of the design's "Backend changes" table; the
existing `ModelPicker.test.tsx` cases (none deleted, C11) plus new cases for
the offer, the kind filter and the typed row; `kinds.test.ts` for
`guessKind` and `modelKind`; `Roles.test.tsx` for the slot-to-kind table and
the `openPicker` call; `Providers.test.tsx` and `ProviderDetail.test.tsx` for
the column, the filter and the popover against fixture data; the
`fixture-shape` and `offline-coverage` gates. These are the fast loop during
implementation and the base for the mutation check T5.1.

## Starred cases: the manual pass

At most five. A person runs these after the agent's pass, on the same host
setup, and writes what they saw next to what the report recorded. T3.5
(OAuth with a real account) is person-run but not starred: its logic is
unchanged from the prior design and the prior run could not obtain an
account for it.

| * | Case | Why a person | Steps for the person |
|---|---|---|---|
| 1 | T2.1 + T3.8 | The page's whole shape: fifty-five rows with marks, the connected ones first, a detail pane, a popover that hangs off its button. Whether it reads as one page is a judgement. | Settings > Model providers. Scroll the left column: marks, dots, the default badge. Click OpenRouter, press "add model", watch the list arrive, click one model on and off. |
| 2 | T3.9 | The kind tabs are the visible face of the whole "kind" idea; counts that match the RPC are what the agent checks, whether the tabs make sense is what the person checks. | In the popover type `gemini`; read the tab counts; pick "image"; clear the search. |
| 3 | T4.2 | The one behaviour the owner asked for by name: the embedding slot lists embedding models only. | Model settings > embedding slot: only `text-embedding-3-small`; rerank slot: only the reranker. Chat slot: neither appears. |
| 4 | T4.7 | The composer chip is the most-used control on the page and it changed: every connected provider listed, fewer models per column, a typed row, the current model always findable. | Close settings. Click the model chip. Every connected provider is on the left; the model the chip names is marked on the right. Look for any embedding or image model (there must be none). Type an id not in the list, choose "use ...". |
| 5 | T4.8 | Placement is a feel: below when there is room, above when there is not, gone on scroll, following on resize. | Open a slot picker near the bottom of the card; scroll; resize the window. Then open the composer's picker: it hangs above the composer. |

## Red flags for whoever runs this

- A case whose E ring is a snapshot: the verdict must come from the file,
  the in-process reply or a DOM attribute, not from the drawing.
- A count in the report that was typed, not computed from `options.json` or
  `fetch_openrouter.json`.
- A case run against the operator's own gateway on 18792, or with the
  operator's `~/.raven` as `RAVEN_HOME`.
- A typed-id case whose id happens to be in the vendor's live list (then the
  row is a normal row, not the typed row); check the reply first.
- A skipped case marked green.
