/* Which way an import may point, and every edge that points the other way.
 *
 * The page has one shape, read from the entry down:
 *
 *   lib <- components <- chrome <- app
 *   lib <- rpc <- state <- chrome, features, app
 *   lib <- features <- app
 *   features -/-> features   (a sibling is reached through its source.ts or
 *                             types.ts, which is the domain's public surface)
 *   state -/-> features      (at runtime; a type import erases, and a call the
 *                             other way is a registered callback --
 *                             state/page.ts's onShow)
 *
 * Nothing enforced it before this file. `tsc` reports neither direction nor
 * cycles, so every new upward edge was free, and the largest single one --
 * state/page.ts reaching the island bag -- put every store that opens a page in
 * the closure of every other, 87 modules deep, with a module-eval order that
 * threw in one test's graph.
 *
 * A ratchet in the house style (see state-dom-touch.test.mjs): PINNED is every
 * upward edge the tree holds today and CROSS every cross-domain one, and both
 * may only shrink. A new one fails here -- and the fix is to invert the call
 * (a registered callback, or the sibling's source.ts), not to add a line below.
 *
 * Read from the source text, so it sees what a reader sees: relative
 * specifiers resolved the way Vite resolves them, `import type` and
 * `import { type X }` dropped (they erase), tests excluded -- a case is an
 * entry point and may reach anything.
 *
 * The cycles are pinned the same way: every strongly connected component of
 * the runtime graph has to be a subset of one listed in CYCLES, and no more
 * files may be inside one than are today. Two pairs stood beside the two left,
 * and each dissolved the same way an upward edge does: the call that closed it
 * is registered by the module that owns the answer rather than imported back.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, posix } from 'node:path'
import { describe, expect, it } from 'vitest'

import { relPath, root } from './paths.mjs'

const SRC = root(new URL('../../src/', import.meta.url))

/* One rank per directory, lowest first: an import may point at its own rank or
   at a higher number, never back.

   Two files at the features level are not domains and are ranked as what they
   are. `features/manifests.ts` is the assembly point -- every domain declares
   into it, so it sits ABOVE them and a domain reading it is an upward edge,
   which is what keeps every island out of every island's closure.
   `features/hosts.ts` sits below them because it is a leaf with no imports of
   its own (EXEMPT says why). */
const RANK = {
  main: 0,
  app: 1,
  App: 2,
  chrome: 3,
  'features/manifests': 3.5,
  features: 4,
  'features/hosts': 5,
  components: 6,
  state: 7,
  rpc: 8,
  lib: 9,
  i18n: 9,
}

/* Allowed for the life of the file rather than pinned as debt, one reason each. */
const EXEMPT = {
  'features/hosts.ts':
    'the three detached island hosts: a leaf with no imports of its own, so a '
    + 'domain reaching it can neither pull a graph in nor close a cycle',
}

/* Every upward runtime edge in the tree today, as `importer -> target`, both
   paths from src/. Down or gone: an edge may disappear, and a new one fails. */
