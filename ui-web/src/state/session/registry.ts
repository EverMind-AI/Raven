/* Which conversation the page is on, and what becomes of the one it leaves.
 *
 * The names here are the ones the session registry will answer to: a switch
 * takes a conversation, a leave parks the turn that was running, a frame that
 * names a subscription finds the conversation it belongs to, and a deletion
 * forgets one. Behind each name today is the page's own override layer, which
 * still holds the two subscription books, the parked-turn map and the `draft`
 * flag -- so every export below is a forward, and the behaviour is exactly the
 * behaviour of the function it forwards to.
 *
 * It exists ahead of that registry so the characterisation tests can be
 * written against the surface the rewrite will keep: the tests move once, here,
 * and the implementation moves under them afterwards without them changing.
 */

import type { TurnEvent } from '../../features/composer/turn'
import type { SessRow } from '../../features/rail/types'
import { touchSession as legacyTouchSession } from '../../legacy/live/030-sessions.js'
import { refreshList as legacyRefreshList } from '../../legacy/live/050-turn.js'
import { parkTurn, parkedTurns, restoreTurn, subSession, transitionTurn } from '../../legacy/live/060-parked.js'
import {
  forgetSubscription,
  openLiveSession,
  startDraft,
  subscribe as legacySubscribe,
} from '../../legacy/live/080-overrides.js'

/** The conversation a reader asked for becomes the one on screen. */
export const switchTo = (row: SessRow): Promise<void> => openLiveSession(row)

/** The new-task screen: a conversation that does not exist yet. */
export const switchToDraft = (): void => startDraft()

/** One stream per conversation per socket, claimed by the one on screen. */
export const subscribe = (key: string): Promise<void> => legacySubscribe(key)

/** Which conversation a frame that names a subscription belongs to. */
export const bySubscription = (id: string): string | undefined =>
  (subSession as Record<string, string>)[id]

/** Stop reading a conversation's stream, and forget that it had one. */
export const forget = (key: string): void => forgetSubscription(key)

/** Whether the conversation still holds its turn while it is off screen. */
export const isResident = (key: string): boolean => parkedTurns.has(key)

/** The turn a conversation kept while it was off screen. */
export const parked = (key: string): unknown => parkedTurns.get(key)

/** Leaving: the running turn goes with the conversation it belongs to. */
export const park = (): void => parkTurn()

/** Arriving: the turn that was kept comes back in place of a disk read. */
export const resume = (snapshot: unknown): void => restoreTurn(snapshot)

/** A phase event for one conversation, whether or not it is on screen. */
export const dispatchTo = (key: string, event: TurnEvent): void => transitionTurn(key, event)

/** A conversation's row moves to the top, under what it just said. */
export const touch = (key: string, preview?: string): void => legacyTouchSession(key, preview)

/** Read the rail back from the server. */
export const refreshList = (): Promise<void> => legacyRefreshList()
