// Every literal key the page takes a word by names one the shared catalogue
// carries, every key sits under a namespace, and a namespace belongs to one
// domain.
//
// The lookup falls back to the hard-coded English silently, so a misspelled or
// never-added key ships as untranslated text with no signal -- two did (see
// KNOWN_MISSING). Every take-a-key form is read, not just `t(`: `lang.attr(` is
// the other one in use, and the 43 keys it carries were unchecked while the
// regex knew only about the lookup. Keys built by concatenation
// (`t('gui.x.' + n)`) are not checked: only a literal closed by `,` or `)` is a
// whole key.
//
// The shape is `gui.<namespace>.<leaf>`, where the namespace is the domain that
// owns the words and `<leaf>` may itself be grouped (`gui.set.usg.cost` is one
// settings card's leaf). Two things were true when this gate grew the rule and
// both are pinned rather than fixed: thirty-nine keys have no namespace at all
// (LEGACY_FLAT), and of the twenty-nine namespaces a single domain owns,
// twenty-eight are abbreviations of the directory name rather than the name
// (ALIAS) -- `gui.kb` for knowledge, `gui.agent` for extAgents.
//
// Why the aliases are pinned and not renamed: the catalogue is
// i18n/messages.json at the REPO root, and ui-tui generates its own copy from
// it (ui-tui/scripts/gen-i18n.mjs), so renaming a namespace is an edit to both
// front ends and to the file neither of them owns. The equality this gate
// states is the one the catalogue rename makes true; until then ALIAS is the
// mapping, and it may only shrink.

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = new URL('../../src/', import.meta.url)
const CATALOGUE = new URL('../../../i18n/messages.json', import.meta.url)

// Absent from the catalogue today; tracked as a copy fix, not fixed here
// because i18n/messages.json is outside the page's own tree.
const KNOWN_MISSING = new Set(['gui.queue.title', 'gui.queue.remove'])

/* Keys with no namespace: the page-wide words the catalogue grew before there
   was a rule. Down or gone -- a new key names a namespace. */
const LEGACY_FLAT = new Set([
  'gui.add',
  'gui.adv_add',
  'gui.adv_addr_ph',
  'gui.adv_hint',
  'gui.adv_name_ph',
  'gui.attach',
  'gui.boot_fail',
  'gui.cancel',
  'gui.cap_detail',
  'gui.clear_body',
  'gui.clear_search',
  'gui.clear_title',
  'gui.clear_yes',
  'gui.close',
  'gui.collapse_rail',
  'gui.collapse_ws',
  'gui.commands',
  'gui.composer_ph',
  'gui.cron_new',
  'gui.expand_rail',
  'gui.expand_ws',
  'gui.filter_status',
  'gui.n_failed',
  'gui.new_task',
  'gui.reconnected',
  'gui.reconnecting',
  'gui.rename_session',
  'gui.resize_rail',
  'gui.resize_ws',
  'gui.retry',
  'gui.save',
  'gui.search',
  'gui.search_sessions',
  'gui.send',
  'gui.session_commands',
  'gui.stop',
  'gui.turn_died',
  'gui.undo',
  'gui.workspace',
])

/* A namespace a single domain owns whose name is not the directory's, as
   `namespace: domain`. Every one is a catalogue rename waiting on the root
   file; the way off this list is that rename, never another line. */
const ALIAS = {
  agent: 'extAgents',
  answer: 'transcript',
  br: 'browser',
  clarify: 'composer',
  confirm: 'composer',
  conn: 'connections',
  deleg: 'transcript',
  dtl: 'transcript',
  dur: 'cron',
  ext: 'installed',
  filter: 'plugins',
  fold: 'transcript',
  img: 'transcript',
  imode: 'subagents',
  imodel: 'subagents',
  job: 'cron',
  kb: 'knowledge',
  live: 'composer',
  mem: 'memory',
  nav: 'playbooks',
  onb: 'onboard',
  pb: 'playbooks',
  perm: 'settings',
  picker: 'model',
  pill: 'composer',
  q: 'composer',
  qa: 'transcript',
  queue: 'subagents',
}

/* Namespaces more than one domain speaks, one reason each. A namespace here is
   not any domain's, so no directory name can be right for it: the way off this
   list is splitting the words, not renaming them. */
const SHARED = {
  arts: 'an artifact is produced in the transcript and collected in the workspace',
  att: 'an attachment is composed, carried into a delegation and drawn in the transcript',
  caps: 'the capabilities page is chrome, and four domains put rows or entrances on it',
  cron: 'a schedule is the cron page\'s and a delegated agent can carry one',
  dag: 'the delegated graph is drawn as a sheet, a transcript card and a roster row',
  hub: 'the two hubs and their two inventories share the market and installed wording',
  model: 'a model is picked in one domain and defaulted in another, plus the chip component',
  op: 'the six verbs any row offers -- rename, delete, retry -- with no domain of their own',
  page: 'the module pages\' own headings, which App.tsx renders for all of them',
  plug: 'an extension is installed from six places, and the wording is the extension\'s',
  sess: 'a conversation is listed on the rail, read in the transcript and filed in the workspace',
  set: 'the settings dialog\'s cards, three of which are another domain\'s to draw',
  tab: 'the two capability tab labels, which chrome renders and both tabs sync',
  time: 'ago, duration and clock words, said wherever a row carries a timestamp',
  ws: 'the workspace panel is shared ground: four domains draw views into it',
}

