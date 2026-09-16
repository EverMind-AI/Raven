// @vitest-environment happy-dom
/* The per-conversation model refresh, driven with deferred responses.
 * model.options does its catalogue work off-thread, so a refresh for a
 * conversation the reader has left can land after the one they moved to; the
 * generation ticket is what keeps the late answer from repainting the page. A
 * synchronous stub cannot exercise that, so this runs the real functions.
 *
 * Migrated from scripts/model-refresh-live.test.mjs when the model and
 * settings sources left the legacy layer: the assertions are unchanged, and
 * what moved is how the harness reaches them -- imports of the two source
 * modules and the override part, instead of one part and its collaborators.
 */

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/legacy-part.mjs'

/* One entry of the traffic log: a method name with its params, or a page verb
   with whatever it was handed. */
type Call = [string, unknown?]
/* A call the case has not settled yet. */
interface Deferred {
  method: string
  params: Record<string, unknown>
  res(answer: unknown): void
  rej(error: unknown): void
}
interface Options {
  session?: string | null
  answers?: Record<string, unknown> | null
}

/* The two sources and the override part in one fresh graph: the staged picks
 * live in the part and both sources read them, and the generation ticket is a
 * module of its own that all three share.
 *
 * `answers` resolves a call at once; without it every call is held in
 * `pending` for the test to settle in whatever order the race needs.
 */
async function live({ session = null, answers = null }: Options = {}) {
  const calls: Call[] = []
  const pending: Deferred[] = []
  /* A view switch re-reads the default model and the permission mode, and
     those two reads are the switch's own -- no case here settles them. Held
     apart so the indices below stay the indices of the calls under test. */
  let switching = false
  await loadPart(() => import('../model/source'), {
    fakes: {
      'src/shell/session': { current: () => session, setCurrent: () => {} },
      'src/shell/banner': { draw: () => {} },
      'src/shell/toast': { show: (text: string) => calls.push(['toast', text]) },
      'src/shell/tier': { load: () => {} },
      'src/shell/perm': { setFromConfig: (m: string) => calls.push(['setPermMode', m]) },
      'src/shell/bridge': {
        t: (key: string, _vars?: unknown, fallback?: string) => (fallback ?? key),
        ds: () => ({}),
        shell: () => ({}),
      },
      'demo/010-kernel.js': { $: looseQuery() },
      'demo/040-state.js': {
        modelCurrent: () => '',
        modelSet: (m: string) => calls.push(['modelSet', m]),
        loadDraft: () => {},
        parkDraft: () => {},
        queueClear: () => {},
        stop_: () => {},
        turn: { dispatch: () => {}, busy: () => false },
      },
      'demo/050-rail.js': { sessionDraw: () => {} },
      'demo/060-conversation.js': { pitch: () => {}, unpitch: () => {} },
      'demo/090-composer.js': { drawMeter: () => {}, goState: () => {}, ta: { focus: () => {} } },
      'demo/100-workspace.js': { setWs: () => {}, wsReset: () => {} },
      'src/state/session/residency': { park: () => {} },
    },
    islands: {
      settings: { openModels: () => calls.push(['openModels']) },
      rail: { endRename: () => {} },
      model: { current: () => '', setCurrent: (m: string) => calls.push(['modelSet', m]) },
    },
  })
  await fakeGateway((method: string, params: Record<string, unknown>) => {
    if (switching) return new Promise(() => {})
    calls.push([method, params])
    /* `?? {}` so a call the case did not name -- the refresh a write kicks
       off behind `void` -- answers an empty envelope rather than crashing
       in a promise nobody is holding. */
    if (answers) return Promise.resolve(answers[method] ?? {})
    return new Promise((res, rej) => pending.push({ method, params, res, rej }))
  })
  const model = await import('../model/source')
  const settingsModule = await import('../settings/source')
  const { generation } = await import('../../state/session/generation')
  const registry = await import('../../state/session/registry')
  const runtime = await import('../../state/session/runtime')
  const { staging } = await import('../../state/session/staging')
  /* The staged picks are the conversation's own now: the draft holds them and
     a switch to another draft drops them with it, so the object the sources
     read is whichever one is on screen. */
  const overrides = {
    get staged() { return staging() },
    startDraft: () => registry.switchToDraft(),
    applyStagedModel: (sessionId: string, gen: number) =>
      runtime.applyStagedModel(registry.viewRuntime(), sessionId, gen),
  }
  /* Everything the two sources read off the page. `modelSet` is the island's
     verb, faked above; the chip painter is the page's. */
  model.setChipPainter(() => {})
  const settings = {
    setupState: model.setupState,
    openModelsForMissingProvider: model.openModelsForMissingProvider,
    loadProviders: model.loadProviders,
    persistModel: model.persistModel,
    loadPermMode: settingsModule.loadPermMode,
    loadSettings: settingsModule.loadSettings,
    settingsSnapshot: settingsModule.settingsSnapshot,
    HIDDEN_PROVIDERS: model.HIDDEN_PROVIDERS,
    get providersLive() { return model.providers() },
    get defaultModelLive() { return model.defaultModel() },
    get defaultProviderLive() { return model.defaultProvider() },
  }
  const tick = () => new Promise((r) => setTimeout(r, 0))
  return {
    settings,
    overrides,
    calls,
    /* A switch to the new-task screen, which is one of the two paths that
       spends a generation ticket -- the real counter, not a stand-in. */
    bump: () => {
      switching = true
      try { overrides.startDraft() } finally { switching = false }
      return generation()
    },
    gen: () => generation(),
    inFlight: () => pending.map((p) => p.method),
    param: (i: number) => pending[i]!.params,
    settle: (i: number, answer: unknown) => { pending[i]!.res(answer); return tick() },
    fail: (i: number, error: unknown) => { pending[i]!.rej(error); return tick() },
    modelsSet: () => calls.filter((c) => c[0] === 'modelSet').map((c) => c[1]),
  }
}