const PINNED = [
  'features/composer/AskApproveSheet.tsx -> chrome/SheetRack.tsx',
  'features/composer/ClarifySheet.tsx -> chrome/SheetRack.tsx',
  'features/composer/GateSheet.tsx -> chrome/SheetRack.tsx',
  'features/settings/wire.ts -> app/updates.ts',
  'lib/clipboard.ts -> state/toast.ts',
  'lib/openUrl.ts -> state/toast.ts',
  'lib/platform.ts -> state/lang/index.ts',
  'lib/prose.ts -> state/sources.ts',
  'rpc/fixtures/channels.ts -> features/connections/catalogue.ts',
  'state/escapeOrder.ts -> features/composer/turn.ts',
  'state/escapeOrder.ts -> features/connections/store.ts',
  'state/escapeOrder.ts -> features/cron/store.ts',
  'state/escapeOrder.ts -> features/knowledge/store.ts',
  'state/escapeOrder.ts -> features/memory/store.ts',
  'state/escapeOrder.ts -> features/playbooks/store.ts',
  'state/escapeOrder.ts -> features/extAgents/store.ts',
  'state/find.ts -> features/composer/store.ts',
  'state/globalListeners.ts -> app/boot.ts',
  'state/globalListeners.ts -> app/updates.ts',
  'state/globalListeners.ts -> chrome/behaviour/panes.ts',
  'state/globalListeners.ts -> chrome/behaviour/scrollbars.ts',
  'state/globalListeners.ts -> features/browser/store.ts',
  'state/globalListeners.ts -> features/composer/mount.tsx',
  'state/globalListeners.ts -> features/composer/store.ts',
  'state/globalListeners.ts -> features/settings/store.ts',
  'state/lang/effects.ts -> features/composer/mount.tsx',
  'state/lang/effects.ts -> features/model/chip.ts',
  'state/lang/effects.ts -> features/rail/store.ts',
  'state/lang/effects.ts -> features/settings/store.ts',
  'state/lang/effects.ts -> features/transcript/mount.tsx',
  'state/navfly.ts -> features/connections/wire.ts',
  'state/navfly.ts -> features/cron/store.ts',
  'state/navfly.ts -> features/extAgents/store.ts',
  'state/session/conversation.ts -> features/transcript/mount.tsx',
  'state/session/conversation.ts -> features/transcript/tail.ts',
  'state/session/pipeline.ts -> features/composer/approve.ts',
  'state/session/pipeline.ts -> features/composer/clarify.ts',
  'state/session/pipeline.ts -> features/composer/mount.tsx',
  'state/session/pipeline.ts -> features/rail/source.ts',
  'state/session/pipeline.ts -> features/rail/store.ts',
  'state/session/naming.ts -> features/rail/store.ts',
  'state/session/naming.ts -> features/rail/title.ts',
  'state/session/registry.ts -> features/composer/mount.tsx',
  'state/session/registry.ts -> features/model/source.ts',
  'state/session/registry.ts -> features/rail/wire.ts',
  'state/session/registry.ts -> features/rail/source.ts',
  'state/session/registry.ts -> features/rail/store.ts',
  'state/session/registry.ts -> features/rail/title.ts',
  'state/session/registry.ts -> features/settings/source.ts',
  'state/session/registry.ts -> features/transcript/mount.tsx',
  'state/session/registry.ts -> features/transcript/source.ts',
  'state/session/registry.ts -> features/workspace/record.ts',
  'state/session/registry.ts -> features/workspace/source.ts',
  'state/session/registry.ts -> features/workspace/store.ts',
  'state/session/resume.ts -> features/dag/mount.tsx',
  'state/session/resume.ts -> features/desk/store.ts',
  'state/session/resume.ts -> features/subagents/store.ts',
  'state/session/residency.ts -> features/composer/mount.tsx',
  'state/session/residency.ts -> features/rail/store.ts',
  'state/session/residency.ts -> features/transcript/mount.tsx',
  'state/session/residency.ts -> features/transcript/tail.ts',
  'state/session/residency.ts -> features/workspace/store.ts',
  'state/session/runtime.ts -> features/composer/mount.tsx',
  'state/session/runtime.ts -> features/desk/store.ts',
  'state/session/runtime.ts -> features/composer/turn.ts',
  'state/session/runtime.ts -> features/model/source.ts',
  'state/session/runtime.ts -> features/rail/source.ts',
  'state/session/runtime.ts -> features/rail/store.ts',
  'state/session/runtime.ts -> features/rail/title.ts',
  'state/session/runtime.ts -> features/settings/source.ts',
  'state/session/runtime.ts -> features/transcript/mount.tsx',
  'state/session/runtime.ts -> features/transcript/tail.ts',
  'state/session/runtime.ts -> features/workspace/source.ts',
  'state/session/runtime.ts -> features/workspace/store.ts',
  'state/session/stages.ts -> features/composer/mount.tsx',
  'state/session/stages.ts -> features/dag/mount.tsx',
  'state/session/stages.ts -> features/dag/nodes.ts',
  'state/session/stages.ts -> features/rail/source.ts',
  'state/session/stages.ts -> features/rail/store.ts',
  'state/session/stages.ts -> features/subagents/store.ts',
  'state/session/stages.ts -> features/transcript/mount.tsx',
  'state/session/stages.ts -> features/transcript/source.ts',
  'state/session/stages.ts -> features/workspace/record.ts',
  'state/session/stages.ts -> features/workspace/store.ts',
  'state/settings.ts -> features/rail/store.ts',
  'state/sheetRack.ts -> features/composer/store.ts',
  'state/ws.ts -> features/desk/store.ts',
  'state/ws.ts -> features/subagents/store.ts',
  'state/ws.ts -> features/workspace/store.ts',
]

/* Every cross-domain runtime edge into something that is not the domain's
   public surface. Same ratchet: the way off this list is a source.ts verb or a
   registered callback, never another line.

   Eight of the features/desk rows below were inside one directory until the
   desk became a domain of its own: what it shows is the workspace's record, so
   it reads that store and its delivery registry directly. They are pinned as
   the debt they now are -- the desk's own source.ts, reading fs.* the way the
   workspace's does, is what takes them off. */
