/* The preview approval sheet's interior: what the agent wants to do, and the two
 * answers.
 *
 * The variant ask_user raises and `window.__approve` previews -- what an
 * approval looked like before the permission gate had its own request. The
 * sheet element and the document key handler belong to
 * features/composer/approve.ts; this renders its children, for the reason
 * src/chrome/SheetRack.tsx gives.
 *
 * The wording arrives as props: the opener reads the catalogue once when the
 * request lands, so a language flip does not re-word a question already on
 * screen.
 */
import { SheetOption } from './SheetRack'
import { CROSS, Glyph } from '../components/Ico'

import type { SheetOptionRow } from './SheetRack'
import type { JSX } from 'react'

/* The row every sheet in the rack opens with: what is being asked, and the
   control that refuses it. */
export function SheetHead(
  { title, deny, onDeny }: { title: string; deny: string; onDeny: () => void },
): JSX.Element {
  return (
    <div className="hd">
      <div className="q">{title}</div>
      <button className="ic tipdn" data-tip={deny} aria-label={deny} onClick={onDeny}>
        <Glyph d={CROSS} />
      </button>
    </div>
  )
}

export interface ApproveProps {
  readonly title: string
  readonly deny: string
  readonly prompt: string
  readonly opts: readonly SheetOptionRow[]
  readonly onDeny: () => void
}

export function ApproveSheet({ title, deny, prompt, opts, onDeny }: ApproveProps): JSX.Element {
  return (
    <>
      <SheetHead title={title} deny={deny} onDeny={onDeny} />
      {/* The request itself is the agent's own words about what it wants to do,
          so it is quoted rather than restated. */}
      <div className="body">
        <div className="what">{prompt}</div>
        {opts.map((row, i) => <SheetOption key={i} n={i + 1} row={row} />)}
      </div>
    </>
  )
}