describe('the first-run provider guard', () => {
  it('opens Models when setup reports no configured provider', async () => {
    const h = await live()
    h.settings.setupState.providerConfigured = false
    expect(h.settings.openModelsForMissingProvider()).toBe(true)
    expect(h.calls.filter((c) => c[0] === 'openModels')).toHaveLength(1)
  })

  it('stops redirecting as soon as a provider refresh authenticates one', async () => {
    const h = await live({ session: 'sess-1' })
    h.settings.setupState.providerConfigured = false
    const read = h.settings.loadProviders()
    await h.settle(0, { providers: [{ slug: 'anthropic', name: 'Anthropic', authenticated: true, models: [] }] })
    await read

    expect(h.settings.openModelsForMissingProvider()).toBe(false)
    expect(h.calls.filter((c) => c[0] === 'openModels')).toEqual([])
  })

  it('does not redirect while first-run status is still unknown', async () => {
    const h = await live()
    expect(h.settings.openModelsForMissingProvider()).toBe(false)
    expect(h.calls.filter((c) => c[0] === 'openModels')).toEqual([])
  })
})

describe('the live model refresh', () => {
  it('drops a superseded refresh even when its response lands last', async () => {
    const h = await live()
    h.bump()
    h.settings.loadProviders('a', h.gen())
    h.bump()
    h.settings.loadProviders('b', h.gen())

    await h.settle(1, { model: 'model-b', providers: [] })
    await h.settle(0, { model: 'model-a', providers: [] })

    expect(h.modelsSet()).toEqual(['model-b'])
  })

  it('commits a refresh whose generation is still current', async () => {
    const h = await live()
    h.bump()
    h.settings.loadProviders('a', h.gen())
    await h.settle(0, { model: 'model-a', providers: [] })
    expect(h.calls).toContainEqual(['modelSet', 'model-a'])
  })

  it('takes its own ticket when the caller brought none, so a late answer is still dropped', async () => {
    /* Three call sites do not pass a generation (the settings load, the
       provider-op refresh, and any future one). Capturing at entry is what
       makes them safe by construction instead of by each caller remembering. */
    const h = await live()
    h.settings.loadProviders('a')
    h.bump()
    await h.settle(0, { model: 'model-a', providers: [] })
    expect(h.modelsSet()).toEqual([])
  })

  it('commits a ticketless refresh when nothing moved under it', async () => {
    const h = await live()
    h.settings.loadProviders('a')
    await h.settle(0, { model: 'model-a', providers: [] })
    expect(h.calls).toContainEqual(['modelSet', 'model-a'])
  })

  it('omits session_id when there is no session, rather than serializing null', async () => {
    const h = await live()
    h.settings.loadProviders(null)
    expect(h.param(0)).toEqual({})
    h.settings.loadProviders('a')
    expect(h.param(1)).toEqual({ session_id: 'a' })
  })
})

