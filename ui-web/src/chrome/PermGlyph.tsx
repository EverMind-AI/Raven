/* The glyph for a permission tier, shared by the chip and the popover's rows.
 *
 * One map and one component so the shape on the chip under the composer is the
 * shape the open list shows for the mode it names: a hand for the tier that
 * asks, a bolt for the tier that decides, an open lock for the tier with
 * nothing to decide. The store carries the tier's id; where the drawing lives
 * is this file's business.
 */

import { HandIcon, LockOpenIcon, ZapIcon } from '@hugeicons/core-free-icons'

import { Icon } from '../components/Icon'

import type { IconSvgElement } from '@hugeicons/react'
import type { JSX } from 'react'

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
  return <Icon className={className} icon={GLYPH[id] || GLYPH.full!} size={size} stroke={stroke} />
}
