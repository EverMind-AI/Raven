# Settings: provider and model pages split, model kinds, one picker - design

Status: draft for G1 (2026-09-20). Branch `feat/settings_model_section`,
target `refactor/ui_web_architecture`.

Companion documents: acceptance cases in
`docs/specs/2026-09-20-web-settings-model-section-acceptance.md`, plan in
`docs/plans/2026-09-20-web-settings-model-section.md` (both written after
G1). The deviations ledger is a working file of the task, not committed; its
entries are quoted here by number (D1, ...).

This change edits the model page that `2026-09-17-web-settings-page-design.md`
delivered. Where that document's constraints and acceptance items still hold
they are cited as "prior C14", "prior A12" and not restated.

## Terms used throughout

- **kind**: which bucket a model list files a model under, one of `text`,
  `image`, `audio`, `video`, `embedding`, `reranker` -- the backend's
  `KINDS` in `raven/providers/registry_data.py`, derived by `kind_of` from what
  a model can do and what it *writes*. A model that reads images is still
  `text`; a model nothing describes is `text`. Distinct from a **capability**
  (`function-call`, `image-recognition`, ..., drawn as one icon each) and from a
  provider's **auth shape** (`key` / `oauth` / `local` / `endpoint`).
- **gateway provider**: a provider that resells other vendors' models under
  `vendor/model` ids -- `ProviderSpec.is_gateway` in the registry, `gateway` on
  the wire row. Twenty-one of the fifty-five today, `custom` among them. Not the
  **Gateway** of `ui-web/CONTEXT.md`, which is the Runtime's WebSocket server;
  where this document says "gateway" alone about a provider it means this.
- **connected**: what `model.options` reports as `authenticated` on the wire;
  both front-end mappers expose it as `on`. One fact, two names; this document
  uses either.
- **offer**: what one opening of the picker lists -- a kind, optionally a
  subset of providers, optionally what a pick does. The composer opens with the
  default offer; a role slot opens with its own.
- **slot** / **role**: one of the eleven model roles of the roles card (chat,
  curator, session naming, skill gate, EverOS llm / embedding / rerank /
  multimodal, image, speech, video).
- **provider catalogue** (shortened to **catalogue** below): every provider
  `model.options` returns, connected or not. Not the Session Mode catalogue of
  the Runtime's `CONTEXT.md`.
- The four names new to `ui-web/CONTEXT.md` -- kind, offer, gateway provider,
  provider catalogue -- are defined there in this change (Decisions, A38).

## Goal

Rebuild the model section of the settings dialog to the revised prototype
(the settings document embedded in the claude.ai artifact listed under
Sources, as of 2026-09-20):

1. Split the "Model" page in two. **Model providers** answers "which accounts
   do I have": a two-column page, the whole catalogue on the left with a search
   box and a filter, the selected provider's connection on the right, every row
   carrying its vendor mark. **Model settings** answers "which model does each
   job run on": the roles card alone.
2. Give every model a kind. The add-model list filters by kind (one tab per kind
   present, with counts), a typed id carries a kind, and
   the kind decides where a model is offered: an embedding slot lists embedding
   models only, a text slot lists text models only, the composer's model chip
   lists text models only.
3. List the union of what the registry knows -- fifty-five providers on a
   stock install -- not the prototype's twenty-one, which are a snapshot of an
   older registry and a strict subset of ours.
4. First, merge the two model pickers into one. `components/ModelPicker.tsx`
   has exactly one caller, the roles card; `features/model/ModelPicker.tsx`
   serves the composer. The kind filter belongs in the picker the slots use,
   and building it twice, or in one of two, is the fork this order avoids.

Who this is for: the person configuring a Raven install from the browser
(every provider Raven supports is reachable from the page, a wrong-kind model
is never offered for a slot); the front-end engineer inheriting the code (one
picker, one catalogue page in the shape two later pages -- channels, memory --
will copy); the composer's owner (the same component, one new filter and one
new row).

Shapes rejected in the discussion round:

- Kinds without the merge. The filter would live in `components/ModelPicker`
  and the composer would keep offering embedding models for chat.
- Merging the other way, `features/model` becoming an adapter over the
  props-only component. It moves 254 lines of drawing (capability icons,
  connection state, protocol override, footer) into the smaller component and
  re-pins a 585-line test file for no behaviour gained.

## Non-goals

- The three navigation entries the prototype also adds -- channels, cron,
  memory. Its own comment says "entries only this round, the pages next
  round"; the owner declined them for this change.
