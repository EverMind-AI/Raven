/* The bird, as a silhouette.
 *
 * Not assets/raven.svg: that is the tile on its own ink ground, square and
 * opaque, which the tab strip and the processor row want and a line of text
 * does not. This is the mark the design draws in chrome: one shape that takes
 * `currentColor`, so it follows whatever text it sits beside through both
 * themes, with the halo and the iris holding the one warm gold that does not
 * belong to either theme.
 *
 * The beak notch and the gap under the wing are cut to the surface behind them
 * rather than painted, and the halo is cut where the body crosses it, which is
 * what keeps a ring of ground between the two. That is three masks' worth of
 * geometry the drawing carries itself -- the caller used to have to name the
 * surface (`--mark-eye`) because the old mark painted its holes.
 *
 * `slot` names the instance, because the masks are referenced by id and ids are
 * document-wide: this renders twice (the rail wordmark and the chat header),
 * and under one id the second instance would point at the first one's masks --
 * which breaks outright, not just cosmetically, the moment the rail collapses
 * and takes that definition with it. A caller-supplied name rather than
 * `useId`, so the ids are stable across renders and legible in the boot golden.
 *
 * No class names anywhere in it: the fills are attributes, so nothing here can
 * collide with a page class (scripts/check-class-namespace.mjs).
 */
import type { JSX } from 'react'

/** The source box: the drawing cropped to its own ink, so a given height is
 *  the bird rather than the bird plus the air the artboard came with. Square,
 *  and it stays square -- the width a caller gets for a height is this ratio.
 *  The masks below still measure from the artboard's own 0,0; this window sits
 *  inside it, so nothing they cover falls outside what is drawn. */
const VIEW_BOX = '7.72 5.19 48.80 48.80'
const W = 48.8
const H = 48.8

/** The one colour that is neither the ink nor the ground, on either theme. */
const GOLD = '#B08D4F'

const BODY =
  'M13 53C14.5 45.5 19 42 22 36.5C24 32.8 22.9 29.7 23.8 25.5C25 19.8 29.4 16 35 16C40.1 16 44 19.1 45.8 23.5C50.2 24.5 54.1 27.4 56 31.5C51.5 30.2 47.7 29.8 43.5 30.7C41.5 31.2 40.3 33 40.1 35.5C39.5 42 42.1 48 46 53H13Z'

export function RavenMark({ slot, height = 21 }: { slot: string; height?: number }): JSX.Element {
  const body = `rv-body-${slot}`
  const halo = `rv-halo-${slot}`
  const cuts = `rv-cuts-${slot}`
  return (
    <svg
      width={Math.round((height * W) / H)}
      height={height}
      viewBox={VIEW_BOX}
      fill="none"
      aria-hidden="true"
    >
      <defs>
        <path id={body} d={BODY} />
        <mask id={halo} maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">
          <rect width="64" height="64" fill="white" />
          <use href={`#${body}`} fill="black" stroke="black" strokeWidth="4" strokeLinejoin="round" />
        </mask>
        <mask id={cuts} maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">
          <rect width="64" height="64" fill="white" />
          <path d="M34.8 24.6Q38.6 20.7 42.4 24.6Q38.6 28.5 34.8 24.6Z" fill="black" />
          <path d="M18 53C22 46 27 41.8 33 38C30.4 43.3 29.8 47.8 30 53H18Z" fill="black" />
        </mask>
      </defs>
      <circle cx="31" cy="29" r="22" stroke={GOLD} strokeWidth="1.6" mask={`url(#${halo})`} />
      <use href={`#${body}`} fill="currentColor" mask={`url(#${cuts})`} />
      <circle cx="38.6" cy="24.6" r="1.5" fill={GOLD} />
      <circle cx="38.6" cy="24.6" r="0.65" fill="currentColor" />
    </svg>
  )
}
