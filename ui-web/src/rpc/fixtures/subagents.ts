/* Every agent this install can dispatch to, and the instances one conversation
 * is talking to directly.
 *
 * Two tables, and they answer two different pages. `subagents.list` is the
 * external-agents roster (the extAgents island) and is a plain inventory; the
 * instance calls below it are the desk canvas's, and they are reached only through
 * `?desk-demo=1` -- which is why they are exported as an override group rather
 * than as part of the offline library. That entrance has always been applied
 * on the live page, and it still is: the group goes on whichever transport the
 * page chose.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'
import type { PushMethod } from '../notifications'
import type { Overrides } from '../overrideTransport'

type Row = ResultOf<'subagents.list'>['rows'][number]
type InstanceRow = ResultOf<'subagents.instances'>['instances'][number]
type DirectTurn = ResultOf<'subagents.instance.history'>['turns'][number]
type ModeReply = ResultOf<'subagents.instance.set_mode'>
type Rung = NonNullable<ModeReply['availableModes']>[number]

/* The roster rows. A row is (name, kind, configured, enabled, probe_status,
   last test). Three facts, deliberately kept apart: `configured` is "Raven
   knows about it", `enabled` is "Raven may dispatch to it", and `probe_status`
   is "the machine can actually run it". Collapsing them is how a disabled
   agent reads as broken, or a missing binary reads as switched off. */
const ROSTER: Row[] = [
  /* `vendored` is not decoration: it is what tells the page that connecting this
     row means running the product's installer, not writing a config entry from a
     preset it does not have. A fixture missing it read as a preset nobody could
     add. */
  { name: 'Raven-Research', kind: 'cli', configured: false, builtin: false, vendored: true,
    enabled: true, group: 'uninstalled', probe_status: 'ready', probe_detail: '', has_api_key: false, needs_auth: false,
    mcps: [], allow_mcp_secrets: false, test_running: false, last_test_detail: '',
    own: true, model_source: 'fixed', model_choices: [],
    description: 'A product row, discovered under agents/ rather than written into config.' },
  { name: 'Raven-PPT', kind: 'cli', configured: false, builtin: false, vendored: true,
    enabled: false, group: 'uninstalled', probe_status: 'missing',
    probe_detail: 'the ppt-engine engine wheel is not installed', has_api_key: false, needs_auth: false,
    mcps: [], allow_mcp_secrets: false, test_running: false, last_test_detail: '',
    own: true, model_source: 'fixed', model_choices: [],
    description: 'A product row whose engine wheel is missing, so it is listed and disabled.' },
  { name: 'raven', kind: 'builtin', configured: false, builtin: true, enabled: true,
    group: 'builtin', probe_status: 'ready', probe_detail: '', has_api_key: false, needs_auth: false,
    mcps: [], allow_mcp_secrets: false, test_running: false, last_test_detail: '',
    own: true, model_source: 'raven', model_choices: [],
    description: 'General-purpose sub-agent with no capability bias.' },
  /* An acp row with a menu, so the offline page has a picker to open: the
     choices are the shape the adapter advertises, not a claim about its list. */
  { name: 'claude_code', preset: 'claude_code', kind: 'acp', configured: true, enabled: true,
    group: 'installed', probe_status: 'ready', probe_detail: '', has_api_key: false, needs_auth: false,
    mcps: [], allow_mcp_secrets: false, test_running: false, last_test_ok: true, last_test_detail: '',
    own: false, model_source: 'agent',
    model_choices: [
      { value: 'claude-opus-5', name: 'Opus 5', group: 'Anthropic' },
      { value: 'claude-sonnet-5', name: 'Sonnet 5', group: 'Anthropic' },
      { value: 'claude-haiku-4-5', name: 'Haiku 4.5', group: 'Anthropic' },
    ],
    description: 'Claude Code CLI - strong general coding / agent tasks.' },
  { name: 'codex', preset: 'codex', kind: 'cli', configured: false, enabled: false,
    group: 'uninstalled', probe_status: 'missing', probe_detail: 'codex: command not found',
    has_api_key: false, needs_auth: false, mcps: [], allow_mcp_secrets: false, test_running: false,
    own: false, model_source: 'fixed', model_choices: [],
    last_test_detail: '', description: 'OpenAI Codex CLI - coding tasks.' },
  { name: 'hermes', preset: 'hermes', kind: 'cli', configured: false, enabled: false,
    group: 'uninstalled', probe_status: 'ready', probe_detail: '', has_api_key: false, needs_auth: false,
    mcps: [], allow_mcp_secrets: false, test_running: false, last_test_ok: false,
    last_test_detail: 'connected, but no session could be opened: Internal error: Hermes is not connected to any AI provider yet. Run `hermes model` to pick one.',
    last_test_remedy: { kind: 'setup', command: 'hermes model' },
    own: false, model_source: 'fixed', model_choices: [],
    description: 'Hermes Agent CLI - general assistant with tool calling.' },
  { name: 'mirothinker', preset: 'mirothinker', kind: 'openai', configured: false, enabled: false,
    group: 'uninstalled', probe_status: 'unknown', probe_detail: '', has_api_key: false, needs_auth: false,
    mcps: [], allow_mcp_secrets: false, test_running: false, last_test_detail: '',
    own: false, model: 'mirothinker-1-7-deepresearch', model_source: 'fixed', model_choices: [],
    description: 'MiroMind deep-research (OpenAI-compatible HTTP).' },
]

