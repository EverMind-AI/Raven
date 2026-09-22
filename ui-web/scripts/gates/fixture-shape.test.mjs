// @vitest-environment happy-dom
/* Every offline responder answers the shape the contract declares.
 *
 * The page is one piece of code in both modes, so a field the contract requires
 * and a fixture omits renders as `undefined` on the offline page while every
 * unit test stays green -- the component tests build their own objects, and the
 * live gates next door only watch the rpc layer. This walks the library the
 * offline page really serves and holds every answer to `rpc-schema/openrpc.json`
 * itself.
 *
 * TypeScript already holds the responders to the generated result types, which
 * is the same contract read at compile time. What this adds is the run: a
 * responder that builds its answer behind a cast, or narrows a union the wrong
 * way, is only caught by calling it.
 */

import { readFileSync } from 'node:fs'
/* Off cwd, not off `import.meta.url`: under happy-dom that is an http URL. */
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { PARAMS } from '../fixture-params.mjs'

const contract = JSON.parse(readFileSync(resolve(process.cwd(), '../rpc-schema/openrpc.json'), 'utf8'))
const schemas = contract.components.schemas
const resultOf = new Map(contract.methods.map((m) => [m.name, m.result.schema]))

const deref = (schema) => {
  let at = schema
  while (at && at.$ref) {
    const name = at.$ref.split('/').pop()
    at = schemas[name]
    if (!at) throw new Error(`${schema.$ref} is absent from the contract`)
  }
  return at || {}
}

const kinds = {
  string: (v) => typeof v === 'string',
  integer: (v) => typeof v === 'number' && Number.isInteger(v),
  number: (v) => typeof v === 'number',
  boolean: (v) => typeof v === 'boolean',
  object: (v) => !!v && typeof v === 'object' && !Array.isArray(v),
  array: (v) => Array.isArray(v),
  null: (v) => v === null,
}

/* A walk rather than a validator off the shelf: the four keywords the contract
   uses are type, required, properties and items, plus anyOf and enum. Adding a
   dependency for that would be the larger change. */
function check(schema, value, path, out) {
  const s = deref(schema)
  if (s.anyOf || s.oneOf) {
    const branches = s.anyOf || s.oneOf
    const ok = branches.some((b) => {
      const errs = []
      check(b, value, path, errs)
      return !errs.length
    })
    if (!ok) out.push(`${path}: matches none of ${branches.length} alternatives`)
    return
  }
  const types = s.type === undefined ? [] : [].concat(s.type)
  if (types.length && !types.some((t) => (kinds[t] || (() => true))(value))) {
    out.push(`${path}: expected ${types.join('|')}, got ${Array.isArray(value) ? 'array' : typeof value}`)
    return
  }
  if (s.enum && !s.enum.includes(value)) out.push(`${path}: ${JSON.stringify(value)} is not one of ${s.enum.join(', ')}`)
  if (types.includes('object') || s.properties || s.required) {
    if (!kinds.object(value)) return
    for (const key of s.required || []) {
      if (value[key] === undefined) out.push(`${path}.${key}: required by the contract, absent from the answer`)
    }
    for (const [key, sub] of Object.entries(s.properties || {})) {
      /* `null` on an optional field is the wire saying nothing, which several
         of the contract's own descriptions spell out (a playbook node's
         `skills` is three-state: null said nothing, [] said none). The
         generated types render those as optional rather than nullable, so a
         null here is checked as an absence rather than against the type. */
      if (value[key] === null && !(s.required || []).includes(key)) continue
      if (value[key] !== undefined) check(sub, value[key], `${path}.${key}`, out)
    }
  }
  if (types.includes('array') && Array.isArray(value) && s.items) {
    value.forEach((item, i) => check(s.items, item, `${path}[${i}]`, out))
  }
}

/* The library, on a clock and a timer of this gate's own: a scheduled emission
   is dropped rather than run, so a scripted turn cannot push frames into a
   page that is not there. */
async function library() {
  const { demoFixtures } = await import('../../src/rpc/fixtures/index.ts')
  const { FixtureTransport } = await import('../../src/rpc/fixtureTransport.ts')
  const transport = new FixtureTransport(demoFixtures, { now: () => 1789000000000, timer: () => {} })
  return transport
}

/* Which optional properties the contract declares, and which of them an answer
   actually carries. Optional is where the two halves of the contract stop
   agreeing: tsc holds a responder to the required fields and says nothing about
   an optional one nobody ever sends, so the offline page draws the branch that
   has the field and the live page draws the branch that does not -- or the
   other way round, which is how the demo ends up being the only page anyone
   ever sees a row on.

   A null counts as not sent, for the same reason the walk above skips it: on an
   optional field null is the wire saying nothing. */
