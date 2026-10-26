/* The transcript's one root per page, mounted into `#turnNav`
 * (features/transcript/manifest.ts).
 *
 * The transcript itself is not drawn from here: it is one root per
 * conversation lane (mount.tsx), since a lane parked off the stage keeps its
 * host. What the page holds once is the turn nav beside it (TurnNav.tsx),
 * which reads whichever lane is on the stage. The language subscription is
 * here, so a pick redraws the nav's words through this root.
 */
import { useSyncExternalStore } from 'react'

import * as lang from '../../state/lang'
import { TurnNav } from './TurnNav'

import type { JSX } from 'react'

export function TranscriptApp(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return <TurnNav />
}
