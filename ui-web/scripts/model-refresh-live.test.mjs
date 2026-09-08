/* The per-conversation model refresh, extracted from the live layer and driven
 * with deferred responses. model.options does its catalogue work off-thread, so
 * a refresh for a conversation the reader has left can land after the one they
 * moved to; the viewGen guard is what keeps the late answer from repainting the
 * page. A synchronous stub cannot exercise that, so this runs the real function.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const src = readFileSync(new URL('../src/live/120-settings.js', import.meta.url), 'utf8')
const match = src.match(/async function loadProviders[\s\S]*?\n}/)
if (!match) throw new Error('loadProviders is absent from the live layer')
const loadProvidersSrc = match[0]
const permMatch = src.match(/async function loadPermMode[\s\S]*?\n}/)
if (!permMatch) throw new Error('loadPermMode is absent from the live layer')
const loadPermModeSrc = permMatch[0]

function harness() {
  const calls = []
  const pending = []
  const build = Function(
    'deps',
    `let viewGen = 0;
     let providersLive = [], curProvider = '';
     const { rpc, sessionCurrent, modelSet, setModelLabel } = deps;
     ${loadProvidersSrc}
     return {
       loadProviders,
       bump: () => { viewGen += 1; return viewGen; },
       curProvider: () => curProvider,
     };`,
  )
  const api = build({
    rpc: { call: (method, params) => new Promise((res) => pending.push({ params, res })) },
    sessionCurrent: () => null,
    modelSet: (m) => calls.push(['modelSet', m]),
    setModelLabel: () => {},
  })
  return {
    ...api,
    calls,
    param: (i) => pending[i].params,
    settle: (i, answer) => {
      pending[i].res(answer)
      return new Promise((r) => setTimeout(r, 0))
    },
  }
}

describe('the live model refresh', () => {
  it('drops a superseded refresh even when its response lands last', async () => {
    const h = harness()
    const gA = h.bump()
    h.loadProviders('a', gA)
    const gB = h.bump()
    h.loadProviders('b', gB)

    await h.settle(1, { model: 'model-b', providers: [] })
    await h.settle(0, { model: 'model-a', providers: [] })

    expect(h.calls.filter((c) => c[0] === 'modelSet').map((c) => c[1])).toEqual(['model-b'])
  })

  it('commits a refresh whose generation is still current', async () => {
    const h = harness()
    const g = h.bump()
    h.loadProviders('a', g)
    await h.settle(0, { model: 'model-a', providers: [] })
    expect(h.calls).toContainEqual(['modelSet', 'model-a'])
  })

  it('takes its own ticket when the caller brought none, so a late answer is still dropped', async () => {
    /* Three call sites do not pass a generation (the settings load, the
       provider-op refresh, and any future one). Capturing at entry is what
       makes them safe by construction instead of by each caller remembering. */
    const h = harness()
    h.loadProviders('a')
    h.bump()
    await h.settle(0, { model: 'model-a', providers: [] })
    expect(h.calls.filter((c) => c[0] === 'modelSet')).toEqual([])
  })

  it('commits a ticketless refresh when nothing moved under it', async () => {
    const h = harness()
    h.loadProviders('a')
    await h.settle(0, { model: 'model-a', providers: [] })
    expect(h.calls).toContainEqual(['modelSet', 'model-a'])
  })

  it('omits session_id when there is no session, rather than serializing null', async () => {
    const h = harness()
    h.loadProviders(null)
    expect(h.param(0)).toEqual({})
    h.loadProviders('a')
    expect(h.param(1)).toEqual({ session_id: 'a' })
  })
})

const persistSrc = src.match(/async function persistModel[\s\S]*?\n}/)
if (!persistSrc) throw new Error('persistModel is absent from the live layer')

