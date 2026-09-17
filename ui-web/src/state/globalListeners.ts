/* Every listener the page holds on the document or the window, in one place.
 *
 * They used to be spread over eight modules and two legacy parts, each
 * registering its own from its own install(), and the order they ended up in
 * was an accident of which file main.tsx called first. That order is a
 * contract, not an accident: three of them are capture-phase and run before the
 * element the reader clicked ever sees the event, and inside one phase the
 * first handler registered runs first. So the popover arbitration closes the
 * two composer popovers only after the menu writer has closed the menu, the
 * chips answer Enter before the Escape chain does, and the link trap reads a
 * click before the transcript can.
 *
 * Hence one function: the sequence below IS the page's listener order, read
 * top to bottom, and src/state/globalListeners.test.ts asserts it call for
 * call. Element-level listeners are not here -- a control's own handler
 * belongs with the control (a component's ref, or the module that draws it) --
 * and neither is anything registered when an overlay opens (each docked sheet
 * adds a keydown of its own, and takes it away again).
 *
 * One line below is not a registration: the hover pill's layer is raised here,
 * beside the listeners that drive it, because the pill's store owns no install
 * of its own and the layer has to stand at the body before the first hover.
 * WHERE it stands among the body's children is state/portals.ts's to decide.
 */

import { trap as linkTrap } from '../features/browser/store'
import { fitField, parkDraftNow } from '../features/composer/mount'
import { composing } from '../features/composer/store'
import { T } from '../i18n/t'
import { openSettings } from '../legacy/demo/150-chrome.js'
import { onLoad as splashOnLoad } from '../legacy/demo/160-boot.js'
import { onClick as chipClick, onKey as chipKey } from '../shell/chips'
import { toggle as toggleFind } from '../shell/find'
import { onPointerDown as menuAway } from '../shell/menu'
import { onResize as reclampPanes } from '../shell/panes'
import { close as closePermPop } from '../shell/perm'
import { isMac } from '../shell/platform'
import { onResize as dropBars, onScroll as barsOnScroll } from '../shell/scrollbars'
import { close as closeTierPop } from '../shell/tier'
import { onContextMenu } from './contextMenu'
import * as overlays from './overlays'
import { get as railOpen, set as setRail } from './rail'
import { clamp as clampSelection } from './selection'
import { close as closeSettings, isOpen as settingsIsOpen } from './settingsDialog'
import { onDblClick as shellZoom, onMouseDown as shellDrag } from './shellWindow'
import * as tip from './tooltip'
import { onVisible as probeOnVisible } from './updates'

/* The two composer popovers have no close button and no Escape branch: a
   pointer landing outside one is the way back out. Capture, because the row
   under the pointer may stop the event.

   The More group is rail navigation rather than a popover, which is why it is
   not here: it folds on its own toggle only, never on an outside click. */
function awayFromPopovers(event: PointerEvent): void {
  const target = event.target as Element
  if (!target.closest('#permPop') && !target.closest('#permChip')) closePermPop()
  if (!target.closest('#tierPop') && !target.closest('#tierChip')) closeTierPop()
}

/* Code blocks come and go with every answer, so the click is caught once here
   rather than bound per block. The text comes from the DOM the reader sees. */
function copyCodeBlock(event: MouseEvent): void {
  const target = event.target as Element | null
  const button = target?.closest ? target.closest<HTMLElement>('.cbcp') : null
  if (!button) return
  const pre = button.closest('.cblk')?.querySelector('pre')
  if (!pre) return
  if (navigator.clipboard) navigator.clipboard.writeText(pre.textContent ?? '')
  button.classList.add('ok')
  button.title = T('gui.code.copied')
  button.setAttribute('aria-label', T('gui.code.copied'))
  setTimeout(() => {
    button.classList.remove('ok')
    button.title = T('gui.code.copy')
    button.setAttribute('aria-label', T('gui.code.copy'))
  }, 1500)
}

