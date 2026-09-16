/* Every frame the gateway pushes, and where it lands.
 *
 * The names here are the ones the event pipeline will answer to: one entry for
 * a turn event, one for the `event` envelope that names the subscription it
 * came in on, one for a phase change that may belong to a conversation the
 * reader is not looking at, and one per side-channel request. Behind each one
 * today is the page's 23-branch dispatcher and its notification handlers, so
 * every export below is a forward, and the behaviour is exactly the behaviour
 * of the function it forwards to.
 *
 * It exists ahead of that pipeline so the characterisation tests can be
 * written against the surface the rewrite will keep: the tests move once,
 * here, and the implementation moves under them afterwards without them
 * changing.
 */

import type { TurnEvent } from '../../features/composer/turn'
import { onEvent } from '../../legacy/live/050-turn.js'
import {
  notifyTurn,
  onApprovalClosed,
  onApprovalRequest,
  onClarifyClosed,
  onClarifyRequest,
  onConfirmRequest,
  onStreamEvent,
} from '../../legacy/live/070-notify.js'

/** One turn event, for the conversation this page is showing. */
export const dispatch = (ev: unknown): void => onEvent(ev)

/** The `event` envelope: a turn event plus the subscription it arrived on. */
export const stream = (params: unknown): void => onStreamEvent(params)

/** A phase change for one conversation, and the paint it is owed. */
export const notify = (owner: string, event: TurnEvent): void => notifyTurn(owner, event)

/** The engine asks a yes-or-no question. */
export const confirmRequest = (p: unknown): void => onConfirmRequest(p)

/** The permission gate asks to run something. */
export const approvalRequest = (p: unknown): void => onApprovalRequest(p)

/** Nobody answered the permission gate, or somebody did. */
export const approvalClosed = (p: unknown): void => onApprovalClosed(p)

/** The agent asks the reader a question mid-turn. */
export const clarifyRequest = (p: unknown): void => onClarifyRequest(p)

/** That question is over, answered or not. */
export const clarifyClosed = (p: unknown): void => onClarifyClosed(p)
