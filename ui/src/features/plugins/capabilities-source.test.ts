// @vitest-environment happy-dom
/* The concat adapters around the capabilities islands: the page opens before
 * its source refreshes, and live extension rows never borrow fixture storage.
 */

// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'

import { beforeEach, describe, expect, it, vi } from 'vitest'

const demoSource = readFileSync('src/demo/120-capabilities.js', 'utf8')
const chromeSource = readFileSync('src/demo/150-chrome.js', 'utf8')
const fixturePluginsFile = readFileSync('src/demo/153-plugins.js', 'utf8')
const liveSource = readFileSync('src/live/090-extensions.js', 'utf8')
const liveSkillsSource = readFileSync('src/live/140-skills.js', 'utf8')
const livePluginsSource = readFileSync('src/live/150-plugins.js', 'utf8')

const demoOpen = demoSource.slice(
  demoSource.indexOf('DS.capabilities ??='),
  demoSource.indexOf('const openSkills'),
)
const manualStart = chromeSource.indexOf("$('#mAdd').onclick")
const manualAdd = chromeSource.slice(manualStart, chromeSource.indexOf('\n};', manualStart) + 3)
const fixtureStart = fixturePluginsFile.indexOf('DS.plugins ??=')
const fixturePluginsSource = fixturePluginsFile.slice(
  fixtureStart,
  fixturePluginsFile.indexOf('\n})();', fixtureStart) + 6,
)

interface CapabilitiesSource {
  loaded(): boolean
  load(): Promise<boolean>
}

function opener(source: CapabilitiesSource): {
  calls: string[]
  open(tab: string): Promise<void>
  toast: ReturnType<typeof vi.fn>
} {
  const calls: string[] = []
  const toast = vi.fn()
  const skeleton = document.createElement('div')
  skeleton.id = 'skeleton'
  const build = new Function(
    'DS', 'extSet', 'showPage', 'drawCaps', 'drawCapsBadge', '$', 'RavenIslands', 'toast',
    `${demoOpen}\nreturn openCaps;`,
  ) as (...args: unknown[]) => (tab: string) => Promise<void>
  const open = build(
    { capabilities: source },
    (tab: string) => calls.push(`tab:${tab}`),
    (page: string) => calls.push(`page:${page}`),
    () => calls.push('draw'),
    () => calls.push('badge'),
    (selector: string) => document.querySelector(selector),
    { skills: { skeleton } },
    toast,
  )
  return { calls, open, toast }
}

beforeEach(() => {
  document.body.innerHTML = '<div id="capsBody"><i id="old"></i></div>'
})

describe('the capabilities page source adapter', () => {
  it('draws the fixture page once without showing a loading skeleton', async () => {
    const load = vi.fn(async () => false)
    const h = opener({ loaded: () => true, load })
    await h.open('skill')
    expect(h.calls).toEqual(['tab:skill', 'page:capsPage', 'draw', 'badge'])
    expect(document.getElementById('skeleton')).toBeNull()
    expect(load).toHaveBeenCalledOnce()
  })

  it('shows the page and a skeleton before the first live refresh', async () => {
    let finish!: (value: boolean) => void
    const h = opener({
      loaded: () => false,
      load: () => new Promise((resolve) => { finish = resolve }),
    })
    const opened = h.open('plugin')
    expect(h.calls).toEqual(['tab:plugin', 'page:capsPage'])
    expect(document.querySelector('#capsBody > #skeleton')).not.toBeNull()
    finish(true)
    await opened
    expect(h.calls).toEqual(['tab:plugin', 'page:capsPage', 'draw', 'badge'])
  })

  it('leaves the skeleton for a real empty state when refresh fails', async () => {
    const h = opener({
      loaded: () => false,
      load: async () => { throw new Error('offline') },
    })
    await h.open('plugin')
    expect(h.toast).toHaveBeenCalledWith('加载失败：offline')
    expect(h.calls).toEqual(['tab:plugin', 'page:capsPage', 'draw', 'badge'])
  })
})

