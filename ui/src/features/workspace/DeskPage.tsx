/** Floating workspace composition and public compatibility entry points. */

import { useEffect, useSyncExternalStore } from 'react'

import { DeskPalette } from './DeskPalette'
import { DeskFollowToggle, DeskSurface } from './DeskSurface'
import * as workspace from './store'

import type { JSX } from 'react'

export {
  notifyDesk,
  openDeskAgent,
  openDeskAgentRecord,
  openDeskDiff,
  openDeskFile,
  openDeskTab,
  reset,
  toggleDesk,
} from './deskStore'

export function DeskApp(): JSX.Element {
  useSyncExternalStore(workspace.subscribe, workspace.getState)
  useEffect(() => {
    document.documentElement.classList.add('desk-ready')
    return () => document.documentElement.classList.remove('desk-ready')
  }, [])
  return <><DeskPalette /><DeskFollowToggle /><DeskSurface /></>
}
