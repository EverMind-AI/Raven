// @vitest-environment happy-dom
/* The legacy adapters around the capabilities islands: the page opens before
 * its source refreshes, and live extension rows never borrow fixture storage.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/legacy-part.mjs'

import type { CapabilitiesSource } from '../../state/sources'

/* Narrower than the real PluginsSource: the two verbs the capabilities page
   reaches for, so a fake here does not have to answer the whole island's. */
interface PluginsSource {
  manual(name: string, address: string): Promise<void>
  rows(): Array<{ name: string }>
}

/* The three domains the parts install, as this test drives them. `skills` is
   narrower than SkillsSource: the only verb the capabilities page reads off it
   is the installed list, and naming the whole interface would mean faking
   every other verb to say anything about that one. */
interface Seam {
  capabilities: CapabilitiesSource
  skills: { installed(): Array<{ name: string }> }
  plugins: PluginsSource
}

async function seam(): Promise<Seam> {
  const { sources } = await import('../../state/sources')
  return sources as unknown as Seam
}

/* `openCaps` with the two verbs it drives registered as decorators -- which is
   the seam the skill and plugin layers already wrap them through -- so the
   order it calls them in is readable without the pages being built.
   `drawCapsBadge` is not in the list: it is a call inside the same part, and it
   has no body, so nothing outside can see it. `draw` stands immediately before
   it at all three sites. */
async function opener(source: CapabilitiesSource): Promise<{
  calls: string[]
  open(tab: string): Promise<void>
  toast: ReturnType<typeof vi.fn>
}> {
  const calls: string[] = []
  const toast = vi.fn()
  const skeleton = document.createElement('div')
  skeleton.id = 'skeleton'
  const part = await loadPart(() => import('../../legacy/demo/120-capabilities.js'), {
    fakes: {
      'demo/010-kernel.js': { $: looseQuery() },
      'demo/152-skills.js': { drawCaps: () => calls.push('draw') },
    },
    globals: {
      RavenIslands: { skills: { skeleton } },
      toast,
    },
  })
  ;(await seam()).capabilities = source
  part.decorateExtSet(() => (tab: string) => calls.push(`tab:${tab}`))
  part.decorateShowPage(() => (page: string) => calls.push(`page:${page}`))
  return { calls, open: part.openCaps, toast }
}

beforeEach(() => {
  document.body.innerHTML = '<div id="capsBody"><i id="old"></i></div>'
})

describe('the capabilities page source adapter', () => {
  it('draws the fixture page once without showing a loading skeleton', async () => {
    const load = vi.fn(async () => false)
    const h = await opener({ loaded: () => true, load })
    await h.open('skill')
    expect(h.calls).toEqual(['tab:skill', 'page:capsPage', 'draw'])
    expect(document.getElementById('skeleton')).toBeNull()
    expect(load).toHaveBeenCalledOnce()
  })

  it('shows the page and a skeleton before the first live refresh', async () => {
    let finish!: (value: boolean) => void
    const h = await opener({
      loaded: () => false,
      load: () => new Promise((resolve) => { finish = resolve }),
    })
    const opened = h.open('plugin')
    expect(h.calls).toEqual(['tab:plugin', 'page:capsPage'])
    expect(document.querySelector('#capsBody > #skeleton')).not.toBeNull()
    finish(true)
    await opened
    expect(h.calls).toEqual(['tab:plugin', 'page:capsPage', 'draw'])
  })

  it('leaves the skeleton for a real empty state when refresh fails', async () => {
    const h = await opener({
      loaded: () => false,
      load: async () => { throw new Error('offline') },
    })
    await h.open('plugin')
    expect(h.toast).toHaveBeenCalledWith('加载失败：offline')
    expect(h.calls).toEqual(['tab:plugin', 'page:capsPage', 'draw'])
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
    const part = await loadPart(() => import('../../legacy/demo/150-chrome.js'), {
      fakes: {
        'demo/010-kernel.js': {
          $: looseQuery(),
          T: (key: string, args?: Record<string, string>) => args?.err || args?.name || key,
        },
        'demo/120-capabilities.js': { drawCapsBadge },
        'demo/152-skills.js': { drawCaps },
      },
      globals: { toast, RavenIslands: {} },
    })
    ;(await seam()).plugins = { manual } as unknown as PluginsSource
    part.install()
    const name = document.querySelector<HTMLInputElement>('#mName')!
    const address = document.querySelector<HTMLInputElement>('#mAddr')!
    name.value = 'CRM'
    address.value = 'npx -y @acme/crm-mcp'

    /* The handler the part hung on the button, called the way a click calls
       it -- it reads the two fields itself and takes no argument. */
    const click = document.querySelector<HTMLButtonElement>('#mAdd')!
      .onclick as unknown as () => Promise<void>
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
    const part = await loadPart(() => import('../../legacy/demo/153-plugins.js'), {
      fakes: { 'demo/010-kernel.js': { $: looseQuery() } },
      globals: { RavenIslands: { plugins: { event: vi.fn() } }, toast: vi.fn() },
    })
    part.install()
    const { plugins } = await seam()

    const before = plugins.rows().map((row) => row.name)
    await plugins.manual('CRM', 'https://mcp.example.test')

    expect(plugins.rows().map((row) => row.name)).toEqual([...before, 'CRM'])
  })
})

describe('the live extension source', () => {
  it('owns its installed rows and reports when the first load finishes', async () => {
    const ext = {
      tools: [{ name: 'read_file', description: 'read', mcp_server: false }],
      skills: [{ name: 'review', source: 'local', description: 'review' }],
      plugins: [{ id: 'python', display_name: 'Python', version: '1', enabled: true }],
      mcp: [{ name: 'remote', transport: 'http', enabled: true, state: 'connected', tool_count: 2 }],
    }
    const extPart = await loadPart(() => import('../../legacy/live/090-extensions.js'), {
      globals: {
        RavenIslands: { plugins: { event: vi.fn() } },
        setMemFault: vi.fn(),
        toast: vi.fn(),
      },
    })
    const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
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
    })
    await fakeGateway(call)
    const skillsPart = await import('../../legacy/live/140-skills.js')
    const pluginsPart = await import('../../legacy/live/150-plugins.js')
    extPart.install()
    skillsPart.install()
    pluginsPart.install()

    const { capabilities, skills, plugins } = await seam()

    expect(capabilities.loaded()).toBe(false)
    await expect(capabilities.load()).resolves.toBe(true)
    expect(capabilities.loaded()).toBe(true)
    expect(skills.installed().map((row) => row.name)).toEqual(['review'])
    expect(plugins.rows().map((row) => row.name)).toEqual(['Python', 'remote'])
    expect(extPart.toolsLive.map((row: { id: string }) => row.id)).toEqual(['read_file'])

    await plugins.manual('CRM', 'npx -y @acme/crm-mcp')
    expect(call).toHaveBeenCalledWith('raven.mcp.set', {
      servers: [
        { name: 'remote', url: 'https://remote.test', connected: true },
        { name: 'CRM', address: 'npx -y @acme/crm-mcp' },
      ],
    })
    expect(plugins.rows().map((row) => row.name)).toEqual(['Python', 'remote', 'CRM'])
  })
})
