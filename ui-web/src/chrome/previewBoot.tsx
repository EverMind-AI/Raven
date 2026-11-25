/* The preview window's boot: one file on a page of its own, with the shell
 * left off.
 *
 * A file preview is not a second application. It reads the same file over the
 * same `/file` route, draws it with the same body the workspace pane draws, and
 * differs from the pane only in having no conversation around it -- so what
 * this module adds is a host and nothing else. The page's own boot never runs:
 * src/main.tsx picks this instead, which is what keeps the socket, the session
 * list, the rail and the composer off a window that shows one file.
 */

import { createRoot } from 'react-dom/client'

import { PreviewWindow } from '../features/workspace/PreviewWindow'
import { setCurrent } from '../lib/session'
import { installCodeCopy } from '../state/globalListeners'
import '../state/lang/store'

import type { PreviewRequest } from '../features/workspace/preview'

export function bootPreview(request: PreviewRequest): void {
  setCurrent(request.session)
  /* The one page listener this window's markup needs: a fenced code block's
     copy control is delegated on the document, and none of the page's other
     listeners belong on a window that shows one file. */
  installCodeCopy()
  /* The static shells and the shell's own regions are hidden together by
     `html[data-preview]` in features/workspace/styles.css: the splash is a
     first-frame shell this page has no use for, and the body it was served
     with belongs to the page that is not being drawn. */
  document.documentElement.dataset.preview = '1'
  const host = document.createElement('div')
  host.id = 'previewRoot'
  document.body.appendChild(host)
  createRoot(host).render(<PreviewWindow path={request.path} />)
}
