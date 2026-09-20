/* The bird, as a silhouette.
 *
 * Not assets/raven.svg: that is the illustrated tile the agent rosters wear --
 * square, full colour, and carrying its own cream ground, which on a dark rail
 * or a dark page is a lit square. This is the mark the design draws in chrome:
 * one shape that takes `currentColor`, so it follows whatever text it sits
 * beside through both themes.
 *
 * The eyes are cut to the surface behind them rather than painted white, or
 * they read as two dots on a dark ground instead of as holes. Which surface
 * that is belongs to the caller, so it comes in as `--mark-eye`; the default is
 * the rail's, which is where this is worn most.
 *
 * The beak keeps its own two greys. They are the one part of the mark that is
 * neither the ink nor the ground, and both read on either theme.
 *
 * No class names anywhere in it: the fills are attributes, so nothing here can
 * collide with a page class (scripts/check-class-namespace.mjs).
 */

import type { JSX } from 'react'

/** The source box, and what keeps a given height from distorting the bird. */
const W = 34
const H = 43

export function RavenMark({ height = 21 }: { height?: number }): JSX.Element {
  return (
    <svg
      width={Math.round((height * W) / H)}
      height={height}
      viewBox={`0 0 ${W} ${H}`}
      fill="none"
      aria-hidden="true"
    >
      <rect y="8.88" width="33.15" height="30.78" rx="7.1" fill="currentColor" />
      <path fill="currentColor" d="M16.8066 0C18.3516 1.71722 19.3264 4.16169 19.3535 6.88184C20.2384 5.60259 20.9061 4.15666 21.2969 2.59766C22.7567 4.12698 23.6816 6.3762 23.6816 8.88574C23.6816 9.08409 23.6735 9.28069 23.6621 9.47559H9.46484C13.1035 8.10172 15.9195 4.49974 16.8066 0Z" />
      <circle cx="25.6358" cy="19.1602" r="2.878" fill="currentColor" />
      <ellipse cx="10.654" cy="20.1277" rx="5.92" ry="5.92" fill="var(--mark-eye, var(--side-bg))" />
      <ellipse cx="24.861" cy="20.1277" rx="5.92" ry="5.92" fill="var(--mark-eye, var(--side-bg))" />
      <circle cx="9.97957" cy="19.4542" r="2.878" fill="currentColor" />
      <circle cx="24.1866" cy="19.4542" r="2.878" fill="currentColor" />
      <path fill="#494949" d="M18.6775 39.0961C18.2783 39.9383 17.0799 39.9383 16.6806 39.0961L10.9968 27.1076C10.3989 25.8465 10.8525 24.3375 12.0466 23.615C15.5093 21.5199 19.8488 21.5199 23.3115 23.615C24.5057 24.3375 24.9592 25.8465 24.3613 27.1076L18.6775 39.0961Z" />
      <path fill="#706E6E" d="M18.8292 40.3666C18.4014 41.2689 17.1174 41.2689 16.6896 40.3666L10.5998 27.5218C9.95921 26.1706 10.4452 24.5538 11.7246 23.7797C15.4347 21.535 20.0841 21.535 23.7942 23.7797C25.0736 24.5538 25.5595 26.1706 24.9189 27.5218L18.8292 40.3666Z" />
    </svg>
  )
}
