/* The plugin tab's face on the capabilities page, and the hero above both.
 *
 * The island renders everything inside the shared body and the shared detail
 * drawer
 * (features/plugins/); what is here is the draw that puts the host there, the
 * chrome that draw decides, and the hero -- which covers BOTH tabs, which is
 * why it is the last step of either draw and why it reads the skill tab's own
 * view mirror (features/skills/tab.ts).
 *
 * The steps here are the outer half of a draw: state/caps.ts declares the
 * order the two tabs draw in and calls these.
 */

import { view as skillView } from '../skills/tab'
import { T } from '../../i18n/t'
import { plugHost } from '../hosts'
import * as plugins from './store'
import * as caps from '../../state/caps'
import * as page from '../../state/page'
import { ds } from '../../state/sources'

/* Which rows the rail's attention badge counts. Skills never block (they are
   method, not access), so a badge that says "something needs you" belongs to
   the plugins module alone -- and it reads the installed rows through the
   source, which is why it lives beside the page that draws them rather than
   with the inventory. */
const needsAttn = (c: { state?: string; update?: unknown }): boolean =>
  c.state === 'need' || c.state === 'fail' || !!c.update
const attnCount = (): number => ds('plugins').rows().filter(needsAttn).length

/* The plugin tab's face on the body: the host first, then the chrome. The host
   node survives other tabs clearing the body (they only detach it), so
   re-appending it costs nothing and loses no React state. */
export function drawPlugTab(): void {
  const box = document.getElementById('capsBody') as HTMLElement
  box.innerHTML = ''
  box.appendChild(plugHost)
  const title = T('gui.tab.plugins')
  const view = plugins.view()
  caps.chrome({
    title: view === 'installed' ? T('gui.plug.installed_title') : title,
    label: title,
    search: T('gui.plug.search_ph'),
    pillsHidden: true,
    advHidden: view !== 'market',
    bar: view === 'installed' ? 'none' : '',
  })
  syncInstalledButton()
  plugins.redraw()
  /* The first reveal fetches; a boot-time draw of the closed page must not fire
     a market search nobody asked for. */
  if (page.get() === 'capsPage') plugins.searchIfIdle()
}

export function syncInstalledButton(): void {
  const attn = attnCount()
  caps.installedButton('plugin', {
    hidden: caps.get().tab !== 'plugin' || plugins.view() === 'installed',
    label: T('gui.plug.installed_n', { n: plugins.installedCount() }),
    badge: attn ? String(attn) : null,
  })
}

/* Title only -- no tagline under it; that copy read as marketing, not UI. It
   covers both tabs, which is why it is the last step of either draw. */
export function syncHero(): void {
  let title = ''
  if (caps.get().tab === 'plugin' && plugins.view() === 'market') title = T('gui.plug.hero')
  else if (caps.get().tab === 'skill' && skillView === 'market') title = T('gui.hub.hero')
  caps.hero(title)
}

/** Everything this used to do while the concatenated page script ran, in order. */
export function install(): void {
  caps.installedButton('plugin', { hidden: false, label: null, badge: null })
  caps.onDraw({ plugin: drawPlugTab, pluginButton: syncInstalledButton, hero: syncHero })

  caps.onTab(() => {
    plugins.reset()
    caps.bar('')
  })

  page.subscribe(() => {
    if (page.get() !== 'capsPage') { plugins.drawerClosed(); caps.bar('') }
  })
}
