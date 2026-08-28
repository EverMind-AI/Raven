import { createRoot } from 'react-dom/client'

import { AttTray, QueueList, SlashList, TurnLive } from './ComposerPage'
import * as store from './store'
export * as turn from './turn'

import type { Root } from 'react-dom/client'

/* The composer island's face: the roots over the dock's containers, the
 * dock's own event wiring, and the verbs the legacy shims call.
 *
 * The roots are created on the first paint rather than at bundle time: this
 * script is assembled AHEAD of the page script (ui-web/build.py), so neither
 * window.RavenShell nor DS.composer exists yet when it evaluates. install()
 * therefore only registers listeners -- every handler reads the seams lazily,
 * by which time the page has published them.
 */

let roots: Root[] = []
let liveHost: HTMLElement | null = null

/* The live turn row rides the tail of #stage, which the transcript island owns
 * a lane host in and the legacy layers still wipe and park wholesale. So it
 * gets a host of its own, one `display: contents` element appended after the
 * transcript's, and the host is only attached while a turn is alive -- the row
 * used to be appended and removed the same way.
 *
 * Moved only when something new landed after it: re-inserting a node restarts
 * its CSS animation, so an unconditional append would make the glyph stutter
 * four times a second.
 */
function syncLiveHost(): void {
  const host = liveHost
  if (!host) return
  if (!store.getState().live) {
    host.remove()
    return
  }
  const stage = document.getElementById('stage')
  if (!stage) return
  if (stage.lastElementChild !== host) stage.appendChild(host)
}

function ensure(): void {
  if (roots.length) return
  const queued = document.getElementById('queued')
  const slashList = document.getElementById('slashList')
  const ta = store.field()
  if (!queued || !slashList || !ta) return
  /* The tray's container is not in the markup: it only exists once something
     is staged, and it belongs above the field inside the card. */
  let atts = document.getElementById('atts')
  if (!atts) {
    atts = document.createElement('div')
    atts.className = 'atts'
    atts.id = 'atts'
    atts.hidden = true
    const dock = ta.closest('.dock-in')
    if (dock) dock.insertBefore(atts, dock.querySelector('.field'))
  }
  liveHost = document.createElement('div')
  liveHost.dataset.cvl = '1'
  liveHost.style.display = 'contents'
  const qr = createRoot(queued)
  const ar = createRoot(atts)
  const sr = createRoot(slashList)
  const lr = createRoot(liveHost)
  qr.render(<QueueList />)
  ar.render(<AttTray />)
  sr.render(<SlashList />)
  lr.render(<TurnLive afterPaint={syncLiveHost} />)
  roots = [qr, ar, sr, lr]
  store.subscribe(syncLiveHost)
}

/* ── the verbs the legacy shims call ──────────────────────────────────── */

export function goPaint(): void {
  ensure()
  store.goPaint()
}

export function drawQueue(): void {
  ensure()
  store.drawQueue()
}

export const queuePush = (text: string): void => store.queuePush(text)
export const queueShift = (): string | undefined => store.queueShift()
export const queueClear = (): void => store.queueClear()
export const queueSnapshot = (): string[] => store.queueSnapshot()
export const queueRestore = (items: string[]): void => store.queueRestore(items)
export const parkDraft = (): void => store.parkDraft()
export const loadDraft = (id: string | null): void => store.loadDraft(id)
export const dropDraft = (id: string | null): void => store.dropDraft(id)
export const claimDraft = (id: string | null): void => store.claimDraft(id)

export function drawMeter(): void {
  ensure()
  store.drawMeter()
}

export function fitField(): void {
  ensure()
  store.fitField()
}

export function dockLift(): void {
  store.dockLift()
}

export const liveAnchor = (): number => store.liveAnchor()

export function setLiveAnchor(ms: number): void {
  store.setLiveAnchor(ms)
}

/* Its svg twin, for a node drawn inside a graph (the dag sheet still draws
   itself). Same three dots, same shared keyframes; only the element type
   differs. `y` is the baseline the bars used to stand on, so the dots are
   centred a glyph-height above it and the call sites keep the coordinates they
   already pass. */
export function workGlyphSvg(x: number, y: number): SVGGElement {
  const ns = 'http://www.w3.org/2000/svg'
  const g = document.createElementNS(ns, 'g')
  g.setAttribute('class', 'workv')
  g.setAttribute('aria-hidden', 'true')
  for (const dx of [0, 4, 8]) {
    const c = document.createElementNS(ns, 'circle')
    c.setAttribute('cx', String(x + dx + 1))
    c.setAttribute('cy', String(y - 4.5))
    c.setAttribute('r', '1.5')
    g.appendChild(c)
  }
  return g
}