describe('the live model persist', () => {
  it('a default write carries the visible session and repaints a chip that follows the default', async () => {
    const h = await live({ session: 'sess-1', answers: { 'config.set': { applied: true, applies_to_session: true } } })
    await h.settings.persistModel('m2', 'minimax', 'default')
    expect(h.calls[0]).toEqual(['config.set', { key: 'model', value: 'm2', provider: 'minimax', scope: 'default', session_id: 'sess-1' }])
    expect(h.calls).toContainEqual(['model.options', { session_id: 'sess-1' }])
    expect([h.settings.defaultModelLive, h.settings.defaultProviderLive]).toEqual(['m2', 'minimax'])
  })

  it('a default write moves a visible draft chip, which follows the default like an unswitched session', async () => {
    const h = await live({ answers: { 'config.set': { applied: true } } })
    await h.settings.persistModel('m2', 'minimax', 'default')
    expect(h.calls).toContainEqual(['modelSet', 'm2'])
  })

  it('a default write leaves a draft that staged its own pick alone', async () => {
    const h = await live({ answers: { 'config.set': { applied: true } } })
    h.overrides.staged.model = { model: 'other', provider: 'anthropic' }
    await h.settings.persistModel('m2', 'minimax', 'default')
    expect(h.modelsSet()).toEqual([])
  })

  it('a default write leaves a session with its own binding alone', async () => {
    const h = await live({ session: 'sess-1', answers: { 'config.set': { applied: true, applies_to_session: false } } })
    await h.settings.persistModel('m2', 'minimax', 'default')
    expect(h.calls.filter((c) => c[0] === 'model.options')).toEqual([])
    expect([h.settings.defaultModelLive, h.settings.defaultProviderLive]).toEqual(['m2', 'minimax'])
  })

  it('a refused default write moves neither half of the stored default pair', async () => {
    const h = await live({ session: 'sess-1' })
    /* Caught as it is made, not after the refusal: a rejection left unheld
       while the test drives the clock is an unhandled rejection. */
    const write = h.settings.persistModel('m2', 'minimax', 'default')
      .then(() => null, (e) => e)
    await h.fail(0, new Error('boom'))
    expect((await write)?.message).toBe('boom')
    expect([h.settings.defaultModelLive, h.settings.defaultProviderLive]).toEqual(['', ''])
  })

  /* The fourth path, added with the migration: a session pick WITH a session
     writes under it and nothing else -- no default pair moves, no refresh. */
  it('a session pick writes under that session and moves no default', async () => {
    const h = await live({ session: 'sess-1', answers: { 'config.set': { applied: true } } })
    await h.settings.persistModel('m2', 'minimax', 'session')
    expect(h.calls).toEqual([['config.set', { key: 'model', value: 'm2', provider: 'minimax', session_id: 'sess-1' }]])
    expect([h.settings.defaultModelLive, h.settings.defaultProviderLive]).toEqual(['', ''])
  })

  it('a session pick with no session stages and says so', async () => {
    const h = await live()
    const out = await h.settings.persistModel('m2', 'minimax', 'session')
    expect(out).toBe('staged')
    expect(h.overrides.staged.model).toEqual({ model: 'm2', provider: 'minimax' })
    expect(h.calls).toEqual([])
  })
})

describe('the live settings snapshot', () => {
  it('pairs the default model with the default provider, not the visible session', async () => {
    const h = await live({
      session: 'sess-1',
      answers: {
        'settings.get': { settings: { agents: { defaults: { model: 'default-b', provider: 'openrouter' } } } },
        'model.options': { model: 'session-a', provider: 'anthropic', providers: [] },
      },
    })
    await h.settings.loadSettings()
    const snap = h.settings.settingsSnapshot()
    expect(snap.model).toBe('default-b')
    expect(snap.curProvider).toBe('openrouter')
  })
})

describe('providers the page does not offer', () => {
  const row = (slug: string) => ({ slug, name: slug, authenticated: false, models: [] })

  async function listed(providers: unknown[]) {
    const h = await live({ session: 'sess-1', answers: { 'model.options': { model: '', provider: '', providers } } })
    await h.settings.loadProviders()
    return h.settings.providersLive
  }

  it('keeps the generic endpoint rows out of the rail and every other row in', async () => {
    const rows = await listed([row('anthropic'), row('custom'), row('hosted_vllm'), row('ollama_chat')])

    expect(rows.map((p) => p.id)).toEqual(['anthropic', 'ollama_chat'])
  })

  it('hides exactly the two it declares, so the list cannot drift from the page', async () => {
    /* Read off the part rather than restated: a copy here would agree with
       itself while the page shipped something else. */
    const h = await live()
    expect([...h.settings.HIDDEN_PROVIDERS]).toEqual(['hosted_vllm', 'custom'])
  })

  it('leaves a hidden provider configured and routable, only unlisted', async () => {
    /* The row goes; the section does not. `model.options` still reports it --
       which is what keeps an existing `custom` deployment serving -- and the
       page simply does not draw it. */
    const rows = await listed([{ ...row('custom'), authenticated: true, models: ['custom/local-7b'] }])

    expect(rows).toEqual([])
  })
})

