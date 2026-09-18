/* The model chip under the composer: what it says, and what clicking it opens.
 *
 * Two halves of one element: the label every path that changes the model
 * repaints, and the one click that
 * raises the picker. Both stay imperative writes on elements src/chrome/Dock.tsx
 * renders -- the chip's text is a model id rather than a phrase from the
 * catalogue, so the component has no value of its own for it, and React diffs
 * against the props it rendered last rather than against the document.
 */

import { openModelsForMissingProvider } from './source'
import { current, open as openPicker } from './store'

/* A model id is provider-qualified (openrouter/anthropic/claude-opus-4.6); the
   chip only has room for the part that identifies the model. */
const shortModel = (m: string): string => String(m || '').split('/').pop() as string

/** Repaints the chip from the current model. The hover title is the full id. */
export function label(): void {
  const model = current()
  ;(document.getElementById('modelName') as HTMLElement).textContent = shortModel(model)
  ;(document.getElementById('modelChip') as HTMLElement).title = model
}

/* The chip's own click: the picker, against the provider list the page really
   has. A build with no provider configured sends the reader to Models first,
   which is what that guard answers. */
export function install(): void {
  ;(document.getElementById('modelChip') as HTMLElement).onclick = () => {
    if (openModelsForMissingProvider()) return
    openPicker(null, label)
  }
}