export interface SubagentsFixture {
  fixtures: Fixtures
}

export function createSubagents(env: FixtureEnv): SubagentsFixture {
  /* Mutated in place so the page is still explorable with no gateway behind
     it, and the same rows answered every time, which is what makes those edits
     stick across a redraw. */
  const rows = ROSTER.map((r) => (r.last_test_ok ? { ...r, last_test_at_ms: env.now() - 3600e3 } : { ...r }))
  const find = (name: string): Row | undefined => rows.find((r) => r.name === name)

  return {
    fixtures: {
      'subagents.list': () => ({ rows: rows.map((r) => ({ ...r })) }),
      'subagents.add': (p) => {
        const row = find(p.name || p.preset || '')
        if (row) {
          row.configured = true
          row.enabled = row.kind !== 'openai' || row.has_api_key
          if (p.api_key) { row.has_api_key = true; row.enabled = true }
        }
        return { added: true, name: p.name || '' }
      },
      'subagents.update': (p) => {
        const row = find(p.name)
        if (row && p.api_key) { row.has_api_key = true; row.enabled = true }
        if (row && typeof p.description === 'string') row.description = p.description
        /* The row's own model, kept the way the server keeps it: a clear wins
           over a pick sent beside it, and the built-in row's pick is stored
           naming its provider (the server spells it in; here it is prefixed
           unless the id already carries it). */
        if (row && p.clear_model) row.model = null
        else if (row && p.model) row.model = p.provider && !p.model.startsWith(`${p.provider}/`) ? `${p.provider}/${p.model}` : p.model
        return { updated: true, name: p.name }
      },
      /* One switch for every kind of row, including the discovered ones: the
         server materializes a registry entry for those and puts the flag on it. */
      'subagents.toggle': (p) => {
        const row = find(p.name)
        if (row) row.enabled = !!p.enabled
        return { enabled: !!p.enabled }
      },
      'subagents.remove': (p) => {
        const row = find(p.name)
        if (row) { row.configured = false; row.enabled = false }
        return { removed: true }
      },
      /* Instant here, minutes in life: the real call returns as soon as the
         download starts and the row carries `building` until it lands. The
         fixture shows the outcome rather than a spinner nothing would ever
         clear -- there is no second answer to poll for. */
      'subagents.build': (p) => {
        const row = find(p.name)
        if (row) { row.building = false; row.probe_status = 'ready'; row.probe_detail = ''; row.enabled = true }
        return { building: false, detail: 'nothing to build behind this page' }
      },
      /* Instant too, and it passes: with no gateway there is no agent to
         dispatch, so the answer is the shape of a verdict rather than a failure
         the reader would go looking for the cause of. */
      'subagents.test': (p) => {
        const row = find(p.name)
        if (row) { row.last_test_ok = true; row.last_test_at_ms = env.now(); row.last_test_detail = '' }
        return { ok: true, detail: '', elapsed_ms: 0 }
      },
      'subagents.test_cancel': (p) => {
        const row = find(p.name)
        if (row) row.test_running = false
        return { cancelled: true }
      },
      'subagents.probe': () => ({ rows: rows.map((r) => ({ ...r })) }),
      /* No delegated work and no instances: a conversation replayed with no
         server behind it has none, and the canvas never invented any -- which
         is what `?desk-demo=1` below is for. */
      'subagents.instances': () => ({ instances: [], pending_handoff_count: 0 }),
      'subagents.instance.history': () => ({ turns: [] }),
      'subagent.list': () => ({ items: [] }),
    },
  }
}

