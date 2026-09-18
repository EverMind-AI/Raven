/* The offline library: one wire answer per method the page asks for.
 *
 * This is what `?stub=1` and a page opened from disk run on. It replaced a
 * second data layer -- twenty fixture sources on the seam, each in its
 * island's own shape -- with responses in the contract's shape behind the one
 * transport, so the offline page and the
 * live page are the same program with a different socket. A field the contract
 * requires and a fixture omits is a compile error here, and a page that draws
 * the wrong thing offline is drawing the wrong thing live.
 *
 * That first claim was false for seven of these responders, which built their
 * answers behind an `as` -- and an assertion switches off the whole check, not
 * the one field it was reached for. They are annotated now, and what an
 * offline answer is held to is `Wire<ResultOf<M>>` (../fixtureTransport.ts):
 * the contract exactly, but with the nulls a gateway really sends where the
 * contract leaves a field optional. The two halves of "optional" that tsc
 * cannot see -- a field the contract declares and nothing here ever sends --
 * are pinned in scripts/gates/fixture-shape.test.mjs.
 *
 * Every time value comes from the injected clock, so two passes over the
 * library answer byte-identically
 * (ui-web/scripts/gates/fixture-now.test.mjs), and the scripted conversations
 * push their frames on the injected timer.
 */

import { createBrowser } from './browser'
import { createChannels } from './channels'
import { createCron } from './cron'
import { createExt } from './ext'
import { createFs } from './fs'
import { createKnowledge } from './knowledge'
import { createMemory } from './memory'
import { createModel } from './model'
import { createPlaybooks } from './playbooks'
import { createPlughub } from './plughub'
import { createSessions } from './sessions'
import { createSettings } from './settings'
import { createSkillhub } from './skillhub'
import { createSubagents } from './subagents'
import { createTasks } from './tasks'
import { RUNS, createTurn } from './turn'

import type { FixtureEnv, Fixtures } from '../fixtureTransport'

/* Built in dependency order, and only the cross-domain reads are handed over:
   the two hubs and the settings answer share the inventory `ext.list` holds,
   and the turn needs to know which script a conversation is running. */
export function demoFixtures(env: FixtureEnv): Fixtures {
  const ext = createExt(env)
  /* The turn and the session list each need the other -- a resume reads the
     script, a send records which one is playing -- so the turn is reached
     through a getter rather than by construction order. */
  let turn: ReturnType<typeof createTurn> | null = null
  const sessions = createSessions(env, () => turn!)
  turn = createTurn(env, {
    runOf: (id) => {
      const key = sessions.runOf(id)
      return (key ? RUNS[key] : null) ?? null
    },
    setRun: (id, key) => sessions.setRun(id, key),
    finished: (id, preview) => sessions.setPreview(id, preview),
  }, ext.websearchOn)

  return {
    ...createSettings(env, ext).fixtures,
    ...createModel(env).fixtures,
    ...sessions.fixtures,
    ...turn.fixtures,
    ...createCron(env).fixtures,
    ...createChannels(env).fixtures,
    ...ext.fixtures,
    ...createSkillhub(env, ext).fixtures,
    ...createPlughub(env, ext).fixtures,
    ...createPlaybooks(env).fixtures,
    ...createSubagents(env).fixtures,
    ...createTasks(env).fixtures,
    ...createMemory(env).fixtures,
    ...createFs(env).fixtures,
    ...createBrowser(env).fixtures,
    ...createKnowledge(env).fixtures,
  }
}

export { deskDemoOverrides } from './subagents'
export { onboardDemoOverrides } from './model'