- The prototype's OAuth pane, which tells the reader to run `raven provider
  login` in a terminal. The page keeps the browser device flow the prior
  design built (prior A13); only the pane's layout follows the prototype.
- Backend enforcement of kinds. `agents.defaults.model` and the other role
  keys accept any id they accept today; the kind is a filter the page applies,
  and a wrong-kind model written by hand or by an older client is not
  refused. Making the backend refuse it is a separate contract change.
- A second kind classifier. The bucket a model lands in is computed once, in
  `registry_data.kind_of`, and reaches the page over the wire (C6). The one
  client-side guess -- the kind chip on a typed id -- copies the registry's two
  name patterns, applied to the id's last path segment as `inferred_tags`
  does, and nothing more.
- Adding providers to the registry, filling fields on the ones that exist
  (`key_url`, `docs`, `homepage`), or a mark for `custom`. The catalogue is
  whatever the registry declares; a row the registry gives no link has no
  link, and `custom` draws the lettered tile every unmarked provider draws.
- A reusable two-column skeleton. The prototype shares one between providers,
  channels and memory; this change builds it for providers and names the
  class family so the later pages can lift it, without abstracting for them.
- Media roles beyond OpenRouter (prior C14 holds). The kind filter narrows
  OpenRouter's list to the slot's kind; it does not widen the provider set.
- Restyling anything outside the two pages and the picker; the composer chip's
  label; the sub-agent instance model chip (`features/subagents/InstanceModel.tsx`),
  which lists the agent's own menu and is not a picker over our catalogue.
- The onboarding wizard's model step, which shares `model.options` and
  `setupState` with this page. It is not redesigned; A37 checks it still
  completes.
- The TUI's provider and model screens (`ui-tui/`). Only its generated message
  catalogue moves, as a by-product of the i18n keys.
- Re-fetching kinds for models already configured. A configured model's kind
  comes from the registry row, the person's overlay, or its name, at read
  time.

## Constraints

Each constraint says how it is checked. `BASE` below is
`$(git merge-base HEAD refactor/ui_web_architecture)`.

### Architecture

| # | Constraint | How to check |
|---|---|---|
| C1 | The prior design's architecture constraints hold unchanged: domain skeleton, RPC only in `source.ts`, cross-domain reach through another domain's `source.ts` or `types.ts` only, `settings-` prefixed classes and ids, `t(key)` everywhere, contract-first RPC edits, fixtures typed. | The gates that now enforce them: `import-direction`, `domain-shape`, `store-shape`, `check-class-namespace`, `css-one-owner`, `i18n-keys`, `fixture-shape`, `offline-coverage`, `rpc-names` (`ui-web/scripts/gates/`), all green in `npm test`; `npm run gen:check`; `tests/test_rpc_schema_match.py`. |
| C2 | Settings reaches the picker through `features/model/source.ts`, which exports the opener; it does not import `features/model/store.ts` or the component. | `grep -rn "model/store\|model/ModelPicker" ui-web/src/features/settings` prints nothing; `import-direction` gate. |
| C3 | One picker. After the change `ui-web/src/components/ModelPicker.tsx`, `ModelPicker.test.tsx`, the `.model-picker` and `model-picker-*` CSS rules, and `lib/popover.ts`'s exported `anchorRow` with the two private helpers only it used (`bounds`, `DIALOG`) do not exist. | `test ! -e ui-web/src/components/ModelPicker.tsx`; `grep -rn "components/ModelPicker\|anchorRow\|model-picker" ui-web/src` prints nothing; `css-one-owner` gate. |
| C4 | Not modified: `App.tsx`, `state/`, `chrome/`, `app/`, `features/model/chip.ts`, `main.tsx`, the gate scripts, `raven/providers/`. One exemption: `ui-web/scripts/check-class-namespace.mjs` changes only the two pin numbers for `settings` and `model` (C22). The composer chip keeps opening the picker with the same call. | `git diff BASE..HEAD --stat -- ui-web/src/App.tsx ui-web/src/state ui-web/src/chrome ui-web/src/app ui-web/src/features/model/chip.ts ui-web/src/main.tsx ui-web/scripts/gates raven/providers` is empty; `git diff BASE..HEAD -- ui-web/scripts/check-class-namespace.mjs` has changed lines of the form `  settings: N,` / `  model: N,` only. |
| C5 | Contract first: the two new wire fields are added to `rpc-schema/openrpc.json`, then `npm run gen`, then the pydantic models, then `source.ts`. Every fixture row that carries the parent object carries them. | `npm run gen:check`; `tests/test_rpc_schema_match.py`; `fixture-shape` gate. |
| C6 | One classifier. The page reads a model's kind from `model_labels[m].kind` (`model.options`) or `kind` (`model.fetch_models`); a model with no label entry is `text`. No TypeScript derives a kind from capabilities or modalities. The only name-based guess is `guessKind(id)` for a typed id, a copy of `_RERANK_NAME` / `_EMBEDDING_NAME`. | `grep -rnE "capabilities|_modalities|image-generation" ui-web/src/features/model ui-web/src/features/settings --include=*.ts --include=*.tsx` matches only three things: the two wire-to-row mappers passing `model_labels` through, the add-model popover writing a typed id's stated tags (A17), and tests -- no line reads a capability or a modality to decide a kind; `kinds.test.ts` and `tests/test_provider_registry_data.py` assert the same id-to-kind table (A33). |
| C7 | Backend writes stay in the writers that exist. The two new fields are read-only facts on `model.options`; no new method, no new config key. | `git diff BASE..HEAD --stat -- raven rpc-schema tests` lists `raven/rpc/models.py`, `raven/rpc/methods/model.py`, `rpc-schema/openrpc.json` and files under `tests/`, nothing else. |

### Design constraints

| # | Constraint | How to check |
|---|---|---|
| C8 | A slot offers its kind and nothing else, per the table in "Frontend": text slots list `text`, the embedding slot `embedding`, rerank `reranker`, image `image`, speech `audio`, video `video`; the composer offers `text`. Every connected provider the role allows is listed; one with no model of the kind shows an empty column that says so and the typed-id row. The model a slot (or the conversation) currently holds is listed in its provider's column even when that provider's list does not carry it. | A22-A25, A28 |
| C9 | The catalogue is every row `model.options` returns, `hosted_vllm` and `custom` included; the composer still lists connected providers only, which is what hid the two generic rows in practice. Confirmed by the owner 2026-09-20: people running their own servers must not be shut out. | A3; `grep -rn HIDDEN_PROVIDERS ui-web/src` prints nothing. |
| C10 | Secrets never round-trip (prior C10). The key pane sends a key only when the person typed one; the eye toggle reveals typed text, never a stored key. | A8 |
| C11 | The composer's picker keeps every behaviour its test file pins except the two this change adds (a kind filter, a typed-id row). | `ModelPicker.test.tsx`: no case deleted; each changed expectation named in the commit body; the diff of `__snapshots__/ModelPicker.test.tsx.snap` is read line by line and every changed line belongs to the kind filter, the typed row or the title -- a regenerated snapshot carrying any other change fails this. |
| C12 | A model a role uses cannot be removed from its provider (prior A15). The add-model popover's per-row toggle honours it. | A13, A14 |

### Assumptions

| # | Assumption | Status |
|---|---|---|
| C13 | `model.options` already lists the whole registry, connected or not. | Verified 2026-09-20: `_entries_off_loop` iterates `list_providers()`, whose `_listable_provider_names` returns every declared provider plus config extras; `features/settings/providers/Providers.tsx` filters to `on` client-side. |
| C14 | The backend already classifies. `registry_data.kind_of`, `inferred_tags`, `KINDS` exist; `model.fetch_models` returns `kind` per row; `model.add_model` accepts `capabilities` / `input_modalities` / `output_modalities` and writes them as a stated overlay. | Verified 2026-09-20: `registry_data.py:257-300`; `rpc/methods/model.py:728`; `rpc/models.py:1681` `ModelAddModelParams`; `_stated_overlay`. |
| C15 | A typed id nothing describes reads back with its name-inferred tags, so a model added as `bge-m3` with no stated kind is `embedding` on the next `model.options`. | Verified 2026-09-20: `providers/catalog.py:137,155` pass `inferred_tags(ref)` into `describe`. |
| C16 | The picker floats over the settings dialog: `--z-picker` (46) is above `--z-veil` (40). | Verified 2026-09-20: `ui-web/src/styles/page.css:144,147`. |
| C17 | Twenty-one registry specs carry `is_gateway=True`, five `is_local=True`; `custom` is a gateway. | Verified 2026-09-20 by reading `PROVIDERS` in `raven/providers/registry.py`. |
| C18 | The only consumer of the shared `SectionId` is the settings domain; nothing else deep-links to the `model` tab. | Verified 2026-09-20: `grep -rn SectionId ui-web/src` lists `SettingsApp.tsx` and `settings/store.ts`; `state/settings.ts` defaults `settingsTab.id` to `usage`; `onboard/store.ts:128`'s `'model'` is an onboarding step id. |
| C19 | Removing `HIDDEN_PROVIDERS` changes nothing for the composer beyond letting a connected `custom` or `hosted_vllm` section be picked: the composer already lists connected providers only. | Verified 2026-09-20: `features/model/store.ts` `authed()` filters `p.on && offered(p).length`; the set has two readers, `features/model/source.ts:85` and `source.test.ts` (:117, :325, :334 -- the last asserts the two names and goes with the set). Onboarding reads `setupState`, not the list. |
| C20 | A click on the settings scrim closes the dialog and, with it, an open picker -- the same as today, where the picker is inside the dialog. No handling is added. | Verified 2026-09-20: `App.tsx` `useScrim` listens to `click` on the veil; the picker closes on a capture-phase `pointerdown` outside itself first. |
| C21 | Adding a navigation entry does not move the boot DOM golden `build.py` compares: the nav is a portal into `#snavList` filled at runtime. | **Unverified.** Checked at the first front-end commit; a change is explained in the commit body, not silenced (prior open question 5). |
| C22 | The `check-class-namespace` counts for `settings` and `model` shift by the classes this change adds and deletes; the pin in `ui-web/scripts/check-class-namespace.mjs` is updated with the diff (the C4 exemption) and the new numbers are explained in the commit body. | **Unverified** until the classes exist. |

