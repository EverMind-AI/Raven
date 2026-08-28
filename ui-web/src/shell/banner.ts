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

/* Stores AND draws. It used to only store, so that the redraw would go out
   through the published drawBanner name and pick up the live layer's override
   of it -- and that override cleared the host, so storing a fault put nothing
   on screen. The override is gone (live/120-settings.js says its no to the one
   notice it means, through the source), and with it the reason for a setter
   whose effect depends on the caller remembering a second call. */
export function setFault(detail: string | null): void {
  fault = detail
  draw()
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

const source = (): BannerSource => ds<BannerSource>('banner')

function el<K extends keyof HTMLElementTagNameMap>(tag: K, text: string): HTMLElementTagNameMap[K] {
  const n = document.createElement(tag)
  n.textContent = text
  return n
}
