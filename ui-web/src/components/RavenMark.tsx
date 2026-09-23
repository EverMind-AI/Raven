/* The product mark: the bird on its own rounded tile.
 *
 * The same drawing as assets/raven-mark.svg, which is what the splash wears
 * before any script runs; this is the copy React renders, inline so the rail
 * and the greeting paint it in the same frame as the text beside it.
 *
 * The tile carries its own ground -- a cool-to-warm gradient -- so the mark no
 * longer takes `currentColor` or needs a surface colour cut into its eyes: it
 * reads the same on the light rail and the dark one.
 *
 * The gradient and the clip are referenced by id, and ids are document-wide,
 * so each instance mints its own: the rail and the greeting are both on the
 * page at once.
 *
 * No class names anywhere in it: the fills are attributes, so nothing here can
 * collide with a page class (scripts/check-class-namespace.mjs).
 */

import { useId } from 'react'

import type { JSX } from 'react'

export function RavenMark({ size = 24 }: { size?: number }): JSX.Element {
  /* useId's punctuation is not safe inside `url(#...)`, so keep only the part
     that is. */
  const id = 'rvm' + useId().replace(/[^\w-]/g, '')
  const clip = `${id}c`
  const fill = `${id}g`
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <g clipPath={`url(#${clip})`}>
        <rect width="24" height="24" rx="6.46154" fill={`url(#${fill})`} />
        <rect x="3.00781" y="8.42383" width="18.344" height="16.9329" rx="4.23322" fill="black" />
        <path d="M2.36235 16.4698C2.61143 16.2112 3.01494 16.1809 3.29982 16.3994L4.33016 17.1895C4.76305 17.5215 4.9658 18.0733 4.85087 18.6065L4.09079 22.1331C3.91636 22.9424 3.08589 23.4284 2.29488 23.184L-1.5428 21.9984C-2.04493 21.8433 -2.20728 21.2133 -1.84266 20.8348L2.36235 16.4698Z" fill="black" />
        <path d="M21.8408 16.4698C21.5917 16.2112 21.1882 16.1809 20.9033 16.3994L19.873 17.1895C19.4401 17.5215 19.2373 18.0733 19.3523 18.6065L20.1123 22.1331C20.2868 22.9424 21.1172 23.4284 21.9082 23.184L25.7459 21.9984C26.2481 21.8433 26.4104 21.2133 26.0458 20.8348L21.8408 16.4698Z" fill="black" />
        <path d="M13.0264 2.78125C13.9482 3.80506 14.5302 5.26317 14.5459 6.88574C15.0743 6.12293 15.473 5.26046 15.7061 4.33008C16.5759 5.24146 17.127 6.58172 17.127 8.07715C17.127 8.1957 17.122 8.31321 17.1152 8.42969H8.64844C10.8182 7.61166 12.4974 5.46404 13.0264 2.78125Z" fill="black" />
        <circle cx="18.2854" cy="12.7893" r="1.71509" fill="black" />
        <circle cx="9.2583" cy="13.2681" r="3.43017" fill="white" />
        <circle cx="8.95337" cy="12.9651" r="1.71509" fill="black" />
        <circle cx="17.7231" cy="13.2681" r="3.43017" fill="white" />
        <circle cx="17.4182" cy="12.9651" r="1.71509" fill="black" />
        <path d="M13.5895 26.7715L10.2852 16.5005C10.1494 16.0784 10.2752 15.6159 10.6062 15.3208C12.3062 13.805 14.8728 13.805 16.5728 15.3208C16.9038 15.6159 17.0296 16.0784 16.8938 16.5005L13.5895 26.7715Z" fill="#868686" />
        <rect x="8.01562" y="23.8086" width="2.458" height="2.73111" fill="black" />
        <rect x="14.8438" y="23.8086" width="2.458" height="2.73111" fill="black" />
      </g>
      <defs>
        <linearGradient id={fill} x1="18" y1="-5" x2="12" y2="24" gradientUnits="userSpaceOnUse">
          <stop stopColor="#C6DAF3" />
          <stop offset="1" stopColor="#FFEEBA" />
        </linearGradient>
        <clipPath id={clip}>
          <rect width="24" height="24" rx="6.46154" fill="white" />
        </clipPath>
      </defs>
    </svg>
  )
}
