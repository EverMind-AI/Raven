# Settings model section: provider and model pages, kinds, one picker - implementation plan

> For the executor: follow `dev-workflow` stage 4; tick steps as `- [ ]`; when
> a situation is not covered here, check Global Constraints first, then the
> three tiers of `unattended-run` section 7 (stop / fallback / assumption
> mismatch), and record before acting. Do not guess.

**Goal**: when this plan is done, the settings dialog served by `raven serve`
on `refactor/ui_web_architecture` has a "Model providers" page (the whole
catalogue in two columns, marks, six filters, a detail pane with the add-model
popover) and a "Model settings" page (the roles card alone); every model
carries a kind that decides where it is offered; the composer chip and every
role slot open one and the same picker; and the cases of
`docs/specs/2026-09-20-web-settings-model-section-acceptance.md` pass on a real
host.

**Design document**: `docs/specs/2026-09-20-web-settings-model-section-design.md`
(acceptance items A1..A38, constraints C1..C22, Decisions, open questions).

**Approach**: two backend fields first, contract first, pinned by pytest and a
mutation. Then the picker gains its offer and the roles card switches to it,
which deletes the second picker. Then the section split and the catalogue
column, the detail pane, the add-model popover. Then fixtures, gates, the four
glossary terms, the real-host run, the PR. Every front-end task ends with
`npm test` green: the class-namespace and css-one-owner gates fail on half-moved
CSS, so a task is not done while they are red.

**Stack**: Python 3 / pydantic v2 (`raven/`), pytest via `uv run`; React 19 +
TypeScript + Vite (`ui-web/`), vitest + happy-dom, `playwright-cli` for the
real browser; `rpc-schema/openrpc.json` as the contract, `npm run gen` for the
client.

**Change map**: the design's "Change map" table, one task per row group:
Task 1 = Backend fields; Task 2 = Picker; Task 3 = Roles + Deleted; Tasks 4-6 =
Catalogue (column and pages, detail pane, popover); Task 7 = Data + Gates +
`ui-web/CONTEXT.md`; Task 8 = the acceptance run; Task 9 = the PR.

**Line numbers** cited below were read on 2026-09-20 at `b63f35087`; grep the symbol before editing, the numbers drift by a few lines as tasks land.

**Deviations already recorded**: D1 -- `providers/Providers.tsx` and its
`AddBlock` are the onboarding wizard's model step (`SetupBodies.tsx:27`) and
stay untouched; the catalogue column is the new `ProviderSide.tsx`. `Roles.tsx`
is shared with the wizard too, so Task 3 changes the wizard's picker as well
(A37 covers it).

**Baselines (copied from real runs on `feat/settings_model_section` at
`b63f35087`, 2026-09-20)**:

```
$ uv run pytest tests/test_rpc_model.py -q -p no:cacheprovider
209 passed in 8.10s
$ uv run pytest tests/test_provider_registry_data.py -q -p no:cacheprovider
24 passed in 2.83s
$ (cd ui-web && npm run gen:check)
generated.ts matches the contract (189 methods)
$ (cd ui-web && npm test)
 Test Files  186 passed (186)
      Tests  2544 passed (2544)
```

`npm test` prints happy-dom `fetch()` abort traces from an unrelated test
before the tally; the tally is the result.

## Global Constraints

Copied verbatim from the design's Constraints section. Every task's
requirements include this section.

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

### Authorization (this run)

Baseline: `unattended-run` section 1. Pre-authorized here: rebasing onto
`refactor/ui_web_architecture`; running the real-host cases with the
operator's real keys copied into a temporary home (deleted at teardown,
recorded in `deviations.md`); temporary directories and processes (cleaned up
and recorded). Never pre-authorized: editing committed team-rule files
(`AGENTS.md`, `CONTRIBUTING.md`, the gate scripts beyond the C4 exemption).

This project (AGENTS.md sections 3.4, 3.6, 3.7): while the owner is present
and no `/goal` is running, commit and push happen only on the owner's word,
one at a time; a `/goal` the owner starts carries that authorization for its
duration. The PR is opened only after its description passed the ASCII scan
and the owner saw the text.

### Limits and goal

Rounds 40, time 48 hours (the defaults). Goal text, for `/goal`, verbatim:

> Every real-host case of `docs/specs/2026-09-20-web-settings-model-section-acceptance.md` passes with its evidence rings and every command block is rerunnable verbatim; the PR to `refactor/ui_web_architecture` is open; every blocker from the self-review is handled; every external review that arrived is handled or recorded in `deviations.md`; **the owner confirms acceptance in the conversation**. Or a stop-tier entry appears in `deviations.md`. Or 40 rounds. Or 48 hours.

---

## Task 1: the two wire fields

**Delivers**: A33 (the `gateway` and `kind` assertions), the wire half of A34; C5, C7.

