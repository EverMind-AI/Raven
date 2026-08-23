/* The one context menu shared by rows and action buttons across the page. */

import type { MenuItem } from './bridge'

const host = (): HTMLElement | null => document.getElementById('menu')

export function close(): void {
  const menu = host()
  if (menu) menu.dataset.open = 'false'
}

export function show(x: number, y: number, items: Array<MenuItem | '-'>): void {
  const menu = host()
  if (!menu) return
  menu.innerHTML = ''
  items.forEach(item => {
    if (item === '-') {
      menu.appendChild(document.createElement('hr'))
      return
    }
    const button = document.createElement('button')
    if (item.bad) button.className = 'bad'
    button.textContent = item.label
    button.onclick = () => {
      menu.dataset.open = 'false'
      item.fn()
    }
    menu.appendChild(button)
  })
  menu.dataset.open = 'true'
  const rect = menu.getBoundingClientRect()
  menu.style.left = `${Math.min(x, window.innerWidth - rect.width - 8)}px`
  menu.style.top = `${Math.min(y, window.innerHeight - rect.height - 8)}px`
}

export function install(): void {
  document.addEventListener(
    'pointerdown',
    event => {
      const target = event.target as Element | null
      if (!target?.closest('#menu')) close()
    },
    true
  )
}