const CROSS = [
  'features/browser/source.ts -> features/workspace/store.ts',
  'features/composer/store.ts -> features/transcript/mount.tsx',
  'features/composer/store.ts -> features/transcript/tail.ts',
  'features/cron/source.ts -> features/rail/store.ts',
  'features/dag/DagSheet.tsx -> features/subagents/store.ts',
  'features/dag/open.ts -> features/subagents/store.ts',
  'features/desk/DeskApp.tsx -> features/subagents/store.ts',
  'features/desk/DeskApp.tsx -> features/workspace/store.ts',
  'features/desk/DeskPalette.tsx -> features/tasks/TasksPage.tsx',
  'features/desk/DeskPalette.tsx -> features/tasks/store.ts',
  'features/desk/DeskPalette.tsx -> features/workspace/deliveries.ts',
  'features/desk/DeskPalette.tsx -> features/workspace/store.ts',
  'features/desk/DeskSurface.tsx -> features/subagents/InstanceMode.tsx',
  'features/desk/DeskSurface.tsx -> features/subagents/InstanceModel.tsx',
  'features/desk/DeskSurface.tsx -> features/subagents/SubagentsPage.tsx',
  'features/desk/DeskSurface.tsx -> features/subagents/TurnClock.tsx',
  'features/desk/DeskSurface.tsx -> features/subagents/store.ts',
  'features/desk/DeskSurface.tsx -> features/tasks/TasksPage.tsx',
  'features/desk/DeskSurface.tsx -> features/workspace/WorkspacePage.tsx',
  'features/desk/DeskSurface.tsx -> features/workspace/deliveries.ts',
  'features/desk/DeskSurface.tsx -> features/workspace/store.ts',
  'features/desk/store.ts -> features/subagents/history.ts',
  'features/desk/store.ts -> features/subagents/store.ts',
  'features/desk/store.ts -> features/tasks/store.ts',
  'features/desk/store.ts -> features/workspace/deliveries.ts',
  'features/desk/store.ts -> features/workspace/store.ts',
  'features/installed/source.ts -> features/plugins/store.ts',
  'features/knowledge/KnowledgePage.tsx -> features/settings/store.ts',
  'features/model/source.ts -> features/settings/store.ts',
  'features/playbooks/PlaybooksPage.tsx -> features/dag/Board.tsx',
  'features/playbooks/PlaybooksPage.tsx -> features/dag/graph.ts',
  'features/playbooks/shape.ts -> features/dag/graph.ts',
  'features/tasks/TasksPage.tsx -> features/dag/Board.tsx',
  'features/tasks/TasksPage.tsx -> features/dag/DagGraph.tsx',
  'features/tasks/TasksPage.tsx -> features/dag/graph.ts',
  'features/tasks/TasksPage.tsx -> features/desk/store.ts',
  'features/tasks/TasksPage.tsx -> features/workspace/store.ts',
  'features/plugins/PluginsPage.tsx -> features/composer/startTaskWith.ts',
  'features/plugins/wire.ts -> features/skills/wire.ts',
  'features/rail/RailPage.tsx -> features/cron/store.ts',
  'features/rail/wire.ts -> features/composer/mount.tsx',
  'features/rail/wire.ts -> features/dag/mount.tsx',
  'features/rail/wire.ts -> features/settings/store.ts',
  'features/rail/source.ts -> features/composer/turn.ts',
  'features/rail/store.ts -> features/composer/store.ts',
  'features/settings/wire.ts -> features/model/chip.ts',
  'features/skills/SkillsPage.tsx -> features/composer/startTaskWith.ts',
  'features/subagents/SubagentsPage.tsx -> features/composer/store.ts',
  'features/subagents/source.ts -> features/transcript/mount.tsx',
  'features/subagents/store.ts -> features/rail/title.ts',
  'features/transcript/TranscriptPage.tsx -> features/dag/DagGraph.tsx',
  'features/transcript/TranscriptPage.tsx -> features/dag/graph.ts',
  'features/transcript/TranscriptPage.tsx -> features/workspace/deliveries.ts',
  'features/transcript/TranscriptPage.tsx -> features/workspace/store.ts',
  'features/transcript/source.ts -> features/dag/mount.tsx',
  'features/transcript/source.ts -> features/dag/open.ts',
  'features/transcript/source.ts -> features/rail/store.ts',
  'features/transcript/source.ts -> features/rail/title.ts',
  'features/transcript/source.ts -> features/subagents/store.ts',
  'features/transcript/source.ts -> features/desk/store.ts',
  'features/transcript/store.ts -> features/dag/nodes.ts',
  'features/transcript/store.ts -> features/workspace/deliveries.ts',
  'features/transcript/store.ts -> features/workspace/hunks.ts',
  'features/workspace/store.ts -> features/browser/mount.tsx',
  'features/workspace/store.ts -> features/subagents/mount.tsx',
]

