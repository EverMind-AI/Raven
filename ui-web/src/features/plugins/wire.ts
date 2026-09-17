/* The plugins domain's side of the page: how the page reaches it, and the face
 * it puts on the capabilities page.
 *
 * Opening the page on whichever of its two tabs is navigation -- "where the
 * rail's button goes" -- and keeping it out of state/caps.ts leaves that store
 * holding only what the page shows. The skeleton before the first load is the
 * skills island's, appended into the shared body by hand: nothing is rendered
 * in there until a draw, and an empty grid reads as "this install has no
 * skills" rather than "this is loading".
 *
 * The island renders everything inside the shared body and the shared detail
 * drawer (features/plugins/); what is here is the draw that puts the host
 * there, the chrome that draw decides, and the hero -- which covers BOTH tabs,
 * which is why it is the last step of either draw and why it reads the skill
 * tab's own view mirror (features/skills/wire.ts).
 *
 * The draw steps are the outer half of a draw: state/caps.ts declares the order
 * the two tabs draw in and calls these. `install()` is everything this used to
 * do while the concatenated page script ran, in order.
 */

import { t } from '../../i18n/t'
import * as caps from '../../state/caps'
import * as page from '../../state/page'
import { ds } from '../../state/sources'
import { show as toast } from '../../state/toast'
import { plugHost, skillsSkeletonHost } from '../hosts'
import { view as skillView } from '../skills/wire'
import * as plugins from './store'

export async function openCaps(tab: caps.Tab): Promise<void> {
  caps.extSet(tab)
  page.show('capsPage')
  const src = ds('capabilities')
  if (src.loaded()) caps.draw()
  else {
    const box = document.getElementById('capsBody') as HTMLElement
    box.innerHTML = ''
    box.appendChild(skillsSkeletonHost)
  }
  try {
    if (await src.load()) caps.draw()
  } catch (e) {
    toast(`加载失败：${(e as Error).message || String(e)}`)
    caps.draw()
  }
}

export const openSkills = (): Promise<void> => openCaps('skill')
export const openPlugins = (): Promise<void> => openCaps('plugin')

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
  const title = t('gui.tab.plugins')
  const view = plugins.view()
  caps.chrome({
    title: view === 'installed' ? t('gui.plug.installed_title') : title,
    label: title,
    search: t('gui.plug.search_ph'),
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
    label: t('gui.plug.installed_n', { n: plugins.installedCount() }),
    badge: attn ? String(attn) : null,
  })
}

/* Title only -- no tagline under it; that copy read as marketing, not UI. It
   covers both tabs, which is why it is the last step of either draw. */
export function syncHero(): void {
  let title = ''
  if (caps.get().tab === 'plugin' && plugins.view() === 'market') title = t('gui.plug.hero')
  else if (caps.get().tab === 'skill' && skillView === 'market') title = t('gui.hub.hero')
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
