// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Where Raven keeps everything, resolved the way the Python side resolves it.
//
// `raven_home()` (raven/home.py) reads RAVEN_HOME, strips it, expands a
// leading `~`, and falls back to `~/.raven` when the value is unset or blank.
// A relative value stays relative there, so it is read against the working
// directory -- the same directory `raven tui` hands the TUI it starts as a
// child, so the two name one file.
//
// Three files read the variable: the composer history (lib/history.ts), the
// loop-lag log (lib/loopLag.ts) and the perf pane (lib/perfPane.tsx). Each
// resolved it its own way, so they disagreed with each other as well as with
// the gateway: a blank value sent the history to the working directory while
// the other two fell back to `~/.raven`, and a `~` was joined literally. One
// resolver, so one directory.

import { homedir } from 'node:os'
import { isAbsolute, join, resolve } from 'node:path'

/** The directory Raven keeps its state in, or its default when none is named. */
export function ravenHome(): string {
  const raw = process.env.RAVEN_HOME?.trim()

  // A blank value is unset, not the working directory: `RAVEN_HOME=` in a
  // shell profile means "no home named here", and the Python side reads it
  // that way.
  if (!raw) {
    return join(homedir(), '.raven')
  }

  // `~` and `~/...` expand the way `Path.expanduser()` expands them. A
  // `~name` form is left alone: Python resolves it only when that account
  // exists, and a name that resolves nowhere is not a directory.
  const expanded = raw === '~' ? homedir() : raw.startsWith('~/') ? join(homedir(), raw.slice(2)) : raw

  // Python leaves a relative RAVEN_HOME relative and reads it against the
  // working directory. Resolving it here names that same file.
  return isAbsolute(expanded) ? expanded : resolve(expanded)
}