/* The runtime cycles, members and all. Every strongly connected component has
   to be a subset of one of these, and the files inside one may only get fewer:
   a cycle that breaks into two smaller ones is an improvement, a new knot is
   not. Two pairs stood here as well -- state/caps.ts with state/page.ts, and
   state/navfly.ts with the rail's store -- both of them inside one
   fifteen-module component while state/page.ts reached the island bag, and
   neither module-eval-safe until the one call back was registered instead. */
const CYCLES = [
  [
    'features/composer/store.ts',
    'features/subagents/SubagentsPage.tsx',
    'features/subagents/mount.tsx',
    'features/transcript/TranscriptPage.tsx',
    'features/transcript/mount.tsx',
    'features/workspace/store.ts',
  ],
  [
    'features/model/source.ts',
    'features/rail/wire.ts',
    'features/rail/source.ts',
    'features/settings/source.ts',
    'state/session/generation.ts',
    'state/session/naming.ts',
    'state/session/registry.ts',
    'state/session/residency.ts',
    'state/session/runtime.ts',
    'state/session/stages.ts',
    'state/session/staging.ts',
  ],
]

/* How many files sit inside a runtime cycle today. Down or equal, with one
   exception on the record: naming.ts was carved OUT of runtime.ts, which was
   already a member of the session component above, so the count rose by one
   without a new knot or a new edge between modules -- the same cycle, one more
   file inside it. The way back down is to invert the two calls runtime.ts
   makes into it (beginNaming, namingDeclined), not another file on the list. */
const IN_CYCLES = 17

const TEST = (rel) => rel.includes('.test.') || rel.startsWith('test/')

function* sources(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) yield* sources(path)
    else if (/\.tsx?$/.test(name)) yield path
  }
}

/** Every non-test module, by its path from src/. */
const modules = [...sources(SRC)].map((path) => relPath(SRC, path)).filter((rel) => !TEST(rel)).sort()
const known = new Set(modules)

/* The bucket a module belongs to, which is what carries the rank. */
function bucket(rel) {
  if (rel === 'main.tsx') return 'main'
  if (rel === 'App.tsx') return 'App'
  if (rel === 'features/hosts.ts') return 'features/hosts'
  if (rel === 'features/manifests.ts') return 'features/manifests'
  const dir = rel.split('/')[0]
  if (dir === 'features') return `features/${rel.split('/')[1]}`
  return dir
}

/* A domain's bucket is `features/<domain>`, which the table does not list; the
   two files that are not domains are listed by name. */
const rankOf = (name) => (name in RANK ? RANK[name] : RANK.features)

/* Vite's own resolution, for the specifiers that name a module in this tree:
   the file itself, a .ts/.tsx extension, or a directory's index. */
