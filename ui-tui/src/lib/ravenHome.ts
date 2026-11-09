// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The one RAVEN_HOME reader for the TUI. history.ts, loopLag.ts and perfPane.tsx
// resolve their state directory through ravenHomeDir(), which trims the value
// and expands only a bare ~, ~/ and ~\ like the host does. Any other
// leading-tilde form, such as ~name, stays a relative value even though the
// host expands it; a relative value names a directory under each process's own
// working directory, so two front ends may disagree about it -- prefer an
// absolute value (the contract lives in using-raven.md).

import { homedir } from 'node:os'
import { join } from 'node:path'

export function ravenHomeDir(): string {
  const value = (process.env.RAVEN_HOME ?? '').trim()

  if (!value) {
    return join(homedir(), '.raven')
  }

  if (value === '~') {
    return homedir()
  }

  if (value.startsWith('~/') || value.startsWith('~\\')) {
    return join(homedir(), value.slice(2))
  }

  return value
}