function persistHarness({ session = 'sess-1', answer = {}, reject = null } = {}) {
  const calls = []
  const build = Function(
    'deps',
    `let defaultModelLive = '', defaultProviderLive = '', pendingModel = null, viewGen = 0;
     const { rpc, sessionCurrent, loadProviders, modelSet, setModelLabel } = deps;
     ${persistSrc[0]}
     return {
       persistModel,
       defaults: () => ({ model: defaultModelLive, provider: defaultProviderLive }),
       pending: () => pendingModel,
       stage: (v) => { pendingModel = v; },
     };`,
  )
  const api = build({
    rpc: { call: (method, params) => {
      calls.push([method, params])
      return reject ? Promise.reject(reject) : Promise.resolve(answer)
    } },
    sessionCurrent: () => session,
    loadProviders: (sid, gen) => calls.push(['loadProviders', sid, gen]),
    modelSet: (m) => calls.push(['modelSet', m]),
    setModelLabel: () => {},
  })
  return { ...api, calls }
}

describe('the live model persist', () => {
  it('a default write carries the visible session and repaints a chip that follows the default', async () => {
    const h = persistHarness({ answer: { applied: true, applies_to_session: true } })
    await h.persistModel('m2', 'minimax', 'default')
    expect(h.calls[0]).toEqual(['config.set', { key: 'model', value: 'm2', provider: 'minimax', scope: 'default', session_id: 'sess-1' }])
    expect(h.calls).toContainEqual(['loadProviders', 'sess-1', 0])
    expect(h.defaults()).toEqual({ model: 'm2', provider: 'minimax' })
  })

  it('a default write moves a visible draft chip, which follows the default like an unswitched session', async () => {
    const h = persistHarness({ session: null, answer: { applied: true } })
    await h.persistModel('m2', 'minimax', 'default')
    expect(h.calls).toContainEqual(['modelSet', 'm2'])
  })

  it('a default write leaves a draft that staged its own pick alone', async () => {
    const h = persistHarness({ session: null, answer: { applied: true } })
    h.stage({ model: 'other', provider: 'anthropic' })
    await h.persistModel('m2', 'minimax', 'default')
    expect(h.calls.filter((c) => c[0] === 'modelSet')).toEqual([])
  })

  it('a default write leaves a session with its own binding alone', async () => {
    const h = persistHarness({ answer: { applied: true, applies_to_session: false } })
    await h.persistModel('m2', 'minimax', 'default')
    expect(h.calls.filter((c) => c[0] === 'loadProviders')).toEqual([])
    expect(h.defaults()).toEqual({ model: 'm2', provider: 'minimax' })
  })

  it('a refused default write moves neither half of the stored default pair', async () => {
    const h = persistHarness({ reject: new Error('boom') })
    await expect(h.persistModel('m2', 'minimax', 'default')).rejects.toThrow('boom')
    expect(h.defaults()).toEqual({ model: '', provider: '' })
  })

  it('a session pick with no session stages and says so', async () => {
    const h = persistHarness({ session: null })
    const out = await h.persistModel('m2', 'minimax', 'session')
    expect(out).toBe('staged')
    expect(h.pending()).toEqual({ model: 'm2', provider: 'minimax' })
    expect(h.calls).toEqual([])
  })
})

const settingsSrc = src.match(/async function loadSettings[\s\S]*?\n}/)
const snapshotSrc = src.match(/const settingsSnapshot = \(\) => \(\{[\s\S]*?\}\);/)
if (!settingsSrc || !snapshotSrc) throw new Error('loadSettings/settingsSnapshot are absent from the live layer')

function settingsHarness() {
  const build = Function(
    'deps',
    `let RAW = {}, configPathLive = '', everosLive = null, toolsLive = [], viewGen = 0;
     let providersLive = [], defaultModelLive = '', defaultProviderLive = '';
     const TOOL_GROUPS = [];
     const { rpc, sessionCurrent, modelSet, setModelLabel, drawBanner } = deps;
     ${loadProvidersSrc}
     ${settingsSrc[0]}
     ${snapshotSrc[0]}
     return { loadSettings, settingsSnapshot };`,
  )
  return build({
    rpc: { call: (method) => Promise.resolve(
      method === 'settings.get'
        ? { settings: { agents: { defaults: { model: 'default-b', provider: 'openrouter' } } } }
        : { model: 'session-a', provider: 'anthropic', providers: [] },
    ) },
    sessionCurrent: () => 'sess-1',
    modelSet: () => {},
    setModelLabel: () => {},
    drawBanner: () => {},
  })
}