function* sources(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      if (name !== 'node_modules') yield* sources(path)
    } else if (/\.(ts|tsx|js|mjs)$/.test(name) && !/\.test\./.test(name)) {
      yield path
    }
  }
}

/* Every form that takes a key and answers a word: the lookup itself, the
   language store's attribute reader, and the two names a component imports
   them under. */
const KEYED = /(?:\blang\.)?\b(?:t|attr|text)\('(gui\.[A-Za-z0-9_.]+)'\s*[,)]/g

/** The domain a file belongs to, or null for the page's own layers. */
function domainOf(rel) {
  const parts = rel.split('/')
  return parts[0] === 'features' && parts.length > 2 ? parts[1] : null
}

/** Every literal key, with the domains and the namespaces that reference it. */
function referenced() {
  const keys = new Set()
  const byNamespace = new Map()
  const root = SRC.pathname
  for (const file of sources(root)) {
    const rel = file.slice(root.length)
    const domain = domainOf(rel)
    for (const m of readFileSync(file, 'utf8').matchAll(KEYED)) {
      keys.add(m[1])
      const namespace = m[1].split('.')[1]
      if (m[1].split('.').length < 3 || !namespace) continue
      if (!byNamespace.has(namespace)) byNamespace.set(namespace, new Set())
      if (domain) byNamespace.get(namespace).add(domain)
    }
  }
  return { keys, byNamespace }
}

describe('i18n keys', () => {
  it('every literal gui.* key exists in the catalogue', () => {
    const ui = JSON.parse(readFileSync(CATALOGUE, 'utf8')).ui
    const { keys } = referenced()
    const missing = [...keys]
      .filter((key) => !(key in ui) && !KNOWN_MISSING.has(key))
      .sort()
    expect(missing).toEqual([])
    const healed = [...KNOWN_MISSING].filter((key) => key in ui)
    expect(healed, 'remove healed keys from KNOWN_MISSING').toEqual([])
  })

  it('gives every key a namespace', () => {
    const { keys } = referenced()
    const flat = [...keys].filter((key) => key.split('.').length < 3 && !LEGACY_FLAT.has(key)).sort()
    expect(flat, 'name a namespace: gui.<domain>.<leaf>').toEqual([])
    const gone = [...LEGACY_FLAT].filter((key) => !keys.has(key)).sort()
    expect(gone, 'delete the keys nothing says any more from LEGACY_FLAT').toEqual([])
  })

  it('lets one domain own a namespace', () => {
    const { byNamespace } = referenced()
    const wrong = []
    for (const [namespace, domains] of byNamespace) {
      const reason = SHARED[namespace]
      if (domains.size > 1 && !reason) {
        wrong.push(`gui.${namespace} is said by ${[...domains].sort().join(', ')}`)
      }
      if (domains.size > 1 && reason && reason.length < 20) wrong.push(`gui.${namespace} has no reason`)
      if (domains.size < 2 && reason) wrong.push(`gui.${namespace} has one domain now: take it off SHARED`)
    }
    for (const namespace of Object.keys(SHARED)) {
      if (!byNamespace.has(namespace)) wrong.push(`gui.${namespace} is said by nothing: take it off SHARED`)
    }
    expect(wrong, 'split the words, or pin the namespace in SHARED with the reason two domains say it').toEqual([])
  })

  it('names a namespace after the domain that owns it', () => {
    const { byNamespace } = referenced()
    const wrong = []
    for (const [namespace, domains] of byNamespace) {
      if (domains.size !== 1) continue
      const [domain] = [...domains]
      if (namespace === domain) {
        if (namespace in ALIAS) wrong.push(`gui.${namespace} matches ${domain} now: take it off ALIAS`)
        continue
      }
      if (ALIAS[namespace] !== domain) {
        wrong.push(`gui.${namespace} is features/${domain}/'s (ALIAS says ${String(ALIAS[namespace])})`)
      }
    }
    for (const [namespace, domain] of Object.entries(ALIAS)) {
      const domains = byNamespace.get(namespace)
      if (!domains) wrong.push(`gui.${namespace} is said by nothing: take it off ALIAS`)
      else if (domains.size > 1) wrong.push(`gui.${namespace} is shared now, not ${domain}'s: move it to SHARED`)
    }
    expect(wrong, 'rename the namespace to the directory, or pin the pair in ALIAS').toEqual([])
  })
})
