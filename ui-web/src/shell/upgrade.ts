/* The upgrade progress card appended over the page while serve is unavailable. */

import { t } from './bridge'

export interface UpgradeShade {
  say(text: string): void
  fail(text: string, detail?: string): void
  close(): void
}

const el = (tag: string, className?: string, text?: string): HTMLElement => {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (text !== undefined) node.textContent = text
  return node
}

export function open(): UpgradeShade {
  document.querySelectorAll('.upshade').forEach(node => node.remove())
  const shade = el('div', 'upshade')
  const card = el('div', 'upcard')
  const bar = el('div', 'upbar')
  bar.appendChild(el('i'))
  const title = el('div', 't')
  card.append(bar, title)
  shade.appendChild(card)
  document.body.appendChild(shade)
  return {
    say(text: string): void {
      title.textContent = text
    },
    fail(text: string, detail?: string): void {
      bar.hidden = true
      title.textContent = text
      const sub = el('div', 'sub', detail ? `${detail}\n${t('gui.upg.manual')}` : t('gui.upg.manual'))
      const command = el('div', 'cmd')
      const code = el('code', undefined, 'raven upgrade')
      const copy = el('button', undefined, t('gui.dtl.copy'))
      copy.onclick = () => {
        if (navigator.clipboard) navigator.clipboard.writeText('raven upgrade')
      }
      command.append(code, copy)
      const foot = el('div', 'foot')
      const close = el('button', 'btn', t('gui.upg.close'))
      close.onclick = () => shade.remove()
      foot.appendChild(close)
      card.append(sub, command, foot)
    },
    close(): void {
      shade.remove()
    },
  }
}