describe('the live settings snapshot', () => {
  it('pairs the default model with the default provider, not the visible session', async () => {
    const h = settingsHarness()
    await h.loadSettings()
    const snap = h.settingsSnapshot()
    expect(snap.model).toBe('default-b')
    expect(snap.curProvider).toBe('openrouter')
  })
})

function combinedHarness({ session = 'a' } = {}) {
  /* The real persistModel driving the real loadProviders, one shared viewGen,
   * every response deferred: the shapes a synchronous recorder cannot fail on. */
  const calls = []
  const pending = []
  const build = Function(
    'deps',
    `let viewGen = 0, providersLive = [], defaultModelLive = '', defaultProviderLive = '', pendingModel = null;
     const { rpc, sessionCurrent, modelSet, setModelLabel } = deps;
     ${loadProvidersSrc}
     ${persistSrc[0]}
     return { persistModel, bump: () => { viewGen += 1; } };`,
  )
  const api = build({
    rpc: { call: (method, params) => new Promise((res) => pending.push({ method, params, res })) },
    sessionCurrent: () => session,
    modelSet: (m) => calls.push(['modelSet', m]),
    setModelLabel: () => {},
  })
  return {
    ...api,
    calls,
    inFlight: () => pending.map((p) => p.method),
    settle: (i, answer) => { pending[i].res(answer); return new Promise((r) => setTimeout(r, 0)) },
  }
}

describe('the follows-default repaint under navigation', () => {
  it('drops the repaint when the reader left during the write', async () => {
    const h = combinedHarness()
    const write = h.persistModel('m2', 'minimax', 'default')
    h.bump()
    await h.settle(0, { applied: true, applies_to_session: true })
    await write
    expect(h.inFlight()).toEqual(['config.set', 'model.options'])
    await h.settle(1, { model: 'model-a', providers: [] })
    expect(h.calls.filter((c) => c[0] === 'modelSet')).toEqual([])
  })

  it('drops a draft repaint when the reader opened a conversation during the write', async () => {
    /* The draft branch runs after the same await, so it needs the same ticket:
       the picker has closed, nothing locks the write, and opening a conversation
       advances the generation. Without the check the resolved draft write
       repaints a chip that has since been loaded for that conversation. */
    const h = combinedHarness({ session: null })
    const write = h.persistModel('m2', 'minimax', 'default')
    h.bump()
    await h.settle(0, { applied: true })
    await write
    expect(h.calls.filter((c) => c[0] === 'modelSet')).toEqual([])
  })

  it('commits a draft repaint when the reader stayed on the draft', async () => {
    const h = combinedHarness({ session: null })
    const write = h.persistModel('m2', 'minimax', 'default')
    await h.settle(0, { applied: true })
    await write
    expect(h.calls).toContainEqual(['modelSet', 'm2'])
  })

  it('commits the repaint when the reader stayed', async () => {
    const h = combinedHarness()
    const write = h.persistModel('m2', 'minimax', 'default')
    await h.settle(0, { applied: true, applies_to_session: true })
    await write
    await h.settle(1, { model: 'm2', providers: [] })
    expect(h.calls).toContainEqual(['modelSet', 'm2'])
  })
})

const stagedSrc = readFileSync(new URL('../src/live/080-overrides.js', import.meta.url), 'utf8')
  .match(/async function applyStagedModel[\s\S]*?\n}/)
if (!stagedSrc) throw new Error('applyStagedModel is absent from the live layer')

