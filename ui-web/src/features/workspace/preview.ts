/* The file preview page: one file on a page of its own, with no rail, no
 * dock and no conversation around it.
 *
 * The pane and the page show the same markup because they are the same
 * component -- what changes is only which file it was handed and that nothing
 * else is on screen. The page is this bundle, told by the query which file to
 * read; `previewRequestOf` is the whole of that mode's switch, and src/main.tsx
 * boots nothing else when it answers.
 *
 * The page opens in a tab of the reader's browser, which is the one surface
 * that survives beside the conversation without taking a window of its own.
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

/** The page's own address: this page, in preview mode, for one file. */
export function previewURL(path: string, session: string | null): string {
  const params = new URLSearchParams({ [PREVIEW_PARAM]: '1', path })
  if (session) params.set('session', session)
  return `${location.pathname}?${params.toString()}`
}

/** Opens the file's page in a new tab. A host that refuses the call is not an
 *  error: the pane the reader is already in stays where it is. */
export function openPreviewTab(path: string, session: string | null): void {
  try {
    window.open(previewURL(path, session), '_blank', 'noopener')
  } catch {
    /* A hardened browser, or a test window, has no tab to give. */
  }
}
