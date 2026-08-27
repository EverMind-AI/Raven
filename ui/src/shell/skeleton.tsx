/* The shape of a card that has not arrived yet.
 *
 * A market card's body is fetched when the card opens, and the wait used to be
 * a line of grey text -- which collapsed the panel to a 63px strip that then
 * jumped to 640px when the body landed. Two layout states for one card, the
 * first of them a bar with a word in it.
 *
 * So: the card's own anatomy, drawn empty, at a settled height. It is a fixed
 * box on purpose -- the point is that nothing moves between "opening" and
 * "open" except the filling-in.
 */

import type { JSX } from 'react'

export function CardSkeleton(): JSX.Element {
  return (
    <div className="skel" aria-busy="true" aria-hidden="true">
      <div className="skhead">
        <div className="sktile" />
        <div className="skmeta">
          <div className="skline w50" />
          <div className="skline w30" />
        </div>
      </div>
      <div className="sksec">
        <div className="skline cap w20" />
        <div className="skline" />
        <div className="skline w80" />
      </div>
      <div className="sksec">
        <div className="skline cap w20" />
        <div className="skline w70" />
        <div className="skline w40" />
      </div>
      <div className="sksec">
        <div className="skline cap w20" />
        <div className="skline w80" />
      </div>
    </div>
  )
}