function optionals(schema, value, path, declared, sent) {
  const s = deref(schema)
  if (s.anyOf || s.oneOf) {
    const branch = (s.anyOf || s.oneOf).find((b) => { const errs = []; check(b, value, path, errs); return !errs.length })
    if (branch) optionals(branch, value, path, declared, sent)
    return
  }
  if (Array.isArray(value) && s.items) {
    for (const item of value) optionals(s.items, item, `${path}[]`, declared, sent)
    return
  }
  if (!s.properties || !kinds.object(value)) return
  const required = new Set(s.required || [])
  for (const [key, sub] of Object.entries(s.properties)) {
    const at = `${path}.${key}`
    if (!required.has(key)) declared.add(at)
    if (value[key] === undefined || value[key] === null) continue
    if (!required.has(key)) sent.add(at)
    optionals(sub, value[key], at, declared, sent)
  }
}

/* Every optional property the library never sends, as the tree stands, grouped
   by the method that would send it. Down or gone: a fixture that starts sending
   one takes its line off, and a line that cannot be measured any more (a
   contract field gone, a responder gone) comes off with it. A NEW entry is a
   responder that stopped sending a field, or a contract that grew one nobody
   answered -- both of which are the offline page quietly diverging from the
   live one, which is what this library exists to prevent.

   Reading the list: most of it is one state this canvas is deliberately in.
   Nine browser answers omit the same seven because no page is open on the
   offline canvas; four model answers omit the same nine, and model.options
   eight of them, because the provider table here is slugs and names rather
   than a full endpoint description; all three session answers omit the same
   five `info` fields, which are what a gateway says about ITSELF. The
   ones worth a second look are the fields a row on the page draws and this
   library has never exercised: `dag.get`'s node_summary and terminal_outputs,
   `session.resume`'s diff and notice, `subagents.list`'s stateful and
   upgrade_to, `ext.list`'s tools[].needs. */