/* ── the dock's wiring ────────────────────────────────────────────────── */

let picker: HTMLInputElement | null = null

function openPicker(): void {
  if (!picker) {
    picker = document.createElement('input')
    picker.type = 'file'
    picker.multiple = true
    picker.style.display = 'none'
    picker.onchange = () => {
      if (picker && picker.files) store.addFiles(picker.files)
      if (picker) picker.value = ''
    }
    document.body.appendChild(picker)
  }
  picker.click()
}

let wired = false

export function install(): void {
  if (wired) return
  const ta = store.field()
  if (!ta) return
  wired = true

  ta.addEventListener('input', () => {
    ensure()
    store.fieldInput()
  })
  ta.addEventListener('keydown', (e) => {
    ensure()
    store.fieldKeydown(e)
  })
  ta.addEventListener('blur', () => setTimeout(() => store.closeSlash(), 120))
  ta.addEventListener('paste', (e) => {
    if (!store.canAttach()) return
    const dt = (e as ClipboardEvent).clipboardData
    const files = dt ? [...dt.files] : []
    if (!files.length) return
    e.preventDefault()
    ensure()
    store.addFiles(files)
  })
  /* The debounce above can still be in flight when the tab goes away. */
  window.addEventListener('beforeunload', () => store.parkDraftNow())

  const go = document.getElementById('go')
  if (go) {
    go.onclick = () => {
      ensure()
      store.goClick()
    }
  }
  const attBtn = document.getElementById('attBtn')
  if (attBtn) {
    attBtn.onclick = () => {
      ensure()
      store.pickFiles(openPicker)
    }
  }

  /* The drop target exists only where a dropped file has somewhere to go: the
     demo canvas offers no upload, so it must not light up the field either. */
  const fieldBox = ta.closest('.field')
  if (fieldBox) {
    let depth = 0
    const over = (e: Event): void => { if (store.canAttach()) e.preventDefault() }
    fieldBox.addEventListener('dragenter', (e) => {
      if (!store.canAttach()) return
      over(e)
      depth += 1
      fieldBox.classList.add('drop')
    })
    fieldBox.addEventListener('dragover', over)
    fieldBox.addEventListener('dragleave', () => {
      depth -= 1
      if (depth <= 0) fieldBox.classList.remove('drop')
    })
    fieldBox.addEventListener('drop', (e) => {
      if (!store.canAttach()) return
      e.preventDefault()
      depth = 0
      fieldBox.classList.remove('drop')
      const dt = (e as DragEvent).dataTransfer
      if (!dt || !dt.files.length) return
      ensure()
      store.addFiles(dt.files)
    })
  }

  const pill = document.getElementById('backpill')
  if (pill) pill.onclick = () => store.pillClick()
  const sc = document.getElementById('scroll')
  if (sc) {
    sc.addEventListener('wheel', (e) => store.wheeled((e as WheelEvent).deltaY < 0), { passive: true })
    sc.addEventListener('scroll', () => store.scrolled())
  }

  /* A vh-based cap changes with the window, so the height has to be
     recomputed. */
  window.addEventListener('resize', () => {
    ensure()
    store.fitField()
  })
  const dock = document.querySelector('.dock')
  if (!dock) return
  /* Catches every reason the dock changes height -- typing, a pasted blob,
     queued rows, staged attachments -- without a call at each site. */
  if (typeof ResizeObserver === 'function') new ResizeObserver(() => store.dockLift()).observe(dock)
  /* A popover above the composer is absolutely positioned, so opening one never
     resizes the dock and the observer above never fires: without this the pill
     would ignore the popover, and worse, keep whatever offset it was last
     given. Safe against a loop -- dockLift writes to .chat, outside .dock. */
  if (typeof MutationObserver === 'function') {
    new MutationObserver(() => store.dockLift()).observe(dock, {
      subtree: true, childList: true, attributes: true,
      attributeFilter: ['data-open', 'hidden', 'style', 'class'],
    })
  }
}

/* Test seam only: the roots and the listeners survive between tests. */
export function _resetForTests(): void {
  roots = []
  liveHost = null
  wired = false
  picker = null
}
