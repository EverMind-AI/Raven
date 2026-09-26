// @vitest-environment happy-dom
/* The third thing the rail-foot row can say: the page is behind the sources it
 * was built from. Only a source checkout is ever in that state; the terminal is
 * told when the page is resolved, but `raven web` detaches that terminal, so
 * the row is where both launch paths show it -- through the header the page's
 * own HEAD probe of '/' reads. The row has to rise on that header, hold it
 * over a rebuild that is itself still behind, fall when a later look no longer
 * carries it, and explain how to rebuild rather than reload when clicked.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { loadPart } from '../../scripts/module-harness.mjs'
import { t } from '../i18n/t'

type Updates = typeof import('./updates')

function row(): HTMLButtonElement {
  document.body.innerHTML =
    '<button id="upnote" hidden><span class="pip"></span><span class="t"></span><span class="rl"></span></button>'
  return document.getElementById('upnote') as HTMLButtonElement
}

/* What the served page answers a HEAD probe with. */
function served(headers: Record<string, string>): Response {
  return { ok: true, headers: new Headers(headers) } as unknown as Response
}

/* The probe awaits the fetch and then the handlers; nothing here waits on a
   timer, so draining the microtask queue settles it. */
async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) await Promise.resolve()
}

async function watching(fakes: Record<string, Record<string, unknown>> = {}): Promise<Updates> {
  vi.stubEnv('PROD', true)
  vi.useFakeTimers()
  const mod = (await loadPart(() => import('./updates'), { fakes })) as Updates
  mod.watchForUpdates()
  await settle()
  return mod
}

describe('the behind-sources notice', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
  })

  it('rises on the header and names the rebuild, then falls when a later look no longer carries it', async () => {
    let behind = true
    vi.stubGlobal('fetch', () =>
      Promise.resolve(served(behind ? { etag: '"a"', 'x-raven-page-behind': 'sources' } : { etag: '"a"' })))
    const note = row()

    const mod = await watching()
    expect(note.hidden).toBe(false)
    expect(note.querySelector('.t')!.textContent).toBe(t('gui.update.behind'))
    expect(note.querySelector('.rl')!.textContent).toBe(t('gui.update.behind_how'))

    behind = false
    mod.onVisible()
    await settle()
    expect(note.hidden).toBe(true)
  })

  it('keeps the row while the page stays behind, even after a rebuild landed under it', async () => {
    let tag = '"a"'
    vi.stubGlobal('fetch', () => Promise.resolve(served({ etag: tag, 'x-raven-page-behind': 'sources' })))
    const note = row()

    const mod = await watching()
    tag = '"b"'
    mod.onVisible()
    await settle()
    expect(note.hidden).toBe(false)
    expect(note.querySelector('.rl')!.textContent).toBe(t('gui.update.behind_how'))
  })

  it('yields the row to a rebuilt page once it is no longer behind, and that reload is what clears it', async () => {
    let tag = '"a"'
    let behind = true
    vi.stubGlobal('fetch', () =>
      Promise.resolve(served(behind ? { etag: tag, 'x-raven-page-behind': 'sources' } : { etag: tag })))
    const note = row()

    const mod = await watching()
    expect(note.querySelector('.rl')!.textContent).toBe(t('gui.update.behind_how'))

    tag = '"b"'
    behind = false
    mod.onVisible()
    await settle()
    expect(note.hidden).toBe(false)
    expect(note.querySelector('.rl')!.textContent).toBe(t('gui.update.reload'))
  })

  it('explains how to rebuild when clicked, instead of reloading a page that would come back the same', async () => {
    vi.stubGlobal('fetch', () => Promise.resolve(served({ etag: '"a"', 'x-raven-page-behind': 'sources' })))
    const asked: string[] = []
    const note = row()

    await watching({ 'src/state/confirm': { ask: (title: string) => { asked.push(title) } } })
    note.click()

    expect(asked).toEqual([t('gui.update.behind_title')])
  })

  it('stays down for a page that is not behind', async () => {
    vi.stubGlobal('fetch', () => Promise.resolve(served({ etag: '"a"' })))
    const note = row()

    await watching()
    expect(note.hidden).toBe(true)
  })
})

/* A refusal is shown on a card whose title and command line are already in the
   reader's language. The server's `detail` is one English sentence for every
   caller, so a reason the page can name is named from the catalogue; a reason
   whose detail carries facts from this run keeps them. */
describe('an upgrade the server refused', () => {
  it('names a refusal it knows in the reader language instead of the server sentence', async () => {
    const mod = (await loadPart(() => import('./updates'), {})) as Updates
    const err = {
      data: {
        reason: 'unsupervised',
        detail: 'This Raven was started by hand, so nothing would bring it back after an upgrade.',
      },
    }

    const text = mod.refusalText(err)

    expect(text).toBe(t('gui.upg.why.unsupervised'))
    expect(text).not.toContain('This Raven was started by hand, so nothing')
  })

  it('keeps the detail when it carries facts from this run', async () => {
    const mod = (await loadPart(() => import('./updates'), {})) as Updates
    const detail = 'This Raven installation is not managed by uv.'

    expect(mod.refusalText({ data: { reason: 'not_upgradable', detail } })).toBe(detail)
  })

  it('still says something when the error carries no data at all', async () => {
    const mod = (await loadPart(() => import('./updates'), {})) as Updates

    expect(mod.refusalText({ message: 'socket closed' })).toBe('socket closed')
  })

  it('answers busy work it cannot see with a notice to wait, not the terminal command', async () => {
    const asked: string[][] = []
    const failed: string[] = []
    const shade = { say: () => {}, fail: (text: string) => failed.push(text), close: vi.fn() }
    const mod = (await loadPart(() => import('./updates'), {
      fakes: {
        'src/state/upgradeShade': { open: () => shade },
        'src/state/confirm': {
          ask: (title: string, body: string) => {
            asked.push([title, body])
          },
        },
        'src/rpc/gateway': {
          gateway: () => ({
            call: () => Promise.reject({ data: { reason: 'busy', detail: 'Raven is still working' } }),
          }),
        },
      },
    })) as Updates

    await mod.runUpgrade()

    expect(failed).toEqual([])
    expect(shade.close).toHaveBeenCalled()
    expect(asked).toEqual([[t('gui.upg.title'), t('gui.upg.why.busy')]])
  })
})