const UNSENT = new Set([
  // browser.close: 7
  'browser.close.can_back', 'browser.close.can_forward', 'browser.close.error', 'browser.close.headful', 'browser.close.loading', 'browser.close.title', 'browser.close.url',
  // browser.frame: 8
  'browser.frame.can_back', 'browser.frame.can_forward', 'browser.frame.error', 'browser.frame.headful', 'browser.frame.jpeg', 'browser.frame.loading', 'browser.frame.title', 'browser.frame.url',
  // browser.input: 7
  'browser.input.can_back', 'browser.input.can_forward', 'browser.input.error', 'browser.input.headful', 'browser.input.loading', 'browser.input.title', 'browser.input.url',
  // browser.mode: 7
  'browser.mode.can_back', 'browser.mode.can_forward', 'browser.mode.error', 'browser.mode.headful', 'browser.mode.loading', 'browser.mode.title', 'browser.mode.url',
  // browser.open: 7
  'browser.open.can_back', 'browser.open.can_forward', 'browser.open.error', 'browser.open.headful', 'browser.open.loading', 'browser.open.title', 'browser.open.url',
  // browser.read: 8
  'browser.read.can_back', 'browser.read.can_forward', 'browser.read.console', 'browser.read.error', 'browser.read.headful', 'browser.read.loading', 'browser.read.title', 'browser.read.url',
  // browser.state: 7
  'browser.state.can_back', 'browser.state.can_forward', 'browser.state.error', 'browser.state.headful', 'browser.state.loading', 'browser.state.title', 'browser.state.url',
  // browser.tabs: 7
  'browser.tabs.can_back', 'browser.tabs.can_forward', 'browser.tabs.error', 'browser.tabs.headful', 'browser.tabs.loading', 'browser.tabs.title', 'browser.tabs.url',
  // browser.watch: 10
  'browser.watch.can_back', 'browser.watch.can_forward', 'browser.watch.error', 'browser.watch.headful', 'browser.watch.loading', 'browser.watch.title', 'browser.watch.url', 'browser.watch.vh', 'browser.watch.vw', 'browser.watch.watching',
  // channels.qr: 2
  'channels.qr.qr', 'channels.qr.qr_text',
  // config.set: 5 -- the model-switch group, which the offline library does not
  // model: its gateway always has a loop, so there is no restart to report.
  'config.set.applies_to_session', 'config.set.needs_restart', 'config.set.scope', 'config.set.session_id', 'config.set.value',
  // cron.list: 4
  'cron.list.jobs[].at_ms', 'cron.list.jobs[].every_ms', 'cron.list.jobs[].last_error', 'cron.list.jobs[].tz',
  // cron.save: 4
  'cron.save.job.at_ms', 'cron.save.job.every_ms', 'cron.save.job.last_error', 'cron.save.job.tz',
  // dag.get: 10
  'dag.get.run.files[].error', 'dag.get.run.files[].instance', 'dag.get.run.files[].node_summary', 'dag.get.run.files[].output_file', 'dag.get.run.files[].prompt_file', 'dag.get.run.summary.cancelled', 'dag.get.run.summary.failed', 'dag.get.run.summary.skipped', 'dag.get.run.task_summary', 'dag.get.run.terminal_outputs',
  // dag.node: 6
  'dag.node.node.error', 'dag.node.node.messages', 'dag.node.node.output', 'dag.node.node.output_file', 'dag.node.node.prompt', 'dag.node.node.prompt_file',
  // deliverables.list: 1
  'deliverables.list.files[].description',
  // ext.list: 3
  'ext.list.mcp[].auth_url', 'ext.list.tools[].mcp_server', 'ext.list.tools[].needs',
  // fs.open: 1
  'fs.open.app',
  // import.status: 4
  'import.status.current', 'import.status.phase', 'import.status.phases', 'import.status.tier',
  // knowledge.search: 2
  'knowledge.search.embed_ms', 'knowledge.search.search_ms',
  // knowledge.status: 1
  'knowledge.status.extensions',
  // model.add_model: 8
  'model.add_model.provider.accepts_api_key', 'model.add_model.provider.api_base', 'model.add_model.provider.default_api_base', 'model.add_model.provider.docs', 'model.add_model.provider.key_env', 'model.add_model.provider.platforms', 'model.add_model.provider.protocol_overrides', 'model.add_model.provider.protocols',
  // model.fetch_models: 7
  'model.fetch_models.error', 'model.fetch_models.models[].capabilities', 'model.fetch_models.models[].context_window', 'model.fetch_models.models[].description', 'model.fetch_models.models[].input_modalities', 'model.fetch_models.models[].output_modalities', 'model.fetch_models.models[].source',
  // model.options: 7
  'model.options.providers[].accepts_api_key', 'model.options.providers[].api_base', 'model.options.providers[].docs', 'model.options.providers[].key_env', 'model.options.providers[].platforms', 'model.options.providers[].protocol_overrides', 'model.options.providers[].protocols',
  // model.add_models: 8 -- the same provider row as model.options, plus the default address the row above states
  'model.add_models.provider.accepts_api_key', 'model.add_models.provider.api_base', 'model.add_models.provider.default_api_base', 'model.add_models.provider.docs', 'model.add_models.provider.key_env', 'model.add_models.provider.platforms', 'model.add_models.provider.protocol_overrides', 'model.add_models.provider.protocols',
  // model.remove_model: 8
  'model.remove_model.provider.accepts_api_key', 'model.remove_model.provider.api_base', 'model.remove_model.provider.default_api_base', 'model.remove_model.provider.docs', 'model.remove_model.provider.key_env', 'model.remove_model.provider.platforms', 'model.remove_model.provider.protocol_overrides', 'model.remove_model.provider.protocols',
  // model.save_key: 8
  'model.save_key.provider.accepts_api_key', 'model.save_key.provider.api_base', 'model.save_key.provider.default_api_base', 'model.save_key.provider.docs', 'model.save_key.provider.key_env', 'model.save_key.provider.platforms', 'model.save_key.provider.protocol_overrides', 'model.save_key.provider.protocols',
  // model.set_protocol: 8
  'model.set_protocol.provider.accepts_api_key', 'model.set_protocol.provider.api_base', 'model.set_protocol.provider.default_api_base', 'model.set_protocol.provider.docs', 'model.set_protocol.provider.key_env', 'model.set_protocol.provider.platforms', 'model.set_protocol.provider.protocol_overrides', 'model.set_protocol.provider.protocols',
  // plug.auth: 1
  'plug.auth.mcp',
  // plug.install: 2
  'plug.install.mcp.auth_url', 'plug.install.mcp.error',
  // plug.toggle: 1
  'plug.toggle.mcp.auth_url',
  // plug.configure / plug.retry / plug.revoke: 2 each -- the row's snapshot, which carries neither a consent URL nor an error here
  'plug.configure.mcp.auth_url', 'plug.configure.mcp.error',
  'plug.retry.mcp.auth_url', 'plug.retry.mcp.error',
  'plug.revoke.mcp.auth_url', 'plug.revoke.mcp.error',
  // skills.manage: 9 -- the gate asks for the list; inspect, open, search, browse and install answer the rest
  'skills.manage.info', 'skills.manage.installed', 'skills.manage.items', 'skills.manage.name', 'skills.manage.opened', 'skills.manage.page', 'skills.manage.results', 'skills.manage.total', 'skills.manage.total_pages',
  // session.compress: 10
  'session.compress.info.config_notices', 'session.compress.info.endpoint', 'session.compress.info.running_ms', 'session.compress.info.update_available', 'session.compress.info.update_command', 'session.compress.info.usage.context_estimated', 'session.compress.messages', 'session.compress.summary.note', 'session.compress.summary.token_line', 'session.compress.usage',
  // session.create: 6 -- nothing is ever in flight on this canvas, so the age of a running turn has nothing to report
  'session.create.info.config_notices', 'session.create.info.endpoint', 'session.create.info.running_ms', 'session.create.info.update_available', 'session.create.info.update_command', 'session.create.info.usage.context_estimated',
  // session.list: 1 -- no offline conversation is pinned to a folder; the chip that pins one is not on this tree yet
  'session.list.sessions[].workdir',
  // session.resume: 13
  'session.resume.info.config_notices', 'session.resume.info.endpoint', 'session.resume.info.running_ms', 'session.resume.info.update_available', 'session.resume.info.update_command', 'session.resume.info.usage.context_estimated', 'session.resume.messages[].context', 'session.resume.messages[].dag_run_id', 'session.resume.messages[].diff', 'session.resume.messages[].notice', 'session.resume.messages[].reasoning_ms', 'session.resume.messages[].spawn_task_id', 'session.resume.messages[].turn_ended',
  // settings.everos: 1
  'settings.everos.note',
  // settings.everosSet: 1 -- the gateway's re-index warning, which only a real embedding move raises
  'settings.everosSet.warning',
  // settings.set: 1 -- the gateway's reload-only warning, which the key the gate writes (a live one) never raises
  'settings.set.warning',
  // settings.usage: 3
  'settings.usage.session_key', 'settings.usage.session_titles', 'settings.usage.sessions',
  // subagents.list: 3
  'subagents.list.rows[].building', 'subagents.list.rows[].stateful', 'subagents.list.rows[].upgrade_to',
  // subagents.probe: 2
  'subagents.probe.rows[].stateful', 'subagents.probe.rows[].upgrade_to',
  // subagents.test: 2
  'subagents.test.cancelled', 'subagents.test.reply',
  // system.hello: 1
  'system.hello.platform',
])