function stagedHarness({ reject = null } = {}) {
  /* The real applyStagedModel driving the real loadProviders over one shared
   * viewGen, with the config.set response deferred: the recovery refresh reports
   * on the session the send began under, so leaving it must drop the answer. */
  const calls = []
  const pending = []
  const build = Function(
    'deps',
    `let viewGen = 0, providersLive = [], pendingModel = { model: 'm2', provider: 'minimax' };
     const { rpc, sessionCurrent, modelSet, setModelLabel, toast } = deps;
     ${loadProvidersSrc}
     ${stagedSrc[0]}
     return { applyStagedModel, bump: () => { viewGen += 1 } };`,
  )
  const api = build({
    rpc: { call: (method, params) => new Promise((res, rej) => pending.push({ method, params, res, rej })) },
    sessionCurrent: () => 'a',
    modelSet: (m) => calls.push(['modelSet', m]),
    setModelLabel: () => {},
    toast: (t) => calls.push(['toast', t]),
  })
  return {
    ...api,
    calls,
    inFlight: () => pending.map((p) => p.method),
    settle: (i, answer) => { pending[i].res(answer); return new Promise((r) => setTimeout(r, 0)) },
    fail: (i, err) => { pending[i].rej(err); return new Promise((r) => setTimeout(r, 0)) },
  }
}

describe('the staged draft-write recovery', () => {
  it('drops its refusal refresh when the reader opened another conversation', async () => {
    /* Order is the whole point: the reader leaves while `config.set` is still
       pending, so by the time the refusal fires the generation has ALREADY
       moved. A ticket taken when the refresh starts would read as current and
       repaint B; only the generation captured when the send began is right. */
    const h = stagedHarness()
    const applied = h.applyStagedModel('a', 0)
    h.bump()
    await h.fail(0, { data: { detail: 'credential gone' } })
    await applied
    expect(h.inFlight()).toEqual(['config.set', 'model.options'])
    await h.settle(1, { model: 'model-a', providers: [] })
    expect(h.calls.filter((c) => c[0] === 'modelSet')).toEqual([])
    expect(h.calls.some((c) => c[0] === 'toast')).toBe(true)
  })

  it('reconciles the chip when the reader stayed on that conversation', async () => {
    const h = stagedHarness()
    const applied = h.applyStagedModel('a', 0)
    await h.fail(0, { data: { detail: 'credential gone' } })
    await applied
    await h.settle(1, { model: 'model-a', providers: [] })
    expect(h.calls).toContainEqual(['modelSet', 'model-a'])
  })

  it('says nothing and refreshes nothing when the write lands', async () => {
    const h = stagedHarness()
    const applied = h.applyStagedModel('a', 0)
    await h.settle(0, { applied: true })
    await applied
    expect(h.calls).toEqual([])
    expect(h.inFlight()).toEqual(['config.set'])
  })
})

/* The permission chip's refresh runs the same race as the model's: two opens in
   a row, the first answer landing last. Same ticket, same outcome -- the chip
   shows the conversation the reader is in, which is the one the gate runs. */
function permHarness() {
  const calls = []
  const pending = []
  const build = Function(
    'deps',
    `let viewGen = 0;
     const { rpc, window } = deps;
     ${loadPermModeSrc}
     return { loadPermMode, bump: () => { viewGen += 1; return viewGen; } };`,
  )
  const api = build({
    rpc: { call: (method, params) => new Promise((res) => pending.push({ params, res })) },
    window: { setPermMode: (m) => calls.push(m) },
  })
  return {
    ...api,
    calls,
    param: (i) => pending[i].params,
    settle: (i, answer) => {
      pending[i].res(answer)
      return new Promise((r) => setTimeout(r, 0))
    },
  }
}

describe('the live permission-mode refresh', () => {
  it('drops a superseded refresh even when its response lands last', async () => {
    const h = permHarness()
    h.loadPermMode('a', h.bump())
    h.loadPermMode('b', h.bump())

    await h.settle(1, { config: { 'permissions.mode': 'full' } })
    await h.settle(0, { config: { 'permissions.mode': 'ask' } })

    expect(h.calls).toEqual(['full'])
    expect(h.param(0)).toEqual({ keys: ['permissions.mode'], session_id: 'a' })
  })

  it('reads the default for a draft and takes its own ticket when the caller brought none', async () => {
    const h = permHarness()
    h.bump()
    h.loadPermMode(null)
    expect(h.param(0)).toEqual({ keys: ['permissions.mode'] })
    await h.settle(0, { config: {} })
    expect(h.calls).toEqual(['ask'])
  })
})
