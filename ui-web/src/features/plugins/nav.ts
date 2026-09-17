/* Opening the capabilities page, on whichever of its two tabs.
 *
 * One opener and two aliases, beside features/connections/nav.ts for the same
 * reason: "where the rail's button goes" is navigation, and keeping it out of
 * state/caps.ts leaves that store holding only what the page shows.
 *
 * The skeleton before the first load is the skills island's, appended into the
 * shared body by hand: nothing is rendered in there until a draw, and an empty
 * grid reads as "this install has no skills" rather than "this is loading".
 */

import { islands } from '../../islands'
import { show as toast } from '../../shell/toast'
import * as caps from '../../state/caps'
import * as page from '../../state/page'
import { sources } from '../../state/sources'

export async function openCaps(tab: caps.Tab): Promise<void> {
  caps.extSet(tab)
  page.show('capsPage')
  const src = sources.capabilities!
  if (src.loaded()) caps.draw()
  else {
    const box = document.getElementById('capsBody') as HTMLElement
    box.innerHTML = ''
    box.appendChild(islands.skills.skeleton)
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