describe('the offline fixture library', () => {
  it('answers every method the contract declares it for', async () => {
    const transport = await library()
    const failures = []
    for (const method of Object.keys(transport.fixtures)) {
      if (!resultOf.has(method)) { failures.push(`${method}: not a contract method`); continue }
      let answer
      try {
        answer = await transport.call(method, PARAMS[method] ?? {})
      } catch (e) {
        failures.push(`${method}: threw ${e instanceof Error ? e.message : String(e)}`)
        continue
      }
      const errs = []
      check(resultOf.get(method), answer, method, errs)
      failures.push(...errs)
    }
    expect(failures).toEqual([])
  })

  it('leaves no optional property unsent but the ones pinned', async () => {
    const transport = await library()
    const declared = new Set()
    const sent = new Set()
    const walk = async (method, params) => {
      let answer
      try { answer = await transport.call(method, params) } catch { return }
      optionals(resultOf.get(method), answer, method, declared, sent)
    }
    for (const method of Object.keys(transport.fixtures)) {
      if (resultOf.has(method)) await walk(method, PARAMS[method] ?? {})
    }
    /* The rows one call answers differently per argument, so a field sent for
       the second conversation counts as sent. The three `k` rows are the
       scheduled ones, and the only place the library sends `origin` and
       `delegated` -- the two entries a reader never types. */
    for (const session_id of ['a', 'b', 'g', 'k1', 'k2', 'k3']) await walk('session.resume', { session_id })
    const unsent = [...declared].filter((at) => !sent.has(at)).sort()
    expect(unsent.filter((at) => !UNSENT.has(at)), 'send the field, or add it to UNSENT under its method')
      .toEqual([])
    expect([...UNSENT].filter((at) => !unsent.includes(at)), 'the library sends these now, or they are gone: take them off UNSENT')
      .toEqual([])
  })

  /* The two canvases the URL asks for, held to the same contract: they are
     overrides on whichever transport the page chose, so a page reaching them on
     a LIVE socket gets these answers over real ones and a wrong shape there is
     a wrong shape in front of a working gateway. */
  it('answers every method its two canvases override', async () => {
    const base = await library()
    const { OverrideTransport } = await import('../../src/rpc/overrideTransport.ts')
    const { deskDemoOverrides, onboardDemoOverrides } = await import('../../src/rpc/fixtures/index.ts')
    const later = (ms, fn) => { fn() }
    const roster = () => Promise.resolve(['raven', 'claude_code'])
    /* What a direct turn pushes on the conversation's own subscription, which
       this gate is not about: the shape of an ANSWER is. */
    const pushed = []
    const groups = {
      'onboard=demo': onboardDemoOverrides(later),
      'desk-demo=1': deskDemoOverrides(() => 1789000000000, later, roster,
        (method, params) => pushed.push([method, params])),
    }
    /* The direct-chat door the canvas knocks on to tell a pane its instance
       answered reads the roster through the seam, and only a booted page has
       one installed. The shape of the answer is what this gate is about, so the
       seam gets the two verbs that door touches and nothing else. */
    const { sources } = await import('../../src/state/sources.ts')
    sources.subagents = { instances: async () => [], roster: async () => [] }
    const failures = []
    for (const [label, overrides] of Object.entries(groups)) {
      const transport = new OverrideTransport(base, overrides)
      /* Asked first, and the row it answers with is what the rest are aimed at:
         the canvas rewrites its instances' agent names onto the roster the
         install really has, so a hard-coded name here would miss. */
      const listed = overrides['subagents.instances']
        ? (await transport.call('subagents.instances', { session_key: 'a' })).instances[0]
        : null
      const at = listed ? { session_key: 'a', agent: listed.agent, handle: listed.handle } : {}
      /* Forget last: it takes the row the calls above are aimed at out of the
         canvas, and a walk that ran it first would be asking the rest about an
         instance it had just removed. */
      const walk = Object.keys(overrides)
        .sort((a, b) => Number(a.endsWith('forget')) - Number(b.endsWith('forget')))
      for (const method of walk) {
        const params = method.startsWith('subagents.instance.')
          ? { ...at, ...(method.endsWith('set_mode') ? { mode: 'max' } : {}) }
          : method === 'turn.send'
            ? { session_key: 'a', content: 'go on', target: { agent: at.agent, handle: at.handle } }
            : PARAMS[method] ?? {}
        let answer
        try {
          answer = await transport.call(method, params)
        } catch (e) {
          failures.push(`${label} ${method}: threw ${e instanceof Error ? e.message : JSON.stringify(e)}`)
          continue
        }
        check(resultOf.get(method), answer, `${label} ${method}`, failures)
      }
    }
    expect(failures).toEqual([])
  })

  /* The one answer the desk canvas gives that is not an answer: a turn
     addressed to an instance is reported by frames, the way a live gateway
     reports one, and the canvas used to reach into the island's store instead.
     Pushed on the subscription the conversation really opened, and every frame
     carries the target -- an untagged one is the main agent's and would be
     typed into the conversation as if raven had said it
     (src/state/session/stages.ts). */
  it('reports a direct turn as frames on the conversation own subscription', async () => {
    const base = await library()
    const { OverrideTransport } = await import('../../src/rpc/overrideTransport.ts')
    const { deskDemoOverrides } = await import('../../src/rpc/fixtures/index.ts')
    const due = []
    const later = (ms, fn) => { due.push([ms, fn]) }
    const roster = () => Promise.resolve(['raven'])
    let canvas = null
    canvas = new OverrideTransport(base, deskDemoOverrides(() => 1789000000000, later, roster,
      (method, params) => canvas.push(method, params)))
    const frames = []
    canvas.on('event', (params) => frames.push(params))

    const { instances } = await canvas.call('subagents.instances', { session_key: 'a' })
    const target = { agent: instances[0].agent, handle: instances[0].handle }
    const { subscription_id: subscription } = await canvas.call('turn.subscribe', { session_key: 'a' })
    await canvas.call('turn.send', { session_key: 'a', content: 'one turn', target })
    for (const [, fn] of due.splice(0)) fn()

    expect(frames.map((f) => [f.subscription_id, f.event.type, f.event.payload.target]))
      .toEqual([
        [subscription, 'message.start', target],
        [subscription, 'message.complete', target],
      ])
    expect(frames[0].event.payload.content).toBe('one turn')
  })

  /* Both forks of the research conversation, because which one plays depends on
     whether web search is configured and only one of them is ever reachable
     from a single page. */
  it('answers a resume for every scripted conversation', async () => {
    const transport = await library()
    const failures = []
    for (const id of ['a', 'b', 'g']) {
      const answer = await transport.call('session.resume', { session_id: id })
      check(resultOf.get('session.resume'), answer, `session.resume(${id})`, failures)
    }
    expect(failures).toEqual([])
  })
})