## Acceptance

A1 to A32 and A37 are checked by an agent on a real host: a real
`raven serve`, a real browser, real config on disk. A33 to A36 and A38 are
checked by the commands they name. Items marked with a star are candidates for the manual pass and
are also run by the agent; the final star table is in the acceptance-cases
document. Numbering is stable once referenced; a withdrawn item keeps its
number.

### Navigation

- A1. The sidebar lists "Model providers" then "Model settings" between Usage and Skills; opening the dialog with `settingsTab.id = 'provider'` shows the catalogue, with `'model'` the roles card. Fails if the `model` tab still shows a provider list.
- A2. Model settings holds the roles card and its chat-parameters fold and nothing else. A slot none of whose allowed providers is connected shows an empty state naming Model providers (for a media slot, the existing "connect OpenRouter" shortcut), and clicking it switches to that page; a slot with a connected provider always opens the picker, whatever that provider has added.

### Model providers, left column

- A3. The list has one row per provider `model.options` returns -- fifty-five on a stock registry, `hosted_vllm` and `custom` among them -- connected rows first with a status dot, the rest in registry order; each row draws its mark (fifty-four) or a lettered tile (`custom`); the provider serving the chat role carries a "default" badge. Fails if the row count differs from the RPC's or any registry provider is missing. (*)
- A4. Typing in the search box narrows rows by display name or id as you type; no match shows `gui.model.prov_no_match`; clearing restores every row.
- A5. The filter menu offers all / connected / direct / gateway / browser auth / local. Connected lists `authenticated` rows; gateway lists exactly the rows with `gateway: true`; browser auth the `oauth` shape; local the `local` shape; direct the `key` and `endpoint` shapes that are not gateways. Fails if any filter shows a row outside its definition.
- A6. Clicking a row shows that provider on the right without leaving the page; on open the right pane shows the provider with the default badge; when no chat model is set, the right pane shows one line asking the reader to choose a provider on the left and the left column is unchanged.

### Model providers, right column

