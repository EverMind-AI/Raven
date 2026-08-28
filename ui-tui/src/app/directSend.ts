// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Sending a prompt to a sub-agent instance from somewhere other than its chat
// view.
//
// The chat stream handle that owns `turn.send` lives in `useMainApp`; the
// Agents Overlay's conversation pane is rendered far from it and holds no
// handle of its own. Bound once, the way `bindInstanceRefresh` binds the rpc,
// so a third caller never has to plumb the handle through.

import type { DirectTargetRef } from './directChatStore.js'

type SendTo = (target: DirectTargetRef, content: string) => Promise<unknown>

let bound: null | SendTo = null
let bindSeq = 0

/** Bind the sender; returns its own unbind (only while this binding is still current). */
export const bindDirectSender = (sendTo: SendTo) => {
  const mine = ++bindSeq
  bound = sendTo

  return () => {
    if (bindSeq === mine) {
      bound = null
    }
  }
}

/** Whether a sender is bound -- false during the pre-attach window. */
export const canSendDirect = () => bound !== null

export const sendDirect = (target: DirectTargetRef, content: string): Promise<unknown> => {
  if (bound === null) {
    return Promise.reject(new Error('chat stream not attached yet'))
  }

  return bound(target, content)
}

/** Test seam. */
export const resetDirectSender = () => {
  bound = null
}
