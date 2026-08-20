/* The nav flyout: the 更多 group of the rail.
 *
 * 连接 / 入口 / 定时 live one level in, because they are set-up-once surfaces
 * rather than daily destinations. Three rows built from a table, into a slim
 * column that grows out of the rail (#moreFly in the markup).
 *
 * A writer, not an island. Two reasons: each row carries raw SVG path data
 * that the style freeze wants byte-identical, and the marks on these rows are
 * decided together with the marks on the rail's own nav buttons -- which live
 * outside any root a component here could own. So this module owns #moreFly
 * and everything inside it, the rail island owns the buttons above it, and
 * mark() below is the seam between them: the rail's markNew() calls it and
 * reads back whether the group stood open, instead of reaching into these rows
 * itself. One writer per element, named.
 *
 * The openers go out through the shell rather than being called directly: they
 * are legacy names that a layer above may still rebind, and a stored reference
 * would keep calling whichever one existed when this module evaluated.
 */

import { shell, t } from './bridge'

interface NavRow {
  page: string
  nameKey: string
  path: string
  go(): void
}

export const MORE_ROWS: readonly NavRow[] = [
  {
    page: 'xaPage',
    nameKey: 'gui.nav.agents',
    path: '<rect x="3.5" y="4" width="7" height="7" rx="1.6"/><rect x="13.5" y="13" width="7" height="7" rx="1.6"/><path d="M10.5 7.5h3.5a3 3 0 0 1 3 3v2.5"/>',
    go: () => shell().openXa?.(),
  },
  {
    page: 'connPage',
    nameKey: 'gui.nav.conn',
    path: '<path d="M9.5 14.5 6.8 17.2a3.3 3.3 0 0 1-4.7-4.7l2.7-2.7M14.5 9.5l2.7-2.7a3.3 3.3 0 0 1 4.7 4.7l-2.7 2.7M9 15l6-6"/>',
    go: () => shell().openConn?.(),
  },
  {
    page: 'cronPage',
    nameKey: 'gui.nav.cron',
    path: '<circle cx="12" cy="12.5" r="7.5"/><path d="M12 8.5v4.2l2.6 1.6M9 2.5h6"/>',
    go: () => shell().openCron?.(),
  },
]

const fly = (): HTMLElement | null => document.getElementById('moreFly')

const pageUp = (page: string): boolean => document.getElementById(page)?.dataset.open === 'true'

export function draw(): void {
  const box = fly()
  if (!box) return
  box.innerHTML = ''
  MORE_ROWS.forEach((row) => {
    const b = document.createElement('button')
    b.className = 'mrow'
    b.setAttribute('aria-current', String(pageUp(row.page)))
    b.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${row.path}</svg>`
    /* Names only. These three rows are places the reader already knows by
       name; a sentence under each turned a three-item group into a panel. */
    const nm = document.createElement('div')
    nm.className = 'nm'
    nm.textContent = t(row.nameKey)
    b.appendChild(nm)
    /* The group stays open on a pick: it is navigation now, and the row's own
       current mark is the answer to "where am I". */
    b.onclick = () => {
      row.go()
      shell().markNew?.()
    }
    box.appendChild(b)
  })
}

/* Re-reads the mark on every row and answers whether the group stands open.
   Called by the rail island's markNew(), which owns the buttons above: while
   the group is open its rows are rail rows and the current one wears the mark
   itself, so the parent must not also claim it. */
export function mark(): boolean {
  const box = fly()
  if (!box || box.dataset.open !== 'true') return false
  const rows = box.querySelectorAll('.mrow')
  rows.forEach((b, i) => {
    const row = MORE_ROWS[i]
    b.setAttribute('aria-current', String(!!row && pageUp(row.page)))
  })
  return true
}

export function toggle(force?: boolean): void {
  const box = fly()
  if (!box) return
  const open = force != null ? force : box.dataset.open !== 'true'
  if (open) draw()
  box.dataset.open = String(open)
  document.getElementById('moreBtn')?.setAttribute('aria-expanded', String(open))
  shell().markNew?.()
}

export function install(): void {
  const btn = document.getElementById('moreBtn')
  if (btn) {
    btn.onclick = (e) => {
      e.stopPropagation()
      toggle()
    }
  }
}