describe('the follows-default repaint under navigation', () => {
  it('drops the repaint when the reader left during the write', async () => {
    const h = await live({ session: 'a' })
    const write = h.settings.persistModel('m2', 'minimax', 'default')
    h.bump()
    await h.settle(0, { applied: true, applies_to_session: true })
    await write
    expect(h.inFlight()).toEqual(['config.set', 'model.options'])
    await h.settle(1, { model: 'model-a', providers: [] })
    expect(h.modelsSet()).toEqual([])
  })

  it('drops a draft repaint when the reader opened a conversation during the write', async () => {
    /* The draft branch runs after the same await, so it needs the same ticket:
       the picker has closed, nothing locks the write, and opening a conversation
       advances the generation. Without the check the resolved draft write
       repaints a chip that has since been loaded for that conversation. */
    const h = await live()
    const write = h.settings.persistModel('m2', 'minimax', 'default')
    h.bump()
    await h.settle(0, { applied: true })
    await write
    expect(h.modelsSet()).toEqual([])
  })

  it('commits a draft repaint when the reader stayed on the draft', async () => {
    const h = await live()
    const write = h.settings.persistModel('m2', 'minimax', 'default')
    await h.settle(0, { applied: true })
    await write
    expect(h.calls).toContainEqual(['modelSet', 'm2'])
  })

  it('commits the repaint when the reader stayed', async () => {
    const h = await live({ session: 'a' })
    const write = h.settings.persistModel('m2', 'minimax', 'default')
    await h.settle(0, { applied: true, applies_to_session: true })
    await write
    await h.settle(1, { model: 'm2', providers: [] })
    expect(h.calls).toContainEqual(['modelSet', 'm2'])
  })
})

describe('the staged draft-write recovery', () => {
  /* The real applyStagedModel driving the real loadProviders over one shared
   * viewGen, with the config.set response deferred: the recovery refresh reports
   * on the session the send began under, so leaving it must drop the answer. */
  async function staged() {
    const h = await live({ session: 'a' })
    h.overrides.staged.model = { model: 'm2', provider: 'minimax' }
    return h
  }

  it('drops its refusal refresh when the reader opened another conversation', async () => {
    /* Order is the whole point: the reader leaves while `config.set` is still
       pending, so by the time the refusal fires the generation has ALREADY
       moved. A ticket taken when the refresh starts would read as current and
       repaint B; only the generation captured when the send began is right. */
    const h = await staged()
    const applied = h.overrides.applyStagedModel('a', h.gen())
    h.bump()
    await h.fail(0, { data: { detail: 'credential gone' } })
    await applied
    expect(h.inFlight()).toEqual(['config.set', 'model.options'])
    await h.settle(1, { model: 'model-a', providers: [] })
    expect(h.modelsSet()).toEqual([])
    expect(h.calls.some((c) => c[0] === 'toast')).toBe(true)
  })

  it('reconciles the chip when the reader stayed on that conversation', async () => {
    const h = await staged()
    const applied = h.overrides.applyStagedModel('a', h.gen())
    await h.fail(0, { data: { detail: 'credential gone' } })
    await applied
    await h.settle(1, { model: 'model-a', providers: [] })
    expect(h.calls).toContainEqual(['modelSet', 'model-a'])
  })

  it('says nothing and refreshes nothing when the write lands', async () => {
    const h = await staged()
    const applied = h.overrides.applyStagedModel('a', h.gen())
    await h.settle(0, { applied: true })
    await applied
    expect(h.calls.filter((c) => c[0] === 'toast' || c[0] === 'modelSet')).toEqual([])
    expect(h.inFlight()).toEqual(['config.set'])
  })
})

/* The permission chip's refresh runs the same race as the model's: two opens in
   a row, the first answer landing last. Same ticket, same outcome -- the chip
   shows the conversation the reader is in, which is the one the gate runs. */
describe('the live permission-mode refresh', () => {
  it('drops a superseded refresh even when its response lands last', async () => {
    const h = await live()
    h.bump()
    h.settings.loadPermMode('a', h.gen())
    h.bump()
    h.settings.loadPermMode('b', h.gen())

    await h.settle(1, { config: { 'permissions.mode': 'full' } })
    await h.settle(0, { config: { 'permissions.mode': 'ask' } })

    expect(h.calls.filter((c) => c[0] === 'setPermMode').map((c) => c[1])).toEqual(['full'])
    expect(h.param(0)).toEqual({ keys: ['permissions.mode'], session_id: 'a' })
  })

  it('reads the default for a draft and takes its own ticket when the caller brought none', async () => {
    const h = await live()
    h.bump()
    h.settings.loadPermMode(null)
    expect(h.param(0)).toEqual({ keys: ['permissions.mode'] })
    await h.settle(0, { config: {} })
    expect(h.calls.filter((c) => c[0] === 'setPermMode').map((c) => c[1])).toEqual(['ask'])
  })
})
