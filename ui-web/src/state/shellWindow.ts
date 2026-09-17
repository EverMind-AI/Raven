/* Moving and zooming the app's own window from inside the page.
 *
 * The OS title bar is hidden in the desktop shell, so the header band has to do
 * its job. WebKit's implicit background-drag heuristic proved fragile -- it
 * silently stopped once the band gained positioned content -- so the shell is
 * told explicitly: grab anywhere in the band that is not a control.
 *
 * Nothing here runs in a browser: the shell marks itself on the root element,
 * and the native bridge is only there to take the message when it does.
 */

type NativeWindow = Window & {
  webkit?: { messageHandlers?: { raven?: { postMessage(value: unknown): void } } }
}

/** Whether this gesture is the band being grabbed rather than a control in it. */
export function band(event: MouseEvent): boolean {
  const target = event.target as Element
  if (document.documentElement.dataset.shell !== '1' || event.button !== 0) return false
  if (!target.closest('.railtop, .top, .ws-top')) return false
  return !target.closest('button, a, input, textarea, select, [role="link"], .grip')
}

function tell(type: 'drag' | 'zoom'): void {
  const bridge = (window as NativeWindow).webkit?.messageHandlers?.raven
  if (!bridge) return
  /* A shell that marked the root but answers its own messages badly is still a
     gesture the reader made; there is nothing to report and nothing to undo. */
  try { bridge.postMessage({ type }) } catch { /* the shell refused it */ }
}

export function onMouseDown(event: MouseEvent): void {
  if (!band(event)) return
  tell('drag')
}

export function onDblClick(event: MouseEvent): void {
  if (!band(event)) return
  tell('zoom')
}
