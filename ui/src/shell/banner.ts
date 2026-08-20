/* The strip above the transcript (#bannerHost): at most one standing notice
 * about something the reader has to act on outside this conversation.
 *
 * A writer, not an island. The host is one node the chrome already lays out,
 * nothing subscribes to what it holds, and the two notices are decided by
 * priority rather than composed -- a component would be a root around a
 * single-child switch.
 *
 * Order is the whole design: a memory fault beats a missing capability,
 * because a backend that stopped storing has been handing back normal-looking
 * replies the whole time, while an unconfigured search has been visibly
 * refusing. Whichever wins draws alone.
 */

import { ds, shell, t } from './bridge'

export interface BannerSource {
  /* Whether the websearch capability is installed but not yet configured.
     A read rather than a fact this module keeps: the answer lives in the
     capability list, which the plugins layer owns and the live layer fills. */
  websearchNeeds(): boolean
}

/* A standing memory fault, or null. Set from the `memory.health` event: three
   consecutive failed writes mean the backend is not coming back on its own. */
let fault: string | null = null

/* Stores, and does NOT redraw. That looks like a missing convenience and is
   not: the live layer replaces the published drawBanner with a no-op (see
   live/120-settings.js -- a config gap belongs in the settings page, not as a
   strip over every conversation), so the redraw has to go out through that name
   for the override to still apply. Drawing from in here would put the strip
   back in live mode, which is a behaviour change nobody asked for. */
export function setFault(detail: string | null): void {
  fault = detail
}

export function draw(): void {
  const host = document.getElementById('bannerHost')
  if (!host) return
  host.innerHTML = ''
  if (fault) {
    /* No dismiss: the condition lasts until it is fixed, and a banner the
       reader can wave away is one they will wave away and then forget. */
    const b = document.createElement('div')
    b.className = 'banner bad'
    b.appendChild(el('b', t('gui.mem.down')))
    b.appendChild(el('span', fault))
    host.appendChild(b)
    return
  }
  if (!source().websearchNeeds()) return
  host.appendChild(websearchNotice())
}

function websearchNotice(): HTMLElement {
  const b = document.createElement('div')
  b.className = 'banner'
  b.appendChild(el('b', '网页搜索还没配置'))
  b.appendChild(el('span', '现在 Raven 只能抓你给出的网址，不能自己找资料。'))
  const go = el('button', '去配置')
  go.onclick = () => shell().openWebsearch?.()
  const x = el('button', '✕')
  x.className = 'x'
  x.setAttribute('aria-label', '忽略')
  /* Dismissable, unlike the fault above: an unconfigured capability is a
     suggestion, and the reader saying "not now" is an answer. */
  x.onclick = () => b.remove()
  b.appendChild(go)
  b.appendChild(x)
  return b
}

function source(): BannerSource {
  /* Tolerated missing rather than thrown on: the banner is drawn from the boot
     sequence, and a page that has not installed this source yet has no notice
     to show anyway. */
  try {
    return ds<BannerSource>('banner')
  } catch {
    return { websearchNeeds: () => false }
  }
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, text: string): HTMLElementTagNameMap[K] {
  const n = document.createElement(tag)
  n.textContent = text
  return n
}
