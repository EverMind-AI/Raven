/* One conversation's turn: its clock, its name, its sends and the things a
 * reader can do to it.
 *
 * The names here are the ones a `SessionRuntime` will answer to. Behind each
 * one today is the page's turn layer -- a single page-level `live` object, the
 * naming timers keyed by session id, and the actions the rail and the slash
 * palette install onto the source seam -- so every export below is a forward,
 * and the behaviour is exactly the behaviour of the function it forwards to.
 *
 * It exists ahead of that class so the characterisation tests can be written
 * against the surface the rewrite will keep: the tests move once, here, and
 * the implementation moves under them afterwards without them changing.
 */

import type { SessRow } from '../../features/rail/types'
import { compressNow } from '../../legacy/live/190-session-actions.js'
import {
  beginNaming as legacyBeginNaming,
  finishTurn as legacyFinishTurn,
  live,
  namingDeclined as legacyNamingDeclined,
  namingEnded as legacyNamingEnded,
  resetTurnState,
  settleNaming as legacySettleNaming,
  turnDur,
} from '../../legacy/live/050-turn.js'
import {
  drainQueue,
  liveSend,
  openConversation as legacyOpenConversation,
  sendOnSession as legacySendOnSession,
  softStop as legacySoftStop,
  draft,
} from '../../legacy/live/080-overrides.js'
import { sources } from '../sources'

/* ---- the turn's own state ---------------------------------------------- */

/** The open step, the streamed say buffer, the calls in flight, the stamps. */
export const state = (): typeof live => live

/** Back to a conversation with no turn running. */
export const reset = (): void => resetTurnState()

/* ---- the clock -------------------------------------------------------- */

/** How long the turn took: the runtime's number, or this page's stamps. */
export const duration = (serverMs?: number): string | null => turnDur(serverMs)

/** The turn folds: the prose becomes the answer and the queue drains. */
export const finishTurn = (payload: unknown): void => legacyFinishTurn(payload)

/* ---- sending ---------------------------------------------------------- */

/** Send, or queue behind the turn that is running. */
export const send = (text: string): void => liveSend(text)

/** The stop button: cancel the turn the reader started. */
export const stop = (): void => sources.composer?.stop()

/** Fold the visible turn without waiting for the server to finish it. */
export const softStop = (keepCancelling?: boolean): void => legacySoftStop(keepCancelling)

/** The queue moves when the engine comes free. */
export const drain = (): void => drainQueue()

/** A conversation to work in, made if there is not one yet. */
export const openConversation = (
  preview?: string,
  atPointer?: (id: string) => void,
): Promise<string | null> => legacyOpenConversation(preview, atPointer)

/** A send onto a conversation that is still being made. */
export const sendOnSession = (text: string, failed: (e: unknown) => void): void =>
  legacySendOnSession(text, failed)

/** Whether the page is on a draft rather than in a conversation. */
export const isDraft = (): boolean => draft

/* ---- naming ----------------------------------------------------------- */

/** Start the wait for a title, on the opening line this text is. */
export const beginNaming = (text: string): void => legacyBeginNaming(text)

/** Stop waiting, and show this title or what the row already had. */
export const settleNaming = (id: string, title?: string | null): void => legacySettleNaming(id, title)

/** No title is coming: settle onto the captured opening line. */
export const namingDeclined = (id: string): void => legacyNamingDeclined(id)

/** What a `session.naming_ended` reason means for the row. */
export const namingEnded = (id: string, reason: string): void | Promise<void> =>
  legacyNamingEnded(id, reason)

/* ---- what a reader can do to a conversation ---------------------------- */

/** Fork it, and open the fork. */
export const branch = (text = ''): void => sources.transcript?.branch?.(text)

/** Empty its transcript, from the slash palette. */
export const clear = (): void => sources.composer?.slash.find((x) => x.id === 'gui.clear')?.fn()

/** Compact it now, rather than when the window fills. */
export const compress = (): Promise<void> => compressNow()

/** Delete it, transcript and all. */
export const remove = (s: SessRow): void => sources.sessions?.remove?.(s)

/** Hide it from the rail, with an undo. */
export const archive = (s: SessRow): void => sources.sessions?.archive?.(s)

/** Pin it to the top of the rail. */
export const pin = (id: string, pinned: boolean): void => sources.sessions?.pin?.(id, pinned)

/** Persist a title the reader typed. */
export const rename = (id: string, title: string, previous: string): void =>
  sources.sessions?.renamed?.(id, title, previous)

/** Delete every conversation, from the settings page. */
export const deleteAll = (): void => sources.sessions?.deleteAll?.()
