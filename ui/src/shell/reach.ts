/* Localized labels for the fixed execution-reach vocabulary. */

import { t } from './bridge'

const KEYS: Record<string, string> = {
  local: 'gui.reach.local',
  net: 'gui.reach.net',
  auth: 'gui.reach.auth',
}

const key = (reach: string): string => KEYS[reach] ?? 'gui.reach.local'

export const text = (reach: string): string => t(key(reach))

export const hint = (reach: string): string => t(key(reach) + '_hint')