/* The Escape order (state/overlays.ts) and the three shortcuts that have always
   shared its handler. They share it because splitting them would add a
   registration, and the sequence above is what this file is for. */
function onEscapeChain(e: KeyboardEvent): void {
  /* Escape ends an open composition; it must not also close a panel or halt
     the running turn behind the reader's back. The guard is the composer
     field's own, which is where the page's other Enter handlers ask it. */
  if (composing(e)) return
  const inField = /INPUT|TEXTAREA/.test(document.activeElement?.tagName ?? '')
  if (e.key === 'Escape' && overlays.dispatch()) return
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') {
    e.preventDefault(); setRail(true); toggleFind(true)
  }
  if ((e.metaKey || e.ctrlKey) && e.key === '\\') {
    e.preventDefault(); setRail(!railOpen())
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'n' && !inField) {
    e.preventDefault(); document.getElementById('newBtn')?.click()
  }
}

/* The platform's own way into settings, the same door the rail's foot row
   opens. A second keydown rather than a branch of the one above, because that
   is how many listeners the page has always had. */
function onSettingsKey(e: KeyboardEvent): void {
  if (e.key !== ',' || !(isMac() ? e.metaKey : e.ctrlKey)) return
  e.preventDefault()
  if (settingsIsOpen()) { closeSettings(); return }
  void openSettings()
}

/* Its own entry point, because the order table it reads has a gate of its own
   that installs this listener and nothing else (state/overlays.test.ts). The
   page reaches it through installGlobalListeners below, which is the only
   caller that may run. */
export function installEscapeChain(): void {
  document.addEventListener('keydown', onEscapeChain)
}

/** Every document- and window-level listener the page holds. Called once. */
export function installGlobalListeners(): void {
  /* A link in an answer belongs to the reader's own browser, never to the
     workspace chrome -- read before the transcript can act on it. */
  document.addEventListener('click', linkTrap, true)
  /* The dock: a draft the debounce has not parked yet, and a field whose
     height cap is a share of the window. */
  window.addEventListener('beforeunload', parkDraftNow)
  window.addEventListener('resize', fitField)
  /* The overlay scrollbars, which cover a scroller drawn at any time by
     listening in the capture phase rather than per element. */
  document.addEventListener('scroll', barsOnScroll, true)
  window.addEventListener('resize', dropBars)
  /* Both panes re-clamp, or a narrowed window leaves the chat no floor. */
  window.addEventListener('resize', reclampPanes)
  /* The path and artefact chips in an answer, which are replaced wholesale
     with every answer -- so the click and the key are the document's. */
  document.addEventListener('click', chipClick)
  document.addEventListener('keydown', chipKey)
  /* The context menu closes on a pointer outside it; the two popovers below
     do the same, in that order, because that is the order they were written
     in and neither has ever been observed to depend on the other. */
  document.addEventListener('pointerdown', menuAway, true)
  document.addEventListener('contextmenu', onContextMenu)
  document.addEventListener('pointerdown', awayFromPopovers, true)
  /* The hover pill, whose one layer stands at the body from here on. */
  tip.mount()
  document.addEventListener('pointerover', tip.follow)
  tip.watch()
  document.addEventListener('scroll', tip.hide, true)
  /* The desktop shell's title band: a grab and a double-click on it move and
     zoom the window the page is drawn in. */
  document.addEventListener('mousedown', shellDrag)
  document.addEventListener('dblclick', shellZoom)
  document.addEventListener('selectionchange', clampSelection)
  document.addEventListener('click', copyCodeBlock)
  installEscapeChain()
  document.addEventListener('keydown', onSettingsKey)
  /* The splash comes down when the page is loaded, unless the page's own boot
     has claimed that moment already (legacy/demo/160-boot.js). */
  window.addEventListener('load', splashOnLoad)
  /* A tab coming back to the front is a reason to look for a new build. The
     watcher decides whether there is anything to look for; until it has
     started, this answers nothing (state/updates.ts). */
  document.addEventListener('visibilitychange', probeOnVisible)
}
