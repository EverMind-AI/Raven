/* The file preview window: one file on a page of its own, with no rail, no
 * dock and no conversation around it.
 *
 * The pane and the window show the same markup because they are the same
 * component -- what changes is only which file it was handed and that nothing
 * else is on screen. The window is a page of this bundle, told by the query
 * which file to read; `previewRequestOf` is the whole of that mode's switch,
 * and src/main.tsx boots nothing else when it answers.
 *
 * Opening the same file twice is deliberately the same window: the name is
 * the path, so the second open navigates the window that is already there,
 * which is also what makes it read the file again rather than show what it
 * held when it was first opened.
 */

export const PREVIEW_PARAM = 'preview'

export interface PreviewRequest {
  path: string
  session: string | null
}

/** The file this page was opened to preview, or null for the ordinary page. */
export function previewRequestOf(search: string): PreviewRequest | null {
  const params = new URLSearchParams(search)
  if (params.get(PREVIEW_PARAM) !== '1') return null
  const path = params.get('path')
  return path ? { path, session: params.get('session') } : null
}

/** The window's own address: this page, in preview mode, for one file. */
export function previewURL(path: string, session: string | null): string {
  const params = new URLSearchParams({ [PREVIEW_PARAM]: '1', path })
  if (session) params.set('session', session)
  return `${location.pathname}?${params.toString()}`
}

/** One window per file, so reopening a file lands on the window that has it. */
export const previewName = (path: string): string => `raven-file:${path}`

/** Whether a window was opened. A blocker answers false, and the pane stays. */
export function openPreviewWindow(path: string, session: string | null): boolean {
  try {
    return window.open(previewURL(path, session), previewName(path), 'popup,width=1040,height=800') != null
  } catch {
    /* A host that refuses the call (a hardened browser, a test window) is a
       window that did not open, which is the answer the pane falls back on. */
    return false
  }
}