- A7. The head shows the mark, the name, a link out when the registry carries a homepage or key page, and one status line: connected / needs an API key / needs authorisation / needs an address, by shape and connection.
- A8. Key pane: pasting a key and "connect" (or "update" when connected) writes it and the status turns connected; the eye toggle shows and hides the typed text; the stored key is never shown and never sent back; the environment-variable hint names `key_env` when there is one. (prior A10)
- A9. Address: gateways and `needs_api_base` providers show the address field in the main body; every other key provider shows it under Advanced as "override"; a reset button appears when a default exists and the stored value differs, and restores it. One component draws it in both places. (prior A16)
- A10. Azure keeps deployment name and API version (prior A12) in the body under the address.
- A11. An OAuth provider shows the browser device flow of prior A13 in the prototype's pane layout; disconnect reads "disconnect authorisation". (*)
- A12. A local provider shows the address field and connect / update and no key field (prior A11).
- A13. The model list shows each configured model as a tag with a remove control; removing one a role uses is refused with the role's name and nothing is written (prior A15).
- A14. "Add model" opens a popover under the button that fetches the vendor's list (`model.fetch_models`); each row shows the model's name or id, its capability icons and context window; clicking a row adds it in one write and the tag appears, clicking it again removes it (subject to A13); a vendor without a list says so and takes a typed id. (*)
- A15. Kind tabs across the popover -- "all N", then one per kind present with its count -- are counted over the current search result; a kind with no rows has no tab, a list with one kind has no tab row; choosing a tab narrows the rows to that kind. Fails if any count disagrees with the `kind` values the RPC returned. (*)
- A16. For a gateway the rows group by vendor prefix with a fold and an "add group" control that adds the group's unadded rows in one write and is disabled when none remain; rows whose ids carry no prefix form one unlabelled group after the labelled ones; a provider with no prefixed id at all shows one flat list.
- A17. A query matching no row shows "add `<id>` by id" with a kind chip; the chip starts on the kind the registry's name patterns imply (`guessKind`) and cycles through the six kinds on click; adding writes the model and, for a non-text kind, the matching `capabilities` and `output_modalities`; for text nothing is stated and the backend's own inference stands (C15).
- A18. "Add all (k)" adds the k rows currently shown and not yet added in one `model.add_models` write; the footer reads "showing n · added m", n counting shown rows and m the provider's configured models.
- A19. Enter in the popover's search adds the first shown row that is not yet added; when every shown row is added, it adds the typed id if there is one and otherwise does nothing. Enter never removes.
- A20. Advanced keeps headers and display names as prior A17 and A18.
- A21. The footer disconnects a connected provider as prior A14, worded by shape.

### Roles and the picker

- A22. A role pill opens the shared picker -- the same `.mpick` element the composer opens -- under the pill; its left column lists every connected provider the role may use (prior A21 / A22 rules) with the count of its models of the slot's kind, and its right column lists only that kind; a provider with none shows the empty-kind text and the typed-id row. Fails if an allowed connected provider is missing or a model of another kind appears.
- A23. The embedding slot lists embedding models only and the rerank slot reranker models only: a chat model configured on the same provider does not appear in either. (*)
- A24. Image, speech and video slots offer OpenRouter only (prior A22) and, within it, `image`, `audio` and `video` models respectively.
- A25. Chat, curator, session naming, EverOS llm, gate and multimodal list `text` models only; an embedding model configured on a connected provider does not appear.
- A26. Picking writes exactly what prior A19-A22 describe, through the roles card's own write path: chat updates `agents.defaults` and the composer chip repaints; curator, skill gate and session naming write model then provider; EverOS roles call `settings.everosSet`; media roles write `tools.media.<kind>` then the tool switch.
- A27. A typed id in a slot's picker shows "use `<id>` · add to `<provider>`" with a kind chip that starts on the slot's kind, not on the name guess; choosing it adds the model to the provider first, with that kind's stated tags as A17 describes, then assigns it.
- A28. The composer's chip opens the same picker with the default offer: every connected provider, `text` models only (a provider with no text model shows the empty-kind text), the conversation's current model listed and marked in its provider's column even when that provider's list does not carry it, and the same typed-id row, which adds the model then switches this conversation to it. (*)
- A29. Opened from a settings row the picker hangs below the row; it flips above only when its bottom edge would pass the viewport's bottom minus 12px and it fits above; it stays inside the viewport and over the dialog; scrolling the settings panel closes it; a window resize re-places it. Opened from the composer chip it keeps hanging above the composer card.
- A30. A click inside the dialog but outside the picker closes the picker and leaves the dialog open; a click on the scrim closes both (C20).
- A31. The protocol override control the composer's picker shows for the current model appears in a slot's picker for the slot's current model and writes through `model.set_protocol` as today.
- A32. Offline: `dist/index.html?stub=1` renders both pages and opens a slot's picker from fixture data, with no console error.

### Commands

- A33. `uv run pytest tests/test_rpc_model.py tests/test_provider_registry_data.py -q`: `model.options` rows carry `gateway` (`openrouter` true, `anthropic` false, `custom` true) and each `model_labels[m]` carries `kind` equal to `kind_of(...)`; the id-to-kind table `kinds.test.ts` asserts is asserted here through `kind_of(inferred_tags(id), ())`.
- A34. `npm run gen:check` is clean and `ui-web/src/rpc/fixtures/model.ts` rows carry `gateway` and label `kind`, typed, no `as`.
- A35. `npm test`: every gate in C1 green; no case of `features/model/ModelPicker.test.tsx` deleted; `components/ModelPicker.test.tsx` gone.
- A36. `make check-source-language`; `python3 -c "import json,sys;u=json.load(open('i18n/messages.json'))['ui'];b=[k for k,v in u.items() if k.startswith(('gui.settings.','gui.model.','gui.picker.')) and not (v.get('en') and v.get('zh'))];print(b);sys.exit(1 if b else 0)"` prints `[]` and exits 0.
- A37. Onboarding still works: on a fresh home the first-run flow lists providers, connects one and picks a model against the changed `model.options` (prior A55).
- A38. `grep -cE "^\*\*(Kind|Offer|Gateway provider|Provider catalogue)\*\*:" ui-web/CONTEXT.md` prints `4`, and each of the four entries names the code it is read from.

## Design

### Reading order

1. Backend: two fields on `model.options`.
2. The picker: the offer, the kind filter, the typed-id row, the second placement.
3. The roles card: from its own picker to the shared one.
4. The catalogue page and the popover.
5. Fixtures, gates, i18n.

