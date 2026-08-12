// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import type { RunExternalProcess } from '@hermes/ink'

import { withInkSuspended } from '@hermes/ink'

import { deferSignalExit } from './gracefulExit.js'

/**
 * Hand the terminal to a child process for as long as it runs.
 *
 * Suspending Ink is only half of it. The child shares this process group, so
 * Ctrl-C reaches both, and the signal handler here would exit the whole session
 * while the child is the thing the user meant to interrupt -- a device-code
 * sign-in can sit there for minutes waiting for a browser. While the child owns
 * the terminal, the signal belongs to the child.
 */
export async function suspendForHandoff(run: RunExternalProcess): Promise<void> {
  const restore = deferSignalExit()

  try {
    await withInkSuspended(run)
  } finally {
    restore()
  }
}