describe('manual plugin add', () => {
  beforeEach(() => {
    document.body.innerHTML = '<input id="mName"><input id="mAddr"><button id="mAdd"></button>'
  })

  it('waits for the source before clearing and redrawing the live form', async () => {
    let finish!: () => void
    const manual = vi.fn(() => new Promise<void>((resolve) => { finish = resolve }))
    const drawCaps = vi.fn()
    const drawCapsBadge = vi.fn()
    const toast = vi.fn()
    const build = new Function(
      '$', 'DS', 'toast', 'T', 'drawCaps', 'drawCapsBadge',
      `${manualAdd}\nreturn $('#mAdd').onclick;`,
    ) as (...args: unknown[]) => () => Promise<void>
    const click = build(
      (selector: string) => document.querySelector(selector),
      { plugins: { manual } },
      toast,
      (key: string, args?: Record<string, string>) => args?.err || args?.name || key,
      drawCaps,
      drawCapsBadge,
    )
    const name = document.querySelector<HTMLInputElement>('#mName')!
    const address = document.querySelector<HTMLInputElement>('#mAddr')!
    name.value = 'CRM'
    address.value = 'npx -y @acme/crm-mcp'

    const pending = click()
    expect(manual).toHaveBeenCalledWith('CRM', 'npx -y @acme/crm-mcp')
    expect(name.value).toBe('CRM')
    expect(drawCaps).not.toHaveBeenCalled()
    finish()
    await pending
    expect(name.value).toBe('')
    expect(address.value).toBe('')
    expect(drawCaps).toHaveBeenCalledOnce()
    expect(drawCapsBadge).toHaveBeenCalledOnce()
  })

  it('keeps fixture additions inside the fixture source', async () => {
    const DS: Record<string, unknown> = {}
    const build = new Function('DS', 'PLUGINS', `${fixturePluginsSource}\nreturn DS.plugins;`) as (
      ds: Record<string, unknown>, plugins: unknown[],
    ) => { manual(name: string, address: string): Promise<void>; rows(): Array<{ name: string }> }
    const plugins = build(DS, [])

    await plugins.manual('CRM', 'https://mcp.example.test')

    expect(plugins.rows().map((row) => row.name)).toEqual(['CRM'])
  })
})

describe('the live extension source', () => {
  it('owns its installed rows and reports when the first load finishes', async () => {
    const DS: Record<string, unknown> = {}
    const ext = {
      tools: [{ name: 'read_file', description: 'read', mcp_server: false }],
      skills: [{ name: 'review', source: 'local', description: 'review' }],
      plugins: [{ id: 'python', display_name: 'Python', version: '1', enabled: true }],
      mcp: [{ name: 'remote', transport: 'http', enabled: true, state: 'connected', tool_count: 2 }],
    }
    const rpc = {
      notify: {} as Record<string, (value: unknown) => void>,
      call: vi.fn(async (method: string, params?: Record<string, unknown>) => {
        if (method === 'settings.get') {
          return { settings: { tools: { disabledTools: [] }, plugins: { disabled: [] } } }
        }
        if (method === 'raven.mcp.list') {
          return { servers: [{ name: 'remote', url: 'https://remote.test', connected: true }] }
        }
        if (method === 'raven.mcp.set') {
          const servers = params?.servers as Array<{ name: string }>
          ext.mcp.push({ name: servers.at(-1)!.name, transport: 'stdio', enabled: true,
            state: 'connected', tool_count: 0 })
          return { ok: true }
        }
        return ext
      }),
    }
    const build = new Function(
      'T', 'rpc', 'toast', 'pmToggle', 'DS', 'LANG', 'showUpNote', 'setMemFault', 'RavenIslands',
      `${liveSource}\n${liveSkillsSource}\n${livePluginsSource}\nreturn {
        tools: () => toolsLive,
      };`,
    ) as (...args: unknown[]) => {
      tools(): Array<{ id: string }>
    }
    const rows = build(
      (key: string, _args?: unknown, fallback?: string) => fallback || key,
      rpc,
      vi.fn(),
      vi.fn(),
      DS,
      'en',
      vi.fn(),
      vi.fn(),
      { plugins: { event: vi.fn() } },
    )
    const capabilities = DS.capabilities as CapabilitiesSource
    const skills = DS.skills as { installed(): Array<{ name: string }> }
    const plugins = DS.plugins as {
      manual(name: string, address: string): Promise<void>
      rows(): Array<{ name: string }>
    }

    expect(capabilities.loaded()).toBe(false)
    await expect(capabilities.load()).resolves.toBe(true)
    expect(capabilities.loaded()).toBe(true)
    expect(skills.installed().map((row) => row.name)).toEqual(['review'])
    expect(plugins.rows().map((row) => row.name)).toEqual(['Python', 'remote'])
    expect(rows.tools().map((row) => row.id)).toEqual(['read_file'])

    await plugins.manual('CRM', 'npx -y @acme/crm-mcp')
    expect(rpc.call).toHaveBeenCalledWith('raven.mcp.set', {
      servers: [
        { name: 'remote', url: 'https://remote.test', connected: true },
        { name: 'CRM', address: 'npx -y @acme/crm-mcp' },
      ],
    })
    expect(plugins.rows().map((row) => row.name)).toEqual(['Python', 'remote', 'CRM'])
  })
})
