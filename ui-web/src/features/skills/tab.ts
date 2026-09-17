/* The skill tab's face on the capabilities page.
 *
 * The island renders everything inside the shared body (features/skills/);
 * what is here is the
 * draw that puts it there and the chrome that draw decides -- the heading, the
 * search hint, the filter bar -- which is state/caps.ts's state and
 * src/chrome/CapsPage.tsx's markup.
 *
 * The "installed" entry point rides in the filter bar rather than in the page,
 * exactly like the plugin tab's: shown only on the market, and synced on the
 * plugin tab too so that it does not linger there.
 */

import { T } from '../../i18n/t'
import * as caps from '../../state/caps'
import * as page from '../../state/page'
import { ds } from '../../state/sources'
import { skillsHost } from '../hosts'
import * as skills from './store'

/* Mirror of the island's view, read by the plugin tab's hero sync: the hero
   covers both tabs and is the last step of either draw. */
export let view = 'market'

export function drawSkillTab(): void {
  const box = document.getElementById('capsBody') as HTMLElement
  box.innerHTML = ''
  const title = T('gui.tab.skills')
  const installed = skills.view() === 'installed'
  caps.chrome({
    title: installed ? T('gui.plug.installed_title') : title,
    label: title,
    search: T('gui.hub.search_ph'),
    pillsHidden: true,
    advHidden: true,
    bar: installed ? 'none' : '',
  })
  box.appendChild(skillsHost)
  skills.redraw()
  /* The first reveal fetches; a boot-time draw of the closed page must not fire
     a hub search nobody asked for. */
  if (page.get() === 'capsPage') skills.ensureSearch()
  syncInstalledButton()
}

export function syncInstalledButton(): void {
  caps.installedButton('skill', {
    hidden: caps.get().tab !== 'skill' || skills.view() === 'installed',
    label: T('gui.plug.installed_n', { n: ds('skills').installed().length }),
    badge: null,
  })
}

/** Everything this used to do while the concatenated page script ran, in order. */
export function install(): void {
  /* Created once, the way it was appended to the bar once: no label until the
     first sync, which is the empty button the builder made. */
  caps.installedButton('skill', { hidden: false, label: null, badge: null })
  caps.onDraw({ skill: drawSkillTab, skillButton: syncInstalledButton })

  /* Switching module drops what this tab had. Registered before the plugin
     tab's, which is the order the two decorators' effects were visible in. */
  caps.onTab(() => {
    skills.reset()
  })

  page.subscribe(() => {
    if (page.get() !== 'capsPage') skills.dropDrawer()
  })

  /* The island owns the view; the chrome follows it from out here. A view flip
     redraws the whole tab (title, bar, hero) through the dispatch; any other
     change only needs the installed count refreshed. */
  skills.subscribe(() => {
    const v = skills.view()
    if (v !== view) { view = v; if (caps.get().tab === 'skill') caps.draw(); return }
    syncInstalledButton()
  })
}