function target(from, spec) {
  if (!spec.startsWith('.')) return null
  const base = posix.join(posix.dirname(from), spec)
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`, posix.join(base, 'index.ts'), posix.join(base, 'index.tsx')]) {
    const rel = candidate.replace(/^\.\//, '')
    if (known.has(rel)) return rel
  }
  return null
}

/* `import type {...}` and `import { type A, type B }` are erased by the
   compiler, so neither is an edge at runtime; one plain binding among the
   types is. */
function typeOnly(clause) {
  const text = clause.trim()
  if (!text) return false
  if (/^type[\s{]/.test(text)) return true
  const braced = text.match(/\{([\s\S]*)\}/)
  if (!braced || text.replace(/\{[\s\S]*\}/, '').replace(/,/g, '').trim()) return false
  const names = braced[1].split(',').map((name) => name.trim()).filter(Boolean)
  return names.length > 0 && names.every((name) => /^type\s/.test(name))
}

const SPECIFIER = /(?:^|\n)\s*(?:import|export)\s+([\s\S]*?)from\s+'([^']+)'|(?:^|\n)\s*import\s+'([^']+)'/g

/** Every runtime edge in the tree, deduplicated: two statements are one edge. */
function graph() {
  const edges = new Set()
  for (const rel of modules) {
    const text = readFileSync(join(SRC, rel), 'utf8')
    for (const match of text.matchAll(SPECIFIER)) {
      const clause = match[1] ?? ''
      const spec = match[2] ?? match[3]
      const to = target(rel, spec)
      if (!to || typeOnly(clause)) continue
      edges.add(`${rel} -> ${to}`)
    }
  }
  return [...edges].sort()
}

const split = (edge) => edge.split(' -> ')

/** Tarjan, over the runtime edges: every component of more than one file. */
function cycles(edges) {
  const out = new Map(modules.map((rel) => [rel, []]))
  for (const edge of edges) {
    const [from, to] = split(edge)
    out.get(from).push(to)
  }
  const index = new Map()
  const low = new Map()
  const stack = []
  const open = new Set()
  const found = []
  let next = 0
  const walk = (v) => {
    index.set(v, next)
    low.set(v, next)
    next += 1
    stack.push(v)
    open.add(v)
    for (const w of out.get(v) ?? []) {
      if (!index.has(w)) {
        walk(w)
        low.set(v, Math.min(low.get(v), low.get(w)))
      } else if (open.has(w)) {
        low.set(v, Math.min(low.get(v), index.get(w)))
      }
    }
    if (low.get(v) !== index.get(v)) return
    const members = []
    for (;;) {
      const w = stack.pop()
      open.delete(w)
      members.push(w)
      if (w === v) break
    }
    if (members.length > 1) found.push(members.sort())
  }
  for (const rel of modules) if (!index.has(rel)) walk(rel)
  return found
}

const edges = graph()

/* The two kinds of violation, read off the ranks. */
function violations() {
  const up = []
  const cross = []
  for (const edge of edges) {
    const [from, to] = split(edge)
    if (EXEMPT[from] || EXEMPT[to]) continue
    const here = bucket(from)
    const there = bucket(to)
    if (here === there) continue
    if (here.startsWith('features/') && there.startsWith('features/')
      && there !== 'features/hosts' && there !== 'features/manifests'
      && here !== 'features/manifests') {
      const file = to.split('/')[2]
      if (file !== 'source.ts' && file !== 'types.ts') cross.push(edge)
      continue
    }
    if (rankOf(there) < rankOf(here)) up.push(edge)
  }
  return { up, cross }
}

describe('which way the imports point', () => {
  it('adds no upward edge to the ones the tree already holds', () => {
    const { up } = violations()
    expect(up.filter((edge) => !PINNED.includes(edge)),
      'invert the call: a registered callback (state/page.ts onShow) or the layer below')
      .toEqual([])
  })

  it('adds no cross-domain edge past a sibling public surface', () => {
    const { cross } = violations()
    expect(cross.filter((edge) => !CROSS.includes(edge)),
      'reach a sibling through its source.ts or types.ts, or take the verb as a callback')
      .toEqual([])
  })

  it('pins nothing that is gone, so the lists shrink with the tree', () => {
    const { up, cross } = violations()
    expect(PINNED.filter((edge) => !up.includes(edge)), 'delete the healed pins').toEqual([])
    expect(CROSS.filter((edge) => !cross.includes(edge)), 'delete the healed pins').toEqual([])
  })

  it('closes no new cycle, and leaves no more files inside one', () => {
    const found = cycles(edges)
    const strange = found.filter((members) =>
      !CYCLES.some((pinned) => members.every((rel) => pinned.includes(rel))))
    expect(strange, 'a new runtime cycle: break it, or the module-eval order decides your page')
      .toEqual([])
    expect(found.reduce((n, members) => n + members.length, 0)).toBeLessThanOrEqual(IN_CYCLES)
  })

  it('names a reason for every exemption, and exempts nothing that imports', () => {
    for (const [rel, why] of Object.entries(EXEMPT)) {
      expect(known.has(rel), `${rel} no longer exists`).toBe(true)
      expect(why.length, `${rel} has no reason`).toBeGreaterThan(20)
      /* The exemption is only honest while the file is a leaf. */
      const text = readFileSync(join(SRC, rel), 'utf8')
      expect([...text.matchAll(SPECIFIER)].map((m) => m[2] ?? m[3]), rel).toEqual([])
    }
  })

  it('resolves every relative specifier it reads', () => {
    const lost = []
    for (const rel of modules) {
      const text = readFileSync(join(SRC, rel), 'utf8')
      for (const match of text.matchAll(SPECIFIER)) {
        const spec = match[2] ?? match[3]
        if (!spec.startsWith('.')) continue
        /* Two relative specifiers name something that is not a module and so
           cannot be an edge in this graph: the message catalogue, which is
           outside src/ altogether, and a domain's own stylesheet, which Vite
           collects into one CSS asset (features/extAgents/styles.css says how)
           rather than into the module graph. */
        if (spec.endsWith('.json') || spec.endsWith('.css')) continue
        if (!target(rel, spec)) lost.push(`${rel}: ${spec}`)
      }
    }
    expect(lost, 'a specifier this gate cannot resolve is an edge it cannot see').toEqual([])
  })
})