**Files**:
- modify: `rpc-schema/openrpc.json` (`components.schemas.ModelLabel.properties`, `components.schemas.ModelOptionProvider.properties`)
- modify: `raven/rpc/models.py:1518-1540` (`ModelLabel`), the `ModelOptionProvider` class below it
- modify: `raven/rpc/methods/model.py:227-268` (`_model_labels`), `:360-400` (`_build_provider_entry`'s returned dict)
- tests: `tests/test_rpc_model.py`, `tests/test_provider_registry_data.py`
- generated: `ui-web/src/rpc/generated.ts` (by `npm run gen`, never by hand)

**Interfaces**:
- produces, on the wire: `ModelOptionProvider.gateway: boolean` (default `false`); `ModelLabel.kind: "text" | "image" | "audio" | "video" | "embedding" | "reranker"` (required on every emitted label)
- produces, in TypeScript after `npm run gen`: `ModelOptionProvider.gateway?: boolean`, `ModelLabel.kind: string` (the generator emits `string` for a Literal; `components/ModelTags.tsx` cannot import `features/model`'s `Kind`, so `ModelTagFacts.kind?: string` stays a string and Task 2's `modelKind()` validates it at run time -- an unknown value is `text`)
- consumed by: Task 2 (`kind` through `model_labels`), Task 4 (`gateway`)

- [ ] **Step 1: the contract**

In `rpc-schema/openrpc.json`, under `ModelLabel.properties` add, and add `"kind"` to `ModelLabel.required`:

```json
"kind": {
  "type": "string",
  "enum": ["text", "image", "audio", "video", "embedding", "reranker"],
  "description": "The bucket a model list files this model under, from what it writes: a model that reads images is still text. Absent label means text."
}
```

Under `ModelOptionProvider.properties` add (not required; readers default it to false):

```json
"gateway": {
  "type": "boolean",
  "description": "Resells other vendors' models under vendor/model ids (the registry's is_gateway). The catalogue's gateway filter reads this."
}
```

- [ ] **Step 2: the pydantic models**

```python
# raven/rpc/models.py, class ModelLabel
    kind: Literal["text", "image", "audio", "video", "embedding", "reranker"]
    #: from registry_data.kind_of: what the model writes decides the bucket.

# raven/rpc/models.py, class ModelOptionProvider, beside needs_api_base
    #: The registry's is_gateway: the catalogue filter's fact, which no client
    #: can derive from a slug.
    gateway: bool = False
```

- [ ] **Step 3: the handlers**

```python
# raven/rpc/methods/model.py, _model_labels: import kind_of beside describe,
# and set the key on every emitted entry
        entry: dict[str, Any] = {"label": row.label, "kind": kind_of(row.capabilities, row.output_modalities)}

# raven/rpc/methods/model.py, _build_provider_entry, in the returned dict beside "needs_api_base"
        "gateway": bool(spec and spec.is_gateway),
```

- [ ] **Step 4: the tests**

```python
# tests/test_rpc_model.py, beside test_options_oauth_provider_warning_and_auth_type
async def test_options_rows_carry_the_gateway_flag(fake_home: Path) -> None:
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    result = await model_options({})
    assert _entry(result, "openrouter")["gateway"] is True
    assert _entry(result, "custom")["gateway"] is True
    assert _entry(result, "anthropic")["gateway"] is False


async def test_model_labels_carry_a_kind(fake_home: Path) -> None:
    # openrouter with a key and one configured model whose name is the only
    # thing that says what it is
    _write_config(
        fake_home,
        {
            "agents": {"defaults": {"model": "openrouter/anthropic/claude-sonnet-4-5"}},
            "providers": {"openrouter": {"apiKey": "sk-or-xxx", "models": ["openai/text-embedding-3-small"]}},
        },
    )
    result = await model_options({})
    labels = _entry(result, "openrouter")["model_labels"]
    assert labels["openai/text-embedding-3-small"]["kind"] == "embedding"
    assert all("kind" in v for v in labels.values())
```

`_write_config(home, payload)` and `_entry(result, slug)` are this file's own
helpers (`tests/test_rpc_model.py:47-58`); do not add a second pair.

```python
# tests/test_provider_registry_data.py: the table kinds.test.ts asserts too (Task 2)
NAME_GUESSES = [
    ("openai/text-embedding-3-small", "embedding"),
    ("BAAI/bge-reranker-v2-m3", "reranker"),
    ("my-team/bge-reranker-custom", "reranker"),
    ("jina-embeddings-v3", "embedding"),
    ("bge-m3", "embedding"),
    ("gte-large", "embedding"),
    ("rerank-co/gpt-4", "text"),           # the last path segment decides
    ("google/gemini-2.5-flash-image", "text"),  # a name says nothing; the registry row does
    ("deepseek-v4-pro", "text"),
]


@pytest.mark.parametrize(("model_id", "kind"), NAME_GUESSES)
def test_kind_of_a_name_alone(model_id: str, kind: str) -> None:
    assert kind_of(inferred_tags(model_id), ()) == kind
```

- [ ] **Step 5: run, regenerate, prove it fails**

Run: `uv run pytest tests/test_rpc_model.py tests/test_provider_registry_data.py -q -p no:cacheprovider`
Expected: the two baselines plus the eleven new cases, all passing -- copy the real tally line; do not type a number.
Run: `cd ui-web && npm run gen && npm run gen:check && git diff --stat src/rpc/generated.ts`
Expected: `generated.ts matches the contract (189 methods)`; one file changed.
Run: `uv run pytest tests/test_rpc_schema_match.py -q`
Expected: green (copy the line).
Mutation: comment out the `"gateway":` line -> `test_options_rows_carry_the_gateway_flag` fails on `KeyError: 'gateway'` or the `is True` assertion; comment out the `"kind"` key -> `test_model_labels_carry_a_kind` fails on `KeyError: 'kind'`. Restore; `git diff` shows only the intended edits.

- [ ] **Step 6: record**

Nothing expected. If `test_rpc_schema_match.py` wants `gateway` in `required`, it is the schema's rule and the field becomes required with `False` in every fixture row (Task 7); record as an assumption mismatch.

---

## Task 2: the picker's offer

**Delivers**: A28 (the composer opening), A29 (placement, both openings), A31, the picker half of A22-A27; C3 groundwork, C6, C8, C11.

**Files**:
- modify: `ui-web/src/features/model/types.ts` (add `Kind`, `Offer`)
- new: `ui-web/src/features/model/kinds.ts`, `ui-web/src/features/model/kinds.test.ts`
- modify: `ui-web/src/features/model/store.ts:18-45` (`OpenAt`), `:79-107` (`open`), `:112` (`authed` -> `listed`), `:129-160` (`choose`)
- modify: `ui-web/src/features/model/source.ts:22-34` (delete `HIDDEN_PROVIDERS` and its comment), `:85-100` (mapper: drop the filter, add `gateway`), new export `openPicker`
- modify: `ui-web/src/features/model/source.test.ts:117,325,334` (the hidden-set assertions go; add "a connected custom is listed")
- modify: `ui-web/src/features/model/ModelPicker.tsx` (kind filter, typed row, title, placement branch, scroll/resize)
- modify: `ui-web/src/features/model/ModelPicker.test.tsx` (new cases; snapshot reviewed line by line, C11)
- modify: `ui-web/src/components/ModelTags.tsx:~150` (`ModelTagFacts` gains `kind?: string`)
- modify: `ui-web/src/styles/page.css` (`.mpick` block: `.model-kind-row`, `.model-kind`, `.model-typed`)
- i18n: reuse `gui.model.type.*`, `gui.model.kind_all`, `gui.model.pick_use`, `gui.model.pick_add_to`; one new key `gui.picker.empty_kind` ("No {kind} model added here yet" / "这一家还没有添加{kind}模型")

**Interfaces**:
- produces (`types.ts`):

```ts
export type Kind = 'text' | 'image' | 'audio' | 'video' | 'embedding' | 'reranker'
export const KIND_ORDER: Kind[] = ['text', 'image', 'embedding', 'reranker', 'audio', 'video']  // the chip's cycle and the tab order

export interface Offer {
  kind: Kind
  providers?: string[]
  title?: string
  current?: { model: string; provider: string }
  pick?(model: string, provider: string, typed: boolean): Promise<void>
}
```

- produces (`kinds.ts`):

```ts
export const modelKind = (facts: ModelTagFacts | undefined): Kind =>
  (KIND_ORDER as string[]).includes(facts?.kind ?? '') ? (facts!.kind as Kind) : 'text'
export const KIND_LABEL: Record<Kind, string> = {
  text: 'gui.model.type.text', image: 'gui.model.type.image', audio: 'gui.model.type.audio',
  video: 'gui.model.type.video', embedding: 'gui.model.type.embedding', reranker: 'gui.model.type.reranker',
}
/* The sprite id in components/ModelTags.tsx for each kind: the sprite has
   `text`, `embedding`, `rerank` and the `*-generation` compounds, never a bare
   `image` / `audio` / `video` / `reranker`. Every TagGlyph drawn for a kind
   goes through this table, not the Kind value. */
export const KIND_GLYPH: Record<Kind, string> = {
  text: 'text', image: 'image-generation', audio: 'audio-generation',
  video: 'video-generation', embedding: 'embedding', reranker: 'rerank',
}
/* registry_data.inferred_tags, copied: the last path segment, rerank first. */
const RERANK = /rerank/i
const EMBED = /(?:^|[-_/])(?:bge|gte|e5|m3e|text2vec|uae|jina-clip)(?:[-_.]|$)|embed/i
export function guessKind(id: string): Kind {
  const bare = id.split('/').pop() ?? id
  if (RERANK.test(bare)) return 'reranker'
  if (EMBED.test(bare)) return 'embedding'
  return 'text'
}
```

- produces (`store.ts`): `open(anchor?: HTMLElement | null, after?: () => void, marked?: string, offer?: Offer): void` -- the fourth argument defaults to `{ kind: 'text' }`; `OpenAt.offer: Offer`; `listed(): Provider[]` replaces `authed()`; `choose(m, provider, typed = false)`.
- produces (`source.ts`): `export function openPicker(anchor: HTMLElement, offer: Offer, after?: () => void): void { store.open(anchor, after, offer.current?.model, offer) }` -- the one door the settings domain uses (C2). `Provider` gains `gateway: boolean` and `current: boolean` (the wire's `is_current`).
- consumed by: Task 3 (`openPicker`, `Offer`, `Kind`), Task 6 (`KIND_ORDER`, `KIND_LABEL`, `guessKind`, `modelKind`), Task 4 (`Provider.gateway` via the settings mapper)

- [ ] **Step 1: kinds.ts and its test first** (pure functions; the table is Task 1's `NAME_GUESSES`, same nine rows, same expected kinds -- two literal copies, one assertion each side)

```ts
// kinds.test.ts
const TABLE: Array<[string, Kind]> = [
  ['openai/text-embedding-3-small', 'embedding'], ['BAAI/bge-reranker-v2-m3', 'reranker'],
  ['my-team/bge-reranker-custom', 'reranker'], ['jina-embeddings-v3', 'embedding'], ['bge-m3', 'embedding'],
  ['gte-large', 'embedding'], ['rerank-co/gpt-4', 'text'], ['google/gemini-2.5-flash-image', 'text'], ['deepseek-v4-pro', 'text'],
]
it.each(TABLE)('guesses %s as %s, the way registry_data.inferred_tags does', (id, kind) => { expect(guessKind(id)).toBe(kind) })
it('files a model with no label, or an unknown kind, under text', () => {
  expect(modelKind(undefined)).toBe('text'); expect(modelKind({ kind: 'weird' })).toBe('text'); expect(modelKind({ kind: 'audio' })).toBe('audio')
})
```

- [ ] **Step 2: the store**

```ts
// store.ts
export interface OpenAt { host; after; footer; scope; marked; offer: Offer }
const DEFAULT_OFFER: Offer = { kind: 'text' }
const CLOSED: OpenAt = { host: null, after: null, footer: false, scope: 'session', marked: null, offer: DEFAULT_OFFER }

/* Connected, and within the offer's providers when it names any. The kind
   never hides a provider (the prototype's rule, and the tester's 2026-09-20
   report: a provider with a key but no list built vanished): it narrows the
   column, see `column`. */
export const listed = (offer: Offer = at.offer): Provider[] =>
  source().providers().filter((p) => p.on && (!offer.providers || offer.providers.includes(p.id)))

/* One provider's column: its added models of the offer's kind, with the
   current model first when the list does not carry it -- onboarding and the
   CLI set agents.defaults.model without adding it, and a model the chip names
   must be somewhere to be marked. */
export const column = (p: Provider, offer: Offer = at.offer): string[] => {
  const rows = offered(p).filter((m) => modelKind(p.labels?.[m]) === offer.kind)
  const cur = offer.current ? (offer.current.provider === p.id ? offer.current.model : null) : (p.current ? selected : null)
  return cur && !rows.includes(cur) ? [cur, ...rows] : rows
}

export function open(anchor?: HTMLElement | null, after?: () => void, marked?: string, offer: Offer = DEFAULT_OFFER): void {
  if (!installed()) return
  const accounts = source().providers().filter((p) => p.on)
  const rows = listed(offer)
  if (!rows.length) { /* the two dead-end toasts, unchanged */ ... return }
  const host = anchor || document.getElementById('modelChip')
  if (!host) return
  at = { host, after: after || null, footer: !anchor, scope: anchor ? 'default' : 'session', marked: marked || null, offer }
  announce()
}

export async function choose(m: string, provider: string, typed = false): Promise<void> {
  const { offer } = at
  if (offer.pick) {            // a slot: its own write path, before any of the scope logic
    close()
    try { await offer.pick(m, provider, typed) } catch (e) { toast(t('gui.op.switch_failed', { detail: detail(e) })) }
    return
  }
  if (typed) await source().addModel?.(m, provider)   // the composer's typed row adds first (ModelSource gains addModel)
  ... /* today's body, unchanged */
}
```

`ModelSource` (types.ts) gains `addModel?(m: string, provider: string): Promise<void>`; `modelSource` (source.ts) implements it as `gateway().call('model.add_model', { slug: provider, model: m }).then(() => loadProviders())`.

- [ ] **Step 3: the component**

In `ModelPicker.tsx`:
- `const providers = store.listed()`; `narrow(term)` maps `store.column(p)` then the term; the provider row's count is `column(p).length`. A provider whose `column` is empty while no term is typed shows `t('gui.picker.empty_kind', { kind: t(KIND_LABEL[at.offer.kind]) })` in the model pane above the typed-id row (the pane keeps `gui.picker.no_match` for a term that matched nothing).
- Title: `{at.offer.title ? <span className="model-kind-row"><b>{at.offer.title}</b></span> : null}` in `.find`, before the input.
- Typed row, after the model list, when `q && !list.some((m) => store.short(m).toLowerCase() === q)`:

```tsx
<button className="row model-typed" onClick={() => void store.choose(q, providers[prov]!.id, true)}>
  <span className="nm">{t('gui.model.pick_use', { id: q })}</span>
  <KindChip value={typedKind} onCycle={() => setTypedKind(next(typedKind))} />
  <span className="ct">{t('gui.model.pick_add_to', { name: providers[prov]!.name })}</span>
</button>
```

  `typedKind` state starts as `at.offer.title ? at.offer.kind : guessKind(q)` and re-derives when `q` changes; `KindChip` is a small local component (label from `KIND_LABEL`, glyph `<TagGlyph name={KIND_GLYPH[kind]} />`, `aria-label` = `gui.model.add_type`). The stated tags for a non-text kind go with the add: `store.choose` passes `typed` and the source's `addModel` takes an optional kind -> `capabilities` / `output_modalities` as the design's A17 says (`{embedding: [['embedding'], ['vector']], reranker: [['rerank'], ['text']], image: [['image-generation'], ['text','image']], audio: [['audio-generation'], ['audio']], video: [['video-generation'], ['video']]}`; text sends nothing).
- Placement (the layout effect): keep today's above-first branch when `!at.offer.title`; otherwise below-first:

```ts
const r = host.getBoundingClientRect(); const b = el.getBoundingClientRect()
let top = r.bottom + 6
if (top + b.height > vh - 12 && r.top - 6 - b.height >= 12) top = r.top - 6 - b.height
el.style.top = `${Math.max(12, Math.min(top, vh - b.height - 12))}px`
el.style.left = `${Math.max(12, Math.min(r.left, vw - b.width - 12))}px`
```

- Scroll and resize, only when `at.offer.title`: the #541 effect moved here verbatim from the deleted `components/ModelPicker.tsx:75-98` (document `scroll` in capture closes when `host.getBoundingClientRect().top` moved more than 1px; `resize` re-runs the placement and refreshes the baseline).

- [ ] **Step 4: the source and its test**

Delete `HIDDEN_PROVIDERS` and the filter at `source.ts:85`; add `gateway: !!p.gateway` to the mapper; add `openPicker` and `addModel`. In `source.test.ts`: the harness line 117 that exposes `HIDDEN_PROVIDERS` goes; the three cases at 324-327, 330-335 and 337-342 that assert the set or its effect go too -- the last, `it('leaves a hidden provider configured and routable, only unlisted', ...)`, asserts `expect(rows).toEqual([])` for a connected `custom`, which is now the opposite of the design (C9). Replace the three with:

```ts
it('lists a connected custom section like any other provider', async () => {
  const rows = await listed([row('anthropic'), row('custom', { authenticated: true })])
  expect(rows.map((p) => p.id)).toEqual(['anthropic', 'custom'])
})
```

- [ ] **Step 5: the picker tests**

Add to `ModelPicker.test.tsx` (harness at lines 60-104; `openIt` gains a fourth argument):

```ts
it('lists every connected provider and narrows each column to the kind; a column with none says so', () => { /* two providers, one with only an embedding model; open({kind:'text'}); two provider rows, the second counted 0 and its pane showing gui.picker.empty_kind */ })
it('lists the current model in its provider column even when the list does not carry it', () => { /* provider p1 is_current with models ['a']; store.setCurrent('b'); open(); p1 column == ['b','a'], 'b' marked */ })
it('a slot opening lists the slot kind and calls its pick, never persist', async () => { /* open(anchor, undefined, 'x', {kind:'embedding', providers:['p1'], title:'Embedding', pick}) ; click row; expect pick called, h.persisted empty */ })
it('a typed id adds to the provider and then picks', async () => { /* type 'my-model'; click .model-typed; expect addModel then persist in order */ })
it('a slot opening hangs below its anchor and closes when the anchor scrolls away', () => { /* boxes via mocked getBoundingClientRect; dispatch scroll after moving the mock */ })
it('the composer opening is unchanged: above the card, no title, no typed-kind chip when nothing is typed', () => { /* snapshot */ })
```

Run: `cd ui-web && npx vitest run src/features/model -q`
Expected: every existing case still passing plus the new ones (copy the tally); the `.snap` diff shows only the kind row / typed row / title lines.
Mutation: drop the kind filter inside `column` -> the first new case fails on the second provider's count; drop the `cur` prepend -> the current-model case fails; make `choose` ignore `offer.pick` -> the slot case fails on `h.persisted`. Restore.

- [ ] **Step 6: record**

Open question 5 (the `scope: 'default'` derivation): this task leaves it. If a test in `ModelPicker.test.tsx` that pins it turns red because of the offer default, record and fix the test's call, not the derivation.

---

## Task 3: the roles card on the shared picker; the second picker goes

**Delivers**: A2 (the empty-state half), A22-A27 (the roles half), A31; C2, C3.

**Files**:
- modify: `ui-web/src/features/settings/providers/Roles.tsx:1-60` (imports, `ROLE_KIND`), `:130-135` (delete `pickerProviders`), `:174-232` (`RolePill`)
- modify: `ui-web/src/features/settings/providers/Roles.test.tsx` (the picker cases spy `openPicker`)
- modify: `ui-web/src/features/settings/store.ts:83-84,115,234` (delete `picker`); `ui-web/src/features/settings/store.test.ts:39` (drop `s.picker` from the asserted array)
- delete: `ui-web/src/components/ModelPicker.tsx`, `ui-web/src/components/ModelPicker.test.tsx`
- modify: `ui-web/src/lib/popover.ts:57-113` (delete `DIALOG`, `bounds`, `anchorRow`; trim the header comment's paragraph about them)
- modify: `ui-web/src/styles/page.css:2317-2341` (delete the `.model-picker*` rules), `ui-web/src/features/settings/styles.css:257-265` (delete the nine rules)
- check: `ui-web/scripts/check-class-namespace.mjs` pins (run the gate; update only the `settings` / `model` numbers if it asks, C4 exemption)

**Interfaces**:
- consumes: `openPicker(anchor, offer, after)`, `Offer`, `Kind` from `features/model/source.ts` / `types.ts` (Task 2)
- produces: `ROLE_KIND: Record<Role['id'], Kind>` (exported for the tests)

- [ ] **Step 1: the table and the pill**

```tsx
// Roles.tsx
import { openPicker } from '../../model/source'
import type { Kind, Offer } from '../../model/types'

export const ROLE_KIND: Record<Role['id'], Kind> = {
  chat: 'text', curator: 'text', title: 'text', memllm: 'text', gate: 'text', multimodal: 'text',
  embedding: 'embedding', rerank: 'reranker', image: 'image', speech: 'audio', video: 'video',
}

export function RolePill({ role }: { role: Role }): JSX.Element {
  const pill = useRef<HTMLButtonElement>(null)
  const s = store.get()
  const val = roleValue(role, s.snap)
  const kind = ROLE_KIND[role.id]
  const provs = roleProviders(role, s.snap)
  const inherit = !!role.keys
  const chat = roleValue(ROLES[0]!, s.snap)
  if (!provs.length && !val) {
    return (
      <button type="button" className="mini ghost"
        onClick={() => { if (role.media) store.set({ provider: MEDIA_PROVIDER }); store.setTab('provider') }}>
        {t(role.media ? 'gui.settings.roles.connect_openrouter' : role.everos ? 'gui.settings.roles.no_key_provider' : 'gui.settings.roles.no_provider')}
      </button>
    )
  }
  const offer: Offer = {
    kind, providers: provs.map((p) => p.id), title: roleName(role),
    current: val ?? (inherit ? chat : undefined),
    pick: (m, p, typed) => store.run(`role:${role.id}`, () => setRole(role, m, p, typed)).then(() => undefined),
  }
  return (
    <span className={cls}>
      <button ref={pill} type="button" className="settings-pm" aria-label=... onClick={() => openPicker(pill.current!, offer)}>...</button>
      {clearable && <button .../>}
    </span>
  )
}
```

`Kind` and `Offer` are types from `../../model/types`; nothing from `kinds.ts` is needed here. No new i18n key: the three empty-state texts (`connect_openrouter`, `no_key_provider`, `no_provider`) exist and keep their meaning -- the state is "no allowed provider connected", as today.

- [ ] **Step 2: delete the second picker**

`git rm ui-web/src/components/ModelPicker.tsx ui-web/src/components/ModelPicker.test.tsx`; remove `.model-picker*` from `page.css` and `styles.css`; remove `anchorRow`, `bounds`, `DIALOG` from `lib/popover.ts` and the header paragraph "The settings dialog, which is the box..." with them; remove `picker` from the settings store and its reset in `setTab`.

- [ ] **Step 3: tests**

In `Roles.test.tsx`: the cases at lines 71, 94, 102, 111 that render `ModelPicker` now spy `openPicker` (`vi.spyOn(modelSource, 'openPicker')`) and assert the offer: `kind`, `providers`, `title`, `current`; the typed case calls `offer.pick('id', 'p', true)` and asserts `add_model` then the role write. Add:

```ts
it('offers each slot its kind over every allowed connected provider, current model included', () => {
  /* harness snapshot: p1, p2 both connected key providers; click the embedding pill -> openPicker offer.kind == 'embedding', offer.providers == ['p1','p2'], offer.current == the slot's {model, provider}; chat -> 'text', same providers */
})
it('a slot with no allowed provider connected offers the Model providers page instead of a picker', () => {
  /* snapshot with no connected provider; the chat slot renders the no_provider button; click -> store.get().tab === 'provider'; openPicker not called */
})
```

Run: `cd ui-web && npx vitest run src/features/settings src/components src/lib -q && npm test`
Expected: green; the gates green (copy tallies). C3's grep: `grep -rn "components/ModelPicker\|anchorRow\|model-picker" ui-web/src` prints nothing.
Mutation: set `ROLE_KIND.embedding = 'text'` -> the first new case fails on `offer.kind`. Restore.

- [ ] **Step 4: record**

`SetupBodies.test.tsx` renders `<Roles />` for the wizard; if it pinned the old inline picker, update the case and note it under D1.

---

## Task 4: the section split and the catalogue column

**Delivers**: A1, A2 (the tab switch half), A3, A4, A5, A6, A36 (the nav labels).

**Files**:
- modify: `ui-web/src/features/settings/store.ts:31-32` (`SectionId`, `SECTIONS`), state (`provQ`, `provFilt`), `setTab` reset
- modify: `ui-web/src/features/settings/SettingsApp.tsx:27-70` (`ICON`, `NAV`, `PAGE`)
- new: `ui-web/src/features/settings/pages/Provider.tsx`, `ui-web/src/features/settings/providers/ProviderSide.tsx`, `ProviderSide.test.tsx`
- modify: `ui-web/src/features/settings/pages/Model.tsx` (render `<Roles />` only)
- modify: `ui-web/src/features/settings/types.ts:15-50` (`ProviderRow.gateway: boolean`), `source.ts` (the mapper -- it reads `features/model/source`'s `providers()`, so `gateway` arrives from Task 2's mapper; only the type changes)
- modify: `ui-web/src/features/settings/styles.css` (append the `.settings-tp*` block: the prototype's `.tp-*` rules at lines 283-394 of the decoded document, class names prefixed)
- modify: `i18n/messages.json`: new `gui.settings.nav.provider` ("Model providers" / "模型服务商"); `gui.settings.nav.model` text becomes "Model settings" / "模型配置"; new `gui.settings.providers.filter_direct` ("Direct vendors" / "直连厂商"), `filter_gateway` ("Aggregators" / "聚合网关"), `filter_oauth` ("Browser sign-in" / "浏览器授权"); reuse `gui.model.prov_filter.all`, `gui.model.prov_filter.on` (connected), `gui.model.kind.local` (本地部署), `gui.model.prov_search_ph`, `gui.model.prov_no_match`, `gui.model.is_default`, `gui.settings.providers.needs_key` / `needs_auth` / `needs_base` / `connected`
- tests: `store.test.ts` (SECTIONS order), `SettingsApp.test.tsx` (nine entries), `ProviderSide.test.tsx`

**Interfaces**:
- produces: `SectionId` gains `'provider'`; `SECTIONS = ['general','usage','provider','model','skills','tools','plugins','archive','about']`; store state `provQ: string`, `provFilt: ProvFilter` with `type ProvFilter = 'all' | 'on' | 'direct' | 'gateway' | 'oauth' | 'local'`; `export const provMatch = (p: ProviderRow, q: string, f: ProvFilter): boolean` and `export const provRows = (rows: ProviderRow[], q: string, f: ProvFilter): ProviderRow[]` (connected first, registry order kept) in `ProviderSide.tsx`
- consumes: `ProviderIcon` from `components/ProviderMark`; `ProviderDetail` (Task 5 reshapes it; the import is the same)

- [ ] **Step 1: store and nav**

```ts
// store.ts
export type SectionId = 'general' | 'usage' | 'provider' | 'model' | 'skills' | 'tools' | 'plugins' | 'archive' | 'about'
export const SECTIONS: SectionId[] = ['general', 'usage', 'provider', 'model', 'skills', 'tools', 'plugins', 'archive', 'about']
export type ProvFilter = 'all' | 'on' | 'direct' | 'gateway' | 'oauth' | 'local'
// state: provQ: '', provFilt: 'all'  (reset in setTab with the others)
```

```tsx
// SettingsApp.tsx: ICON gains the prototype's two glyphs
provider: <><path d="M12 3.5 20 8v8l-8 4.5L4 16V8l8-4.5Z" /><path d="M12 12v8.5M12 12 4 8M12 12l8-4" /></>,   // the old model glyph
model: <><path d="M4 6.5h9M4 12h8M4 17.5h6" /><path d="m14.5 16 2 2 4-4.5" /></>,                            // the prototype's 'assign'
// NAV: provider: 'gui.settings.nav.provider'; PAGE: provider: Provider
```

- [ ] **Step 2: the column**

```tsx
// providers/ProviderSide.tsx
export const provMatch = (p: ProviderRow, q: string, f: ProvFilter): boolean => {
  const needle = q.trim().toLowerCase()
  if (needle && !`${p.name} ${p.id}`.toLowerCase().includes(needle)) return false
  if (f === 'all') return true
  if (f === 'on') return p.on
  if (f === 'gateway') return !!p.gateway
  if (f === 'oauth') return p.kind === 'oauth'
  if (f === 'local') return p.kind === 'local'
  return !p.gateway && (p.kind === 'key' || p.kind === 'endpoint')   // direct
}
/* Connected first: the question this column answers most often is "which do I have". Order within each half is the registry's, which is the wire's. */
export const provRows = (rows: ProviderRow[], q: string, f: ProvFilter): ProviderRow[] => {
  const kept = rows.filter((p) => provMatch(p, q, f))
  return [...kept.filter((p) => p.on), ...kept.filter((p) => !p.on)]
}

export function ProviderSide(): JSX.Element {
  const s = store.get()
  const rows = provRows(s.snap.providers, s.provQ, s.provFilt)
  return (
    <div className="settings-tp-side">
      <div className="settings-tp-find">
        <input value={s.provQ} placeholder={t('gui.model.prov_search_ph')} onChange={(e) => store.set({ provQ: e.currentTarget.value })} />
        <FilterMenu value={s.provFilt} onPick={(f) => store.set({ provFilt: f })} />
      </div>
      {rows.length ? (
        <div className="settings-tp-list">
          {rows.map((p) => (
            <div key={p.id} className="settings-tp-row" data-id={p.id} aria-current={s.provider === p.id}>
              <button type="button" className="settings-tp-hit" onClick={() => store.set({ provider: p.id, err: '' })}>
                <ProviderIcon id={p.id} name={p.name} />
                <span className="settings-tp-nm">{p.name}</span>
                {s.snap.curProvider === p.id && <span className="settings-tp-def">{t('gui.model.is_default')}</span>}
                {p.on && <span className="settings-tp-dot" />}
              </button>
            </div>
          ))}
        </div>
      ) : <div className="settings-tp-empty">{t('gui.model.prov_no_match')}</div>}
    </div>
  )
}
```

`FilterMenu` is a `<select>` (native, one element, keyboard-accessible; the prototype's funnel button is drawn beside it as the label) -- rung 4 of the ladder over a hand-made popover.

- [ ] **Step 3: the pages**

```tsx
// pages/Provider.tsx
export function Provider(): JSX.Element {
  const s = store.get()
  const slug = s.provider ?? s.snap.curProvider ?? null
  return (
    <div className="settings-tp">
      <ProviderSide />
      {slug && s.snap.providers.some((p) => p.id === slug)
        ? <ProviderDetail slug={slug} />
        : <div className="settings-tp-none">{t('gui.settings.providers.pick_one')}</div>}
    </div>
  )
}
// pages/Model.tsx
export function Model(): JSX.Element { return <Roles /> }
```

One more key: `gui.settings.providers.pick_one` ("Choose a provider on the left" / "在左边选一家服务商").

- [ ] **Step 4: tests**

```ts
// ProviderSide.test.tsx (fixture rows via test/settingsHarness.ts, which Task 7 gives gateway + kind)
it('lists every row the snapshot holds, connected first, in the snapshot order within each half', () => {
  /* provRows(rows, '', 'all') has rows.length entries; the first k are the k connected rows in their original relative order; the rest likewise */
})
it('filters: connected, gateway, browser sign-in, local, direct are disjoint and cover every row together', () => {
  /* for the harness rows: the five sets from provMatch are pairwise disjoint and their union equals the row set */
})
it('searches name and id, shows the no-match text, and restores on clear', () => {
  /* set provQ 'moon' -> one row; 'zzz' -> the gui.model.prov_no_match text; '' -> full count */
})
it('marks the chat role provider as default and every connected row with a dot', () => {
  /* snap.curProvider row has .settings-tp-def; rows with on === true have .settings-tp-dot; no other row has either */
})
it('a row carries data-id and selecting it sets store.provider', () => {
  /* [data-id] attribute equals the row id; click -> store.get().provider === id */
})
// store.test.ts: SECTIONS is the nine in order; setTab resets provQ / provFilt
// SettingsApp.test.tsx: nine nav entries; the panel's data-section follows the tab
```

Run: `cd ui-web && npx vitest run src/features/settings -q && npm test`
Expected: green (copy). `npm run type-check` green.
Mutation: swap `'provider'` and `'model'` in `SECTIONS` -> the nav test fails on order; make `provMatch` treat `direct` as `!p.gateway` alone -> the disjointness test fails (oauth rows land in direct). Restore.

- [ ] **Step 5: record**

C21 (boot DOM golden): run `cd ui-web && python3 build.py` here; if the golden moved, the commit body explains why (a nav entry is runtime-portalled and should not) -- record as an assumption mismatch either way.

---

## Task 5: the detail pane in the prototype's shape

**Delivers**: A7, A8, A9, A10, A11, A12, A13, A20, A21; C10 (the key pane reuses `KeyInput` and sends a key only when one was typed).

**Files**:
- modify: `ui-web/src/features/settings/providers/ProviderDetail.tsx:18-135` (`Connection` becomes the head + key / address / Azure / OAuth sections), `:196-235` (`Models`: tags plus the "add model" button that opens Task 6's popover), `:250-360` (`Advanced`: the address override row stays for key providers only), `:109-126` (`ProviderDetail`: head, body by shape, footer)
- modify: `ui-web/src/features/settings/providers/ProviderDetail.test.tsx` (status line, address placement, footer wording)
- modify: `ui-web/src/features/settings/styles.css` (the `.settings-tp-main`, `-head`, `-sec`, `-lab`, `-in`, `-box`, `-hint`, `-foot` rules from the same prototype block)
- i18n: reuse `gui.settings.providers.connected` / `needs_key` / `needs_auth` / `needs_base` for the status line, `gui.settings.providers.base_override`, `gui.settings.providers.disconnect`; new `gui.settings.providers.disconnect_oauth` ("Disconnect authorisation" / "断开授权"), `disconnect_local` ("Disconnect this server" / "断开这台服务"), `disconnect_key` ("Disconnect and clear the key" / "断开并清除密钥")

**Interfaces**:
- consumes: `ProviderRow` (with `gateway`, Task 4), `KeyInput` (`components/KeyInput.tsx`, already masked with an eye), `kindOf` / `needsKey` / `takesKey` / `takesBase` from `Providers.tsx` (unchanged)
- produces: `export function ProviderDetail({ slug }: { slug: string }): JSX.Element | null` (same signature); `Models` renders `<button type="button" className="mini ghost" aria-expanded={open} onClick={() => store.popOpen(p.id)}>` -- Task 6 supplies `popOpen`

- [ ] **Step 1: head and status**

```tsx
function Head({ p }: { p: ProviderRow }): JSX.Element {
  const state = p.on ? 'connected' : kindOf(p) === 'oauth' ? 'needs_auth' : kindOf(p) === 'local' ? 'needs_base' : 'needs_key'
  const link = p.keyUrl || p.homepage
  return (
    <div className="settings-tp-head">
      <ProviderIcon id={p.id} name={p.name} />
      <div className="settings-tp-ttl">
        <div className="settings-tp-name">{p.name}{link && <a href={link} target="_blank" rel="noopener" aria-label={t('gui.model.docs')}>...</a>}</div>
        <div className={`settings-tp-state ${p.on ? 'settings-tp-live' : ''}`}>{t(`gui.settings.providers.${state}`)}</div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: the body by shape** (the prototype's `pvDetail`), the sections being today's `Row`s of the `Connection` card re-homed under `.settings-tp-sec` blocks:

```tsx
export function ProviderDetail({ slug }: { slug: string }): JSX.Element | null {
  const s = store.get()
  const p = s.snap.providers.find((x) => x.id === slug)
  if (!p) return null
  const shape = kindOf(p)                       // 'oauth' | 'local' | 'key'
  const addressInBody = shape === 'key' && (p.gateway || !!p.needsBase)
  return (
    <div className="settings-tp-main">
      <Head p={p} />
      {shape === 'oauth' && <OauthSection p={p} />}
      {shape === 'local' && <AddressSection p={p} label={t('gui.settings.providers.base')} placeholder="http://localhost:11434" connect />}
      {shape === 'key' && (
        <>
          <KeySection p={p} />
          {addressInBody && <AddressSection p={p} label={t('gui.settings.providers.base')} placeholder={p.needsBase ? 'https://' : t('gui.settings.providers.base_default')} />}
          {p.id === AZURE && <AzureFields p={p} />}
        </>
      )}
      <Models p={p} />
      {shape === 'key' && <Advanced p={p} address={!addressInBody} />}
      {p.on && <Footer p={p} shape={shape} />}
    </div>
  )
}
```

`OauthSection`, `KeySection`, `AddressSection`, `AzureFields` are the existing `Connection` markup split at its `Row` boundaries (`ProviderDetail.tsx:46-100`), state and handlers moved with them; `Advanced` gains an `address` prop that shows the override row only when the body did not (`:250-262`); `Footer` is the disconnect button with `disconnect_<shape>` wording and the existing `rolesUsing` refusal (`:21-25`).

- [ ] **Step 3: tests**

Update the cases at `ProviderDetail.test.tsx:64,76,148,162,181` to the new structure (same assertions, new selectors); add:

```ts
it('states connected / needs a key / needs authorisation / needs an address by shape and connection', () => {
  /* four harness rows: key+on, key+off, oauth+off, local+off -> the four status keys' text in .settings-tp-state */
})
it('puts the address in the body for a gateway or a needs-base provider and under Advanced otherwise', () => {
  /* openrouter (gateway): the address input is outside the Advanced fold; moonshot (key): inside it */
})
it('words the disconnect footer by shape', () => {
  /* connected oauth / local / key rows -> disconnect_oauth / disconnect_local / disconnect_key texts */
})
```

Run: `cd ui-web && npx vitest run src/features/settings/providers -q && npm test`
Expected: green (copy).
Mutation: swap the `needs_auth` / `needs_base` branches -> the status case fails on the local provider's line. Restore.

- [ ] **Step 4: record**

If the `check-class-namespace` gate's `settings` pin moves (it counts unprefixed class uses; every new class here is `settings-` prefixed, so it should not), record and update the pin (C22).

---

## Task 6: the add-model popover

**Delivers**: A14, A15, A16, A17, A18, A19; C12.

**Files**:
- new: `ui-web/src/features/settings/providers/AddModelPop.tsx`, `AddModelPop.test.tsx`
- modify: `ui-web/src/features/settings/store.ts:34-45,79,111` (`Sheet` -> `AddPop`), `:297-325` (`sheetOpen` -> `popOpen`, `sheetPatch` -> `popPatch`; `sheetToggle` goes -- a click writes)
- modify: `ui-web/src/features/settings/providers/ProviderDetail.tsx:135-195` (delete `Sheet`)
- modify: `ui-web/src/features/settings/types.ts` (`ModelCandidate` already carries `kind`; nothing)
- modify: `ui-web/src/features/settings/styles.css` (the `.settings-ap*` block: the prototype's `.apop`, `.mkind`, `.apm`, `.mgroup`, `.apwait`, `.mpempty`, `.mphr` rules at lines 497-634 of the decoded document, prefixed)
- i18n: reuse `gui.model.kind_all`, `gui.model.type.*`, `gui.model.add_all`, `gui.model.add_group`, `gui.model.add_type`, `gui.model.pick_use`, `gui.model.catalogue_n`, `gui.settings.providers.fetching` / `no_list` / `no_match` / `added`; new `gui.settings.providers.add_model` ("Add model" / "添加模型"), `gui.settings.providers.showing_added` ("showing {n} · added {m}" / "显示 {n} 个 · 已添加 {m}")

**Interfaces**:
- produces (`store.ts`):

```ts
export interface AddPop { slug: string; q: string; state: 'loading' | 'ready' | 'failed'; items: ModelCandidate[]; kind: 'all' | Kind; folded: Record<string, boolean>; typedKind: Kind | null }
export async function popOpen(slug: string): Promise<void>   // fetchModels, as sheetOpen did
export function popPatch(patch: Partial<AddPop>): void
export function popToggle(slug: string, id: string, kind?: Kind): Promise<boolean>   // add_model or remove_model (with the rolesUsing refusal), one write
export function popAddMany(slug: string, ids: string[]): Promise<boolean>            // add_models
```

- consumes: `KIND_ORDER`, `KIND_LABEL`, `KIND_GLYPH`, `guessKind`, `modelKind` (Task 2); `ModelTags`, `TagGlyph` (`components/ModelTags`; every kind glyph through `KIND_GLYPH`); `rolesUsing`, `roleName` (Roles.tsx)

- [ ] **Step 1: the pure parts first, with tests**

```ts
// AddModelPop.tsx exports for the test
export const shownRows = (pop: AddPop, configured: string[]): ModelCandidate[]   // items ∪ configured-not-in-items, then the term, then the kind
export const kindCounts = (rows: ModelCandidate[]): Array<['all' | Kind, number]>  // 'all' first, then KIND_ORDER kinds with count > 0
export const groups = (rows: ModelCandidate[]): Array<[string, ModelCandidate[]]>  // vendor prefix; '' last; one group -> [['', rows]]
export const statedTags = (kind: Kind): { capabilities?: string[]; output_modalities?: string[] }  // text -> {}
```

```ts
// AddModelPop.test.tsx
it('counts kinds over the search result and hides a kind with no rows', () => {
  /* items: 3 text, 1 image, 1 embedding; kindCounts(shownRows) == [['all',5],['text',3],['image',1],['embedding',1]]; with q 'gem' matching one text row -> [['all',1],['text',1]] */
})
it('groups a gateway by vendor prefix, unprefixed rows last, and a flat list when nothing is prefixed', () => {
  /* ids a/x, a/y, b/z, plain -> [['a',..],['b',..],['',[plain]]]; ids x, y -> [['',[x,y]]] */
})
it('a typed id not in the list gets a chip starting on the name guess and cycling six kinds', () => {
  /* q 'my-team/bge-reranker-custom' -> chip label type.reranker; six clicks -> back to reranker through six distinct labels */
})
it('a click on a listed row adds it in one write, a second click removes it, a used model is refused', () => {
  /* source.provider spy: click -> ('add_model', {slug, model}); click again -> ('remove_model', ...); a model rolesUsing names -> store.refuse called, no provider call */
})
it('add all writes the shown unadded rows in one add_models call', () => {
  /* addModels spy called once with exactly the shown ids minus configured */
})
it('enter adds the first unadded row, else the typed id, and never removes', () => {
  /* Enter with an unadded first row -> add_model for it; all added + q typed -> add_model for q; all added, no q -> no call */
})
it('states tags for a non-text typed kind and nothing for text', () => { expect(statedTags('text')).toEqual({}); expect(statedTags('embedding')).toEqual({ capabilities: ['embedding'], output_modalities: ['vector'] }) })
```

- [ ] **Step 2: the component**: the prototype's `drawAddPop` translated -- search input (`autoFocus`), the kind row (`role="tablist"`, buttons with `aria-pressed`), the grouped rows (`role="menuitemcheckbox"`, `aria-checked` = configured), the typed row with the chip (`aria-label={t('gui.model.add_type')}`), the footer (`showing_added`, "add all (k)"). Placement per the prototype's `placeAddPop` (below the button; above only when fewer than 220px remain below and more above; `max-height` from the room), positioned `fixed` inside the dialog; closes on Escape, on an outside `pointerdown`, and when its button leaves the DOM. A click on a row -> `store.popToggle(slug, id)`; the typed row -> `store.popToggle(slug, q, typedKind)`; Enter -> the A19 rule.

- [ ] **Step 3: run**

Run: `cd ui-web && npx vitest run src/features/settings -q && npm test`
Expected: green (copy).
Mutation: count kinds over `items` instead of the search result -> the first case fails after a term is typed; make `statedTags('text')` return `{ capabilities: [] }` -> the last case fails. Restore.

- [ ] **Step 4: record**

`MODALITIES` in `raven/providers/registry_data.py:61` is `("text", "image", "video", "audio", "vector")` (read 2026-09-20), so the embedding overlay's `output_modalities: ["vector"]` is accepted by `_stated_overlay`'s `clean_tags`. Nothing to record unless `clean_tags` drops it at run time -- then it is an assumption mismatch.

---

## Task 7: fixtures, gates, terms

**Delivers**: A32, A34, A35, A36, A38; C1, C4, C6, C9 (their checks), C22.

**Files**:
- modify: `ui-web/src/rpc/fixtures/model.ts` (every row `gateway`; `model_labels` for the rows the offline slots need: openrouter with an embedding, a reranker and an image model, kinds set), `ui-web/src/test/settingsHarness.ts` (same)
- modify: `ui-web/CONTEXT.md` (four entries, `**Term**:` form, each naming its code)
- modify: `ui-web/scripts/check-class-namespace.mjs` (the two pins, only if the gate asks; C4)
- run: every gate

- [ ] **Step 1: fixtures**: add `gateway: true` to the openrouter and any aggregator row, `gateway: false` elsewhere; add `model_labels` entries with `kind` for the seed-like set (`anthropic/claude-sonnet-4-5` text, `openai/text-embedding-3-small` embedding, `BAAI/bge-reranker-v2-m3` reranker, `google/gemini-2.5-flash-image` image). The `fixture-shape` gate checks the rows against the generated types.

- [ ] **Step 2: the four terms** in `ui-web/CONTEXT.md`, after **Popover**:

```markdown
**Kind**:
The bucket a model list files a model under -- `text`, `image`, `audio`,
`video`, `embedding`, `reranker` -- as `raven/providers/registry_data.py`'s
`kind_of` derives it from what the model writes, carried on the wire as
`model_labels[id].kind` and read by `src/features/model/kinds.ts`. A model
with no label is `text`. _Avoid_: "type" and "category" for this; "kind" alone
for a provider's auth shape, which the rows call `auth_type`.

**Offer**:
What one opening of the model picker lists: a kind, optionally a subset of
providers, optionally a title and what a pick does (`Offer` in
`src/features/model/types.ts`). The composer opens with the default offer; a
role slot opens with its own through `openPicker` in `features/model/source.ts`.

**Gateway provider**:
A provider that resells other vendors' models under `vendor/model` ids --
`ProviderSpec.is_gateway` in `raven/providers/registry.py`, `gateway` on a
`model.options` row. _Avoid_: "gateway" alone, which in this document is the
page's RPC entry point above.

**Provider catalogue**:
Every provider `model.options` returns, connected or not; the left column of
the settings' Model providers page. _Avoid_: "catalogue" alone where the
Runtime's Session Mode catalogue could be meant.
```

- [ ] **Step 3: run every gate and every check**

Run, each, and copy the output into the report:
- `cd ui-web && npm run type-check && npm test && npm run gen:check && npm run build && python3 build.py`
- `make check-source-language`
- the A36 one-liner; the A38 grep (`4`)
- C3, C4, C6, C7, C9 "How to check" commands from the design
- `uv run pytest tests/test_rpc_model.py tests/test_provider_registry_data.py tests/test_rpc_schema_match.py -q -p no:cacheprovider`

Expected: all green; each grep prints what its constraint says. A `check-class-namespace` pin change is made here, explained in the commit body (C22).

---

## Task 8: the real-host acceptance run

**Delivers**: every A1..A32, A37 with evidence; the G4 screenshots; the star journeys once more on the same host.

- [ ] **Step 1**: chapter 0 of the acceptance document, verbatim, into `.work_context/ui_web_model_settings/acceptance/env/host.sh` + `seed.sh` (copies of the command blocks, nothing the document does not say); the built page from Task 7.
- [ ] **Step 2**: every `yes` case in order, rings T P E V R into `acceptance/run.md` and `acceptance/snapshots/`; expected values copied from the run.
- [ ] **Step 3**: the G4 pass: one screenshot per surface this change touched (Model providers with the column and a detail; the popover with kind tabs; Model settings; a slot picker; the composer picker), read by eye against the prototype before anything is called done -- the 2026-09-18 lesson.
- [ ] **Step 4**: `/ponytail-review` on the diff; `/ponytail-debt` into `deviations.md`; the four-dimension review of `review-prompts.md` section 2 (completeness with spec + deviations; correctness; appropriateness; maintainability), findings merged, blockers reproduced.
- [ ] **Step 5**: the acceptance report (`references/acceptance-report-template.html`) with the star table's five journeys and the deviations ledger for the owner.

---

## Task 9: the PR

- [ ] **Step 1**: `git fetch github && git merge-tree --write-tree HEAD github/refactor/ui_web_architecture`; rebase if behind; rerun Task 7's commands.
- [ ] **Step 2**: on the owner's word: push `feat/settings_model_section` over SSH (`git@github.com:EverMind-AI/Raven.git`), base `refactor/ui_web_architecture`.
- [ ] **Step 3**: the description from `.github/pull_request_template.md`; the ASCII scan of AGENTS.md section 3.7; the owner sees the text; `gh pr create`; the review loop of `unattended-run` section 5 until no blocker and CI green.
