/* The glyph for a permission tier, shared by the chip and the popover's rows.
 *
 * One map and one component so the shape on the chip under the composer is the
 * shape the open list shows for the mode it names: a hand for the tier that
 * asks, a bolt for the tier that decides, an open lock for the tier with
 * nothing to decide. The store carries the tier's id; where the drawing lives
 * is this file's business.
 *
 * An id this build does not know draws the served shield rather than a tier's
 * glyph: a map that has not caught up with the store must never present an
 * unknown mode as the least restrictive one.
 */

import { HandIcon, LockOpenIcon, ZapIcon } from '@hugeicons/core-free-icons'

import { Icon } from '../components/Icon'

import type { IconSvgElement } from '@hugeicons/react'
import type { JSX } from 'react'

/** The shield page.html serves before the store has drawn a tier, and the
 *  neutral drawing for an id no tier here names. */
export const SERVED_SHIELD = 'M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z'

const GLYPH: Record<string, IconSvgElement> = {
  ask: HandIcon,
  smart: ZapIcon,
  full: LockOpenIcon,
}

export function PermGlyph({
  id,
  size,
  stroke,
  className,
}: {
  id: string
  size: number
  stroke: number
  className?: string
}): JSX.Element {
  const icon = GLYPH[id]
  if (!icon) {
    return (
      <svg
        className={className}
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={stroke}
        aria-hidden="true"
      >
        <path d={SERVED_SHIELD} />
      </svg>
    )
  }
  return <Icon className={className} icon={icon} size={size} stroke={stroke} />
}