/* ---- the ?desk-demo=1 canvas ------------------------------------------- */

/**
 * Seven instances, their histories and their rungs, as an override group.
 *
 * `roster` is asked of the transport underneath rather than of this group, for
 * the reason the block it came from asked the live source: the canvas's agent
 * names are mapped onto whatever agents the install really has, so the panes
 * are headed by names the reader recognises.
 */
export function deskDemoOverrides(
  clock: () => number,
  schedule: (ms: number, fn: () => void) => void,
  roster: () => Promise<string[]>,
  push: (method: PushMethod, params: unknown) => void,
): Overrides {
  /* The subscription each conversation's frames ride, read off the answer the
     transport underneath gave: a direct turn's events are pushed on it, which
     is what the lane does on a live page (see state/session/stages.ts). */
  const subs = new Map<string, string>()
  const notify = (sessionKey: string, target: unknown, type: string, payload: object): void => {
    const subscription = subs.get(sessionKey)
    if (!subscription) return
    push('event', { subscription_id: subscription, event: { type, payload: { ...payload, target } } })
  }
  const now = clock()
  const instances: InstanceRow[] = [
    { sessionKey: 'desk-demo', agent: 'research-raven', handle: 'market-map-a19f', kind: 'playbook', status: 'running', runId: '市场调研', nodeId: '竞品功能调研', createdAtMs: now - 420000, updatedAtMs: now, turnStartedAtMs: now - 96000, resumable: true },
    { sessionKey: 'desk-demo', agent: 'research-raven', handle: 'model-permissions-7c2a', kind: 'playbook', status: 'completed', runId: 'UI 能力核验', nodeId: '整理权限模型差异', createdAtMs: now - 830000, updatedAtMs: now - 220000, resumable: true },
    { sessionKey: 'desk-demo', agent: 'coding-raven', handle: 'instance-sync-coder12', kind: 'dag', status: 'running', runId: '子智能体列表改造', nodeId: '修复实例状态同步', createdAtMs: now - 190000, updatedAtMs: now, turnStartedAtMs: now - 23000, resumable: true },
    { sessionKey: 'desk-demo', agent: 'coding-raven', handle: 'history-render-9d0e', kind: 'playbook', status: 'failed', runId: 'UI 回归', nodeId: '验证历史消息渲染', createdAtMs: now - 620000, updatedAtMs: now - 480000, resumable: true },
    { sessionKey: 'desk-demo', agent: 'coding-raven', handle: 'legacy-build-31ab', kind: 'spawn', status: 'completed', nodeId: '旧版构建迁移', createdAtMs: now - 940000, updatedAtMs: now - 720000, resumable: false },
    { sessionKey: 'desk-demo', agent: 'content-raven', handle: 'release-notes-18ca', kind: 'dag', status: 'completed', runId: '发布流程', nodeId: '整理发布说明', createdAtMs: now - 380000, updatedAtMs: now - 140000, resumable: true },
    { sessionKey: 'desk-demo', agent: 'review-raven', handle: 'interaction-coverage-612e', kind: 'playbook', status: 'idle', runId: '交互验收', nodeId: '检查交互状态覆盖', createdAtMs: now - 50000, updatedAtMs: now - 50000, resumable: true },
  ]
  const history = new Map<string, DirectTurn[]>(instances.map((row): [string, DirectTurn[]] => [row.handle, [
    { call_id: `${row.handle}-u`, role: 'user', content: `请执行「${row.nodeId}」，完成后给出可验证的结果。`, at_ms: row.createdAtMs! },
    { call_id: `${row.handle}-a`, role: 'assistant', content: row.status === 'failed'
      ? '检查过程中发现历史记录的消息结构与渲染器不一致，需要修正后重试。'
      : '已读取任务上下文并完成第一轮处理，正在整理关键结果。', at_ms: row.updatedAtMs!, live: row.status === 'running' },
  ]]));
  /* Each pane's own rungs and its own override, so the canvas can show the chip
   in all three states it has -- following the conversation, set apart from it,
   and absent because the agent advertises nothing to choose between.

   Keyed by handle, not by agent, because `bindDemoAgents` below rewrites
   `row.agent` onto whatever the roster really answers. And two vocabularies on
   purpose: one spelling the tier ladder, which is what every shipped agent
   does today, and one that does not, which is the case the control exists for
   and the one whose label lengths are worth looking at. The third has an empty
   menu, and `InstanceMode` draws no chip for it -- an agent with nothing to
   choose between, which reads differently from a pane whose mode failed to
   load and should be seen next to the others.

   `inherited` is the conversation's tier clamped to the agent's rungs. Here
   that is the fixture's own arithmetic rather than the clamp's: `DEMO_INHERITS`
   answers what `clamp_tier` would for these two vocabularies, so an Auto chip
   names a rung the menu actually holds. */
  const LADDER: string[] = ['medium', 'high', 'max'];
  const DEMO_RUNGS: Record<string, Rung[]> = {
    'market-map-a19f': LADDER.map((id) => ({ id, name: id.charAt(0).toUpperCase() + id.slice(1) })),
    'model-permissions-7c2a': LADDER.map((id) => ({ id, name: id.charAt(0).toUpperCase() + id.slice(1) })),
    'instance-sync-coder12': [
      { id: 'quick', name: 'Quick' }, { id: 'thorough', name: 'Thorough' },
    ],
    'history-render-9d0e': [
      { id: 'quick', name: 'Quick' }, { id: 'thorough', name: 'Thorough' },
    ],
    'release-notes-18ca': LADDER.map((id) => ({ id, name: id.charAt(0).toUpperCase() + id.slice(1) })),
    'interaction-coverage-612e': [],
  }
  const override = new Map<string, string>([['model-permissions-7c2a', 'max']]);
  let tier = 'high';
  /* What a dispatch would run at with no override: the conversation's tier if
   this agent ranks it, and otherwise nothing, so the row says the agent's own
   default instead of naming a rung it cannot run. */
  const inherits = (handle: string): string | null =>
    ((DEMO_RUNGS[handle] || []).some((m) => m.id === tier) ? tier : null);
  const modeReply = (handle: string): ModeReply => ({
    mode: override.get(handle) || null,
    inherited: inherits(handle),
    availableModes: (DEMO_RUNGS[handle] || []).slice(),
  });
  const setMode = (handle: string, mode: string | null): ModeReply => {
    if (mode === null) override.delete(handle);
    else if (!(DEMO_RUNGS[handle] || []).some((m) => m.id === mode)) {
      /* The manager refuses a rung the agent does not advertise and names what
       it does offer; the chip says so and stays where it was. The canvas
       cannot show that unless the fixture refuses too. */
      throw { data: { detail: `no mode ${mode}; this agent offers ${(DEMO_RUNGS[handle] || []).map((m) => m.id).join(', ')}` } };
    } else override.set(handle, mode);
    return modeReply(handle);
  }
  let bound: Promise<void> | null = null
  const bindAgents = (): Promise<void> => {
    if (bound) return bound
    bound = roster().then((names) => {
      if (!names.length) return
      const groups = [...new Set(instances.map((row) => row.agent))]
      const mapped = new Map(groups.map((name, index) => [name, names[index % names.length]!]))
      instances.forEach((row) => { row.agent = mapped.get(row.agent) || row.agent })
    }).catch(() => {})
    return bound
  }

  const rowFor = (agent: string, handle: string): InstanceRow | undefined =>
    instances.find((item) => item.agent === agent && item.handle === handle)

  return {
    'subagents.instances': async () => {
      await bindAgents()
      return { instances: instances.slice(), pending_handoff_count: 0 }
    },
    'subagents.instance.history': (p) => ({ turns: (history.get(p.handle) || []).slice() }),
    'subagents.instance.forget': (p) => {
      const at = instances.findIndex((row) => row.agent === p.agent && row.handle === p.handle)
      if (at >= 0) instances.splice(at, 1)
      return { removed: true }
    },
    /* Report, set and clear on one method, which is the shape the control
       reads: no `mode` and no `clear` is the report. */
    'subagents.instance.set_mode': (p) => {
      const params = p as { handle: string; mode?: string; clear?: boolean }
      if (params.clear) return setMode(params.handle, null)
      if (params.mode !== undefined) return setMode(params.handle, params.mode)
      return modeReply(params.handle)
    },
    /* No model choice on the canvas: the panes are about the mode chip, and an
       agent that advertises no models is a state the control already draws. */
    'subagents.instance.set_model': () => ({ model: null, availableModels: [] }),
    /* The tier chip and these panes are one system: under Auto a pane is
       showing the conversation's tier, so a switch has to move it here too.
       Answered by the transport underneath -- the rung really is the
       conversation's to change -- and only remembered here. */
    'session.set_mode': async (_p, next) => {
      const r = await next()
      if (r && r.mode) tier = r.mode
      return r
    },
    /* A turn addressed to one instance. The canvas answers it the way the
       block this came from did: the row goes running, the two turns land in
       its history, and the pane is told through the same direct-event door a
       delegated frame arrives by. */
    'turn.send': (p, next) => {
      const target = (p as { target?: { agent: string; handle: string } }).target
      if (!target) return next()
      const row = rowFor(target.agent, target.handle)
      if (!row) throw new Error('Instance not found')
      const text = p.content || ''
      row.status = 'running'; row.updatedAtMs = clock()
      const turns = history.get(target.handle) || []
      turns.push({ call_id: `${target.handle}-${clock()}-u`, role: 'user', content: text, at_ms: clock() })
      history.set(target.handle, turns)
      notify(p.session_key, target, 'message.start', { content: text })
      schedule(1200, () => {
        turns.push({ call_id: `${target.handle}-${clock()}-a`, role: 'assistant', content: `已收到并完成：${text}`, at_ms: clock() })
        row.status = 'completed'; row.updatedAtMs = clock()
        notify(p.session_key, target, 'message.complete', {})
      })
      return { turn_id: `direct-${clock()}`, accepted: true, naming: false }
    },
    /* Not answered here, only watched: the page opens one subscription per
       conversation and every frame names it, so the direct turn above has to
       push on the one this conversation really got. */
    'turn.subscribe': async (p, next) => {
      const r = await next()
      if (r && r.subscription_id) subs.set(p.session_key, r.subscription_id)
      return r
    },
  }
}