### Backend changes

Two read-only facts join `model.options`; no method is added.

| Where | Now | After |
|---|---|---|
| `raven/rpc/models.py` `ModelOptionProvider` | no gateway fact | `gateway: bool = False` |
| `raven/rpc/models.py` `ModelLabel` | `label`, `description`, `capabilities`, `input_modalities`, `output_modalities`, `context_window` | plus `kind: Literal["text", "image", "audio", "video", "embedding", "reranker"]` |
| `raven/rpc/methods/model.py` `_build_provider_entry` | -- | `"gateway": bool(spec and spec.is_gateway)` |
| `raven/rpc/methods/model.py` `_model_labels` | emits an entry when described, tagged or windowed | every emitted entry carries `"kind": kind_of(row.capabilities, row.output_modalities)` |
| `rpc-schema/openrpc.json` | -- | the two properties, edited first (C5) |
| `tests/test_rpc_model.py` | pins `auth_type` | plus `gateway` and `kind` (A33) |
| `tests/test_provider_registry_data.py` | -- | the shared id-to-kind table (A33) |

A model with no label entry -- nothing describes it, nothing tags it, no
window is known -- has no `kind` on the wire and is `text`, which is what
`kind_of((), ())` returns. `model.fetch_models` already returns `kind` per row
and is unchanged.

### Frontend: the picker

`features/model/` is the picker's home; the change is one new input and what
it implies.

**The offer** (`features/model/types.ts`):

```ts
export type Kind = 'text' | 'image' | 'audio' | 'video' | 'embedding' | 'reranker'

export interface Offer {
  /* The kind this opening lists. The composer's default is 'text'. */
  kind: Kind
  /* Provider ids this opening may list; absent means every connected one. */
  providers?: string[]
  /* The slot's name, drawn beside the search box; absent for the composer. */
  title?: string
  /* The model the slot holds today and whose provider serves it: marked, given
     the protocol control, and listed in that provider's column even when the
     provider's list does not carry it. Absent means the conversation's current
     model, whose provider the wire names (`is_current`). Fills `at.marked`. */
  current?: { model: string; provider: string }
  /* What a pick does. Absent means store.choose (switch this conversation).
     `typed` is true for an id the provider does not list yet; the caller
     adds it first. */
  pick?(model: string, provider: string, typed: boolean): Promise<void>
}
```

