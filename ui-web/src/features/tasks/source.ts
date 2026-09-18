/* -- tasks: the source -------------------------------------------------
   The tasks island talks to sources.tasks and knows nothing about where the
   rows came from; this module is the two answers there are.

   There is no `tasks.*` method on the contract, so unlike every other domain
   here this one has no gateway call to make. A page served by a raven gets the
   empty list -- the honest answer for a panel whose server side has not landed
   -- and a page with no gateway gets the stand-in rows, so the surface can be
   opened and reviewed offline. Which one a page installs is the wiring's
   decision (src/app/install.ts), the same place that decides every other seam.

   When `tasks.list` lands on the contract, `tasksSource` becomes a gateway call
   like its neighbours and `fixtureTasksSource` goes with ./fixtures.ts. */

import { TASK_FIXTURES } from './fixtures'

import type { TasksSource } from './types'

export const tasksSource: TasksSource = {
  list: () => Promise.resolve([]),
}

/* A copy per read: the panel holds what it is given, and a fixture handed out
   by reference would carry one reader's edits into the next session's list. */
export const fixtureTasksSource: TasksSource = {
  list: () => Promise.resolve(TASK_FIXTURES.map((t) => ({ ...t }))),
}