**The store** (`features/model/store.ts`): `OpenAt` gains `offer: Offer`;
`open(anchor?, after?, marked?, offer?)` defaults the offer to `{ kind: 'text' }`;
`authed()` becomes `listed()`, which applies the offer -- connected and in
`offer.providers` when given; the kind narrows each provider's column, never
the provider list, and the current model (the offer's, else the
conversation's, whose provider row the wire marks `is_current`) is put at the
top of its provider's column when the list does not carry it. A provider with
nothing added at all offers the registry's own shortlist (`p.models`), for a
text opening only: the first-run wizard connects a vendor and picks a chat
model in one step, before anyone has built a list, and an empty column there
is the whole of that step -- while an embedding slot asking for the vendor's
entire catalogue would be a worse answer than "nothing here yet"; `choose()` calls `offer.pick` and returns before any of today's
logic when a pick is given, and without one does what it does today, the
`scope` derivation from the anchor included. That derivation is reached by no
caller once every anchored opening carries a pick; open question 5 asks
whether it goes now.

**Kind of a model** (`features/model/kinds.ts`, new):

```ts
export const modelKind = (facts: ModelTagFacts | undefined): Kind => (facts?.kind ?? 'text')
export const KINDS: Array<[Kind, string /* i18n key */, string /* glyph name */]>
export function guessKind(id: string): Kind   // last path segment, then the registry's two patterns, rerank first
```

`ModelTagFacts` gains `kind?: Kind` from the wire. `guessKind` copies
`inferred_tags`: the last path segment, `_RERANK_NAME` first, then
`_EMBEDDING_NAME`; both sides assert the same table (A33). The helper is
`modelKind`, not `kindOf`: `providers/Providers.tsx` already has a `kindOf`
that answers a provider's auth shape.

**The opener for other domains** (`features/model/source.ts`):
`export function openPicker(anchor: HTMLElement, offer: Offer, after?: () => void): void`
-- a one-line call into the store, so the settings domain never imports the
store (C2).

**The component** (`features/model/ModelPicker.tsx`):

- Reads `at.offer`; `providers = store.listed()`; `narrow()` filters each
  provider's column (kind first, then the search term). A column left empty by
  the kind shows the new `gui.picker.empty_kind` text ("No {kind} model added
  here yet" / "这一家还没有添加{kind}模型") above the typed-id row.
- A typed-id row under the list when the term matches no row of the selected
  provider: "use `<id>` · add to `<provider>`", with the kind chip of A17,
  starting on `offer.kind` when the opening has a title (a slot) and on
  `guessKind(id)` otherwise (the composer). Choosing it calls `pick(id, provider, true)` -- or, with no `pick`, adds
  through the model source then `choose`s.
- Title: `offer.title` in the search row when present.
- Placement: today's layout effect hangs the popover above `clearance(host)`
  and falls below when there is no room. With `offer.title` present the order
  inverts: below the anchor first, above only when the bottom edge would pass
  the viewport's bottom minus 12px and the popover fits above, clamped to the
  viewport -- what the prototype's `placePopover` does and what #541
  settled for the row case. The measured-origin trick of the deleted
  `anchorRow` is not needed: the popover is a body-level fixed element with no
  transformed ancestor.
- Scroll and resize, only when opened with a title: a `scroll` listener in
  capture on the document closes the popover when the anchor's `top` moved
  (the #541 rule, with its baseline refreshed on resize); `resize` re-places.
  The composer's opening registers neither, as today.

**Deleted**: `components/ModelPicker.tsx`, its test, the `.model-picker` and
`model-picker-*` rules in `styles/page.css` (2317-2341 today) and the nine
`.settings-ctl .model-picker*` / `.settings-ppcard .model-picker*` rules in
`features/settings/styles.css` (257-265; nothing renders `.settings-ppcard`
any more, so it goes too); the exported `anchorRow` and its two private
helpers `bounds` and `DIALOG` in `lib/popover.ts` (its header comment loses
the paragraph about them).

### Frontend: the roles card

`features/settings/providers/Roles.tsx`:

| Role | Kind offered |
|---|---|
| chat, curator, title, memllm, gate, multimodal | `text` |
| embedding | `embedding` |
| rerank | `reranker` |
| image | `image` |
| speech | `audio` |
| video | `video` |

`RolePill` calls `openPicker(pill, { kind, providers: roleProviders(role).map(id), title: roleName(role), current: val ?? (inherit ? chat : undefined), pick: (m, p, typed) => setRole(role, m, p, typed) })`.
`setRole` is unchanged; it already adds a typed id first. The store's
`picker: string | null` state and the `pickerProviders` mapping go; the
`ModelPicker` import goes.

The empty state per slot (A2): when `roleProviders(role)` is empty -- no
allowed provider connected -- the pill's place shows the existing "no
provider" text with a button that sets the tab to `provider` (for media roles
the existing "connect OpenRouter" shortcut, which also selects it); with any
allowed provider connected the pill always opens the picker.

### Frontend: the catalogue page

`features/settings/`:

| File | Now | After |
|---|---|---|
| `store.ts` | `SectionId` of eight; state `provider`, `provAdd`, `sheet`, `picker` | `'provider'` added before `'model'`; state `provQ: string`, `provFilt: 'all' \| 'on' \| 'direct' \| 'gateway' \| 'oauth' \| 'local'`; `picker` removed (the model store owns the open picker); `provAdd` stays for the wizard's `AddBlock` (D1); `sheet` becomes `addPop: { slug, q, state, items, kind, folded } \| null` |
| `SettingsApp.tsx` | `ICON`, `NAV`, `PAGE` for eight | nine; `provider` uses the prototype's `model` glyph and `model` the `assign` glyph |
| `pages/Provider.tsx` (new) | -- | `<div class="settings-tp">` two columns: `<ProviderSide />` and `<ProviderDetail slug />` or the pick-one prompt |
| `pages/Model.tsx` | providers card + roles | `<Roles />` |
| `providers/Providers.tsx` | connected list + inline `AddBlock` with a vendor `<select>`; also the onboarding wizard's model step (`SetupBodies.tsx` renders `<Providers setup />`) | untouched: the wizard keeps rendering it (D1). The settings page stops rendering it; `kindOf` / `takesKey` / `needsKey` / `takesBase` are imported from it as today |
| `providers/ProviderSide.tsx` (new) | -- | the catalogue column: search box, filter menu, rows (mark, name, default badge, dot; each row element carries `data-id={p.id}` so a driver can read the listed set without parsing names), connected first then registry order |
| `providers/ProviderDetail.tsx` | pane with key / address / models card with a checkbox `Sheet` / advanced | head per prototype (mark, name, link, status line); key pane with eye toggle and "get a key"; address in body or Advanced by A9; `Sheet` replaced by `AddModelPop` (A14-A19); disconnect footer |
| `providers/AddModelPop.tsx` (new) | -- | the add-model popover (A14-A19), lifted out of `ProviderDetail.tsx` where the `Sheet` was |
| `providers/Roles.tsx` | own picker | shared picker (above) |
| `source.ts` | maps `model.options` rows | plus `gateway`; label `kind` passes through |
| `types.ts` | `ProviderRow` | plus `gateway: boolean` |
| `styles.css` | -- | `.settings-tp*` (two columns, side, row, find, filter, main, section, tile, dot, badge) and `.settings-ap*` (popover, kind row, row, group head, footer), copied from the prototype with the prefix |

Filter semantics (A5), on `p.kind`, the wire's `auth_type` -- not
`Providers.tsx`'s `kindOf(p)`, which folds `endpoint` into `local` for the
pane's shape: `on` = `p.on`; `gateway` = `p.gateway`; `oauth` = `p.kind ===
'oauth'`; `local` = `p.kind === 'local'`; `direct` = `!p.gateway && (p.kind ===
'key' || p.kind === 'endpoint')`.

**The add-model popover** (`providers/AddModelPop.tsx`): opens under the "add
model" button inside the dialog (it is the dialog's own element, positioned
like the prototype's `placeAddPop`: below the button, flipping above only when
fewer than 220px remain below and more remain above, max-height from the
room). Fetches
`model.fetch_models` on open; rows as A14 with `ModelTags`; kind tabs as A15
counted over the search result; groups as A16 for gateways; typed row as A17;
"add all" as A18. A row click writes immediately: `model.add_model` or
`model.remove_model` (with the A13 refusal), one write per click; "add all"
and "add group" use `model.add_models`.

### Page contracts

| Surface | Reads | Writes |
|---|---|---|
| Catalogue rows | `model.options` rows; `agents.defaults.provider` for the badge | -- |
| Detail: key / address / Azure / disconnect / headers / display names | as prior design | as prior design (`model.save_key`, `model.set_fields`, `model.disconnect`, `model.add_model` for overlays) |
| Add-model popover | `model.fetch_models` (`kind` per row); configured list | `model.add_model` (with stated tags for a non-text typed id), `model.remove_model`, `model.add_models` |
| Roles card | snapshot as today; `model_labels[m].kind` through the picker | as prior design, through `setRole` / `clearRole` |
| Picker (composer opening) | `model.options` rows and labels | `config.set` model (as today); `model.add_model` then the switch for a typed id |

### Change map

| Area | Files | Nature |
|---|---|---|
| Backend fields | `raven/rpc/models.py`, `raven/rpc/methods/model.py`, `rpc-schema/openrpc.json`, `tests/test_rpc_model.py`, `tests/test_provider_registry_data.py` | add two fields, pin them |
| Picker | `features/model/{types,store,source,kinds,ModelPicker}.ts(x)`, `ModelPicker.test.tsx`, `kinds.test.ts` (new), `source.test.ts` (the `HIDDEN_PROVIDERS` assertions go), `styles/page.css` (`.mpick` block gains `model-kind-row`, `model-kind`, `model-typed`; loses `.model-picker` and `model-picker-*`) | one input, two additions, one deletion |
| Deleted | `components/ModelPicker.tsx`, `components/ModelPicker.test.tsx`, `lib/popover.ts` (one export and its two private helpers), `features/settings/styles.css` (nine rules, 257-265) | delete |
| Roles | `features/settings/providers/Roles.tsx`, `Roles.test.tsx` | swap the picker call |
| Catalogue | `features/settings/{store,SettingsApp,source,types}.ts(x)`, `pages/Provider.tsx` (new), `pages/Model.tsx`, `providers/ProviderSide.tsx` (new), `providers/ProviderDetail.tsx`, `providers/AddModelPop.tsx` (new), their tests, `styles.css`; `providers/Providers.tsx` untouched (the wizard's, D1) | the page |
| Data | `rpc/fixtures/model.ts`, `test/settingsHarness.ts`, `rpc/generated.ts` (generated), `i18n/messages.json`, `ui-tui/src/i18n/messages.generated.ts` (generated), `ui-web/CONTEXT.md` (four terms) | fields, keys, terms |
| Gates | `ui-web/scripts/check-class-namespace.mjs` pin (C22, the C4 exemption) | pins |

Sibling sites enumerated for the shared changes: `SectionId` (C18, two
files); `Provider` / `ProviderRow` mappers (two `source.ts`); every reader of
`HIDDEN_PROVIDERS` (two, C19); every caller of `store.open` (the chip, unchanged;
the tests); every place that draws a model row (`ModelPicker.tsx`,
`AddModelPop`, `InstanceModel.tsx` -- the last is out of scope, C-list in
Non-goals).

## Cost

- Who pays. Composer users see two changes: embedding and rerank models
  leave the chip's picker, and a typed id becomes possible there. Settings
  users learn two entries where there was one. The backend gains two fields
  and no method. The front-end takeover inherits one picker and one page
  skeleton fewer.
- Reversibility. The split is a data change in `SECTIONS`; the merge deletes
  a component that `git revert` restores; the two wire fields are additive
  and optional on the reader's side.
- The minimal half. The split and the catalogue page can ship without kinds
  (the popover would show a flat list); kinds cannot ship without the merge
  without forking the filter, which is the order the plan will take: fields,
  merge, kinds, page.
- Not free. The kind filter hides a wrongly classified model from every slot
  it should be in; the escape is the popover's kind chip (a stated overlay
  wins over inference) and, for a model the registry describes wrongly, a
  registry refresh. Named in Open questions.

## Decisions

- **Two pages, `provider` before `model`, the prototype's order.** The
  prototype's own comment states the split: accounts versus assignments. The
  section id is `provider`, the roles page keeps `model` so `settingsTab.id`
  values written elsewhere still land on a model page.
- **The catalogue lists every row `model.options` returns, `hosted_vllm` and
  `custom` included; `HIDDEN_PROVIDERS` is deleted.** The set was hiding an
  unconnected generic row from a list that had no filter; the catalogue has
  one, and `custom` is the page's only entry for a vendor Raven carries no spec
  for. The composer is unaffected beyond a connected `custom` becoming
  pickable (C19). The owner's reason: a person running their own server is a
  likely user and must not be shut out. Rejected: keeping the set.
- **Gateway comes from the registry, on the wire, as `gateway: bool`.** The
  filter needs a fact the page cannot derive; `ProviderSpec.is_gateway` has it.
  Rejected: a front-end list of gateway ids.
- **Kind is computed once, in Python, and travels as `model_labels[m].kind`.**
  `kind_of` exists, `fetch_models` already emits it; a TypeScript copy of the
  bucket logic would be a second source (Non-goals). A model with no label is
  `text`, which is also `kind_of`'s answer for nothing. Rejected: deriving
  kind from capabilities in TypeScript.
- **The typed-id kind chip guesses from the name with a copy of the two
  registry patterns, pinned by a shared table.** The alternative was a new
  `model.kind_of` RPC for one chip's default; the patterns are two regular
  expressions and the drift guard is one test on each side (A33). A text
  guess states nothing on add, so the backend's own inference (C15) is what
  is stored. Rejected: an RPC per keystroke; always writing a stated kind.
- **Kinds filter the slots, hard, and the composer with them.** A kind that
  only decorates is a tag; the owner asked for a distinction, and the
  prototype's comment names the slot filter as the one consumer. The
  prototype's own `drawPicker` does not filter -- the comment and the
  behaviour disagree, and this design follows the comment. Rejected: tabs in
  the popover only; filtering slots but not the composer.
- **Text slots take `text`; vision does not make an image model.** `kind_of`
  files a model by what it writes, so the multimodal slot lists `text` models
  like the others; capability data is incomplete ("absent means unknown, not
  cannot", `ModelTags.tsx`) and filtering by it would hide working models.
  Rejected: a capability filter per slot.
- **A provider with nothing added at all offers the registry's shortlist, and
  only for text.** The picker this one replaced carried that fallback for the
  first-run wizard, whose model step is the one place a model is picked before
  any list exists; dropping it in the merge left that step with an empty
  column, which is the whole step. Restricted to text because the wizard only
  ever asks for a chat model, and because a kind-filtered slice of a vendor's
  whole catalogue is a worse answer than saying the list is empty. Rejected:
  the fallback for every kind; no fallback (a non-goal crossed -- the wizard is
  not redesigned here, so it must keep working).
- **Every connected provider the offer allows is listed; a column with no
  model of the kind says so and offers the typed-id row; the current model is
  always in its provider's column.** The prototype's `drawPicker` lists every
  connected provider and writes "no model added here yet" in an empty column.
  A tester's report (2026-09-20) showed why the alternative fails: a provider
  whose key works but whose list nobody built vanished from the composer's
  picker, and the model the chip named -- set by onboarding, never "added" --
  was in no column to be marked; `agents.defaults.model` is routinely set
  without a matching entry in `providers.<slug>.models`. The owner reversed
  the earlier ruling to hide such providers. Rejected: hiding providers with
  none of the kind; a special case for the current provider alone.
- **The typed-id row is in both openings.** One control, one behaviour; the
  prototype's reason for the popover -- "fetching and typing are two routes to
  one thing, not two buttons" -- applies to the composer too. Rejected: a row
  only under slots.
- **`features/model` survives; `components/ModelPicker` goes.** One new
  input (the offer) versus moving 254 lines of drawing and re-pinning a
  585-line test. The prior design's decision "one shared picker in
  `components/`, props only" is reversed here: it produced two pickers, not
  one. The cross-domain rule is kept by an opener in `features/model/source.ts`
  (C2). Rejected: direction B; keeping both.
- **Body-level portal for the settings opening; `anchorRow` deleted.** The
  #541 positioning existed because the picker rendered inside the dialog's
  scroller and had to escape clipping through a transformed containing block.
  A body-level fixed element has no such ancestor; the row placement is a
  branch in the existing layout effect, and #541's scroll-closes / resize-
  re-places rules are kept for that branch. Rejected: portalling into
  `.smodal`; keeping `anchorRow` for one caller.
- **Scrim click closes dialog and picker, as today.** The picker closes on
  `pointerdown`, the scrim on `click`; both fire; today's in-dialog picker
  dies with the dialog anyway (C20). Rejected: swallowing the click, which
  reaches into `App.tsx`.
- **Per-row add and remove in the popover, batch for "add all" and "add
  group".** The prototype toggles on click; `model.add_models` stays for the
  two batch controls. Rejected: keeping the checkbox sheet with an "add n"
  button.
- **The OAuth pane keeps the browser device flow in the prototype's layout.**
  The prototype regressed to a CLI instruction; the prior design built the
  flow from the page on the owner's decision. Rejected: the CLI text.
- **Media slots stay OpenRouter-only, narrowed by kind.** Prior C14.
- **No two-column abstraction for pages that do not exist yet.** The class
  family is named `settings-tp*` after the prototype's `tp-*` so channels and
  memory can lift it; it is one page's CSS until a second page needs it.
- **The four coined terms go into `ui-web/CONTEXT.md` in this change.**
  AGENTS.md section 6: kind (of a model), offer, gateway provider and provider
  catalogue are new names; each entry names the code it is read from
  (`registry_data.KINDS`, `features/model/types.ts`, `ProviderSpec.is_gateway`,
  `model.options`), and "gateway provider" carries an _Avoid_ line because
  **Gateway** there already means the Runtime's WebSocket server. Rejected:
  leaving the glossary to the takeover.
- **No whole-design alternatives round.** The prototype is the mockup and the
  owner's answer to the merge question fixed the shape; two grill rounds
  settled the rest.

## Open questions

Assumptions the execution will confirm; each is a likely row of the
deviations ledger.

1. C21: whether the ninth nav entry moves the boot DOM golden.
2. C22: the exact `check-class-namespace` count changes.
3. `custom` filters as a gateway because the registry says so; whether the
   owner wants it under "direct" or its own bucket is decided on sight.
4. A model the registry classifies wrongly -- an embedding model with no
   `embedding` tag and a name the patterns miss -- is invisible to the
   embedding slot until an overlay states its kind. The popover's chip is the
   escape; whether a "show every kind" switch is wanted on a slot is decided
   on the first such case.
5. Whether the `scope: 'default'` derivation in `features/model/store.ts`
   (reached by no caller once every anchored opening carries a pick; three
   tests pin it) is deleted in this change or left for the composer's owner.
6. The `ModelPicker.test.tsx` snapshot ("keeps its rendered shape") changes
   with the kind row and typed row; the new snapshot is reviewed, not
   accepted blind (C11).

## Sources

- Prototype: the claude.ai artifact
  `https://claude.ai/artifact/DVhkghoDZucJPmzsCvPwQx` (title "Raven
  sub-agents"), whose settings page is a separate document embedded as
  `SETTINGS_B64` (title "Raven settings prototype", twelve nav entries as of
  2026-09-20). The relevant code: `PAGES` (the split), `PROVIDERS` and
  `PV_FILT` / `pvMatch` / `pvRows` / `pvRow` (the catalogue), `pvDetail` and
  its sections (the right pane), `KINDS` / `kindOf` / `inferredCaps` /
  `modelMeta` and `drawAddPop` (kinds and the popover), `roleProviders` /
  `drawPicker` (the slots). Not committed (repository rule on HTML assets); the
  artifact link is the shared pointer, and a decoded copy sits in the task's
  working directory.
- Prior design: `docs/specs/2026-09-17-web-settings-page-design.md`, whose
  model-page items A9-A23 and constraints C10, C14 this document cites.
- Backend: `raven/providers/registry_data.py` (`KINDS`, `kind_of`,
  `inferred_tags`, `CAPABILITIES`), `raven/providers/registry.py`
  (`ProviderSpec.is_gateway`, `auth_shape`), `raven/rpc/methods/model.py`
  (`model.options`, `model.fetch_models`, `model.add_model`).
