/* What the page knows about an agent beyond what the wire says: the one-line
 * summary under its name, who makes it, and -- for one this machine has not got
 * -- where to get it. The row itself carries none of this: `description` is
 * the prompt text the dispatching model reads, and `probe_detail` is a verdict,
 * not an introduction.
 *
 * Keyed by preset first and by name second. A preset is the package behind a
 * row and survives a rename; Raven's own agents carry no preset, and their
 * names are the manifest's and not the reader's to change, so the name is the
 * stable key there. A hand-written row matching neither gets nothing, and the
 * page falls back to what the wire does say.
 *
 * The install data is the vendor's own documentation, the same commands
 * raven/agent/subagent's presets hint at where they hint at all. `site` is a
 * bare host, spelled the way the prototype spells it and linked as https.
 */

import { t } from '../../i18n/t'

import type { ExtAgentRow } from './types'

interface CatalogueEntry {
  /* The one-line summary, as a message key. */
  short: string
  /* Who makes it: a message key when the name is translated, else the brand
     verbatim. */
  by?: string
  brand?: string
  site?: string
  cmd?: string
}

const OWN_BY = 'gui.agent.by_raven'

/* A Mirasim row is the agent of the same name run through Mirasim: its maker is
   that agent's, and what an absent one lacks is Mirasim itself, which installs
   from Mirasim's own site rather than by one command. DSH names no maker it can
   be held to, so it gets none. */
const MIRASIM_SHORT = 'gui.agent.short_mirasim'
const MIRASIM_SITE = 'mirasim.ai/download'

/* The nine presets that reach an agent through Mirasim, named by the prefix
   they share. A family rather than a table of its own: the roster is the
   server's, and a family spelled here would be a second place for it to
   drift -- this one only has to answer which card a row folds into.
   Derived from the preset and never from the name: a preset survives a
   rename, and `mirasim_` is what the server's own presets are keyed by. */
export const MIRASIM_FAMILY = 'mirasim'

/* Two, not one: a card standing for a single agent hides the very thing the
   reader came for, and a family that has come down to one visible member is
   better drawn as that member. */
export const MIN_FAMILY = 2

export function familyOf(row: ExtAgentRow): string | null {
  return row.preset && row.preset.startsWith(`${MIRASIM_FAMILY}_`) ? MIRASIM_FAMILY : null
}

/* A catalogue key that belongs to a family, by the same prefix test the rows
   are folded by, so the skeleton and the grid cannot disagree about which
   entries collapse. */
const familyOfKey = (key: string): string | null =>
  key.startsWith(`${MIRASIM_FAMILY}_`) ? MIRASIM_FAMILY : null

const CATALOGUE: Record<string, CatalogueEntry> = {
  Raven: { short: 'gui.agent.short_raven', by: OWN_BY },
  'Raven-Code': { short: 'gui.agent.short_raven_code', by: OWN_BY },
  'Raven-Design': { short: 'gui.agent.short_raven_design', by: OWN_BY },
  'Raven-Research': { short: 'gui.agent.short_raven_research', by: OWN_BY },
  'Raven-Oncall': { short: 'gui.agent.short_raven_oncall', by: OWN_BY },
  claude_code: {
    short: 'gui.agent.short_claude_code',
    brand: 'Anthropic',
    site: 'claude.com/product/claude-code',
    cmd: 'npm install -g @anthropic-ai/claude-code',
  },
  codex: { short: 'gui.agent.short_codex', brand: 'OpenAI', site: 'openai.com/codex', cmd: 'npm install -g @openai/codex' },
  opencode: {
    short: 'gui.agent.short_opencode',
    by: 'gui.agent.by_oss',
    site: 'opencode.ai',
    cmd: 'curl -fsSL https://opencode.ai/install | bash',
  },
  hermes: {
    short: 'gui.agent.short_hermes',
    brand: 'Nous Research',
    site: 'hermes-agent.nousresearch.com',
    cmd: 'curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash',
  },
  openclaw: {
    short: 'gui.agent.short_openclaw',
    brand: 'OpenClaw',
    site: 'openclaw.ai',
    cmd: 'curl -fsSL https://openclaw.ai/install.sh | bash',
  },
  mirothinker: { short: 'gui.agent.short_mirothinker', brand: 'MiroMind', site: 'platform.miromind.ai' },
  github_copilot: {
    short: 'gui.agent.short_github_copilot',
    brand: 'GitHub',
    site: 'github.com/features/copilot/cli',
    cmd: 'npm install -g @github/copilot',
  },
  qwen_code: {
    short: 'gui.agent.short_qwen_code',
    by: 'gui.agent.by_qwen_code',
    site: 'github.com/QwenLM/qwen-code',
    cmd: 'npm install -g @qwen-code/qwen-code',
  },
  codebuddy: {
    short: 'gui.agent.short_codebuddy',
    by: 'gui.agent.by_codebuddy',
    site: 'www.codebuddy.ai',
    cmd: 'npm install -g @tencent-ai/codebuddy-code',
  },
  qoder: { short: 'gui.agent.short_qoder', by: 'gui.agent.by_qoder', site: 'qoder.com', cmd: 'curl -fsSL https://qoder.com/install | bash' },
  grok: { short: 'gui.agent.short_grok', brand: 'xAI', site: 'x.ai', cmd: 'npm i -g @xai-official/grok' },
  kimi_code: {
    short: 'gui.agent.short_kimi_code',
    by: 'gui.agent.by_kimi_code',
    site: 'kimi.com/code',
    cmd: 'curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash',
  },
  pi: { short: 'gui.agent.short_pi', brand: 'Pi', site: 'pi.dev', cmd: 'npm install -g @earendil-works/pi-coding-agent' },
  mirasim_claude: { short: MIRASIM_SHORT, brand: 'Anthropic', site: MIRASIM_SITE },
  mirasim_codex: { short: MIRASIM_SHORT, brand: 'OpenAI', site: MIRASIM_SITE },
  mirasim_kimi: { short: MIRASIM_SHORT, by: 'gui.agent.by_kimi_code', site: MIRASIM_SITE },
  mirasim_antigravity: { short: MIRASIM_SHORT, brand: 'Google', site: MIRASIM_SITE },
  mirasim_qwen: { short: MIRASIM_SHORT, by: 'gui.agent.by_qwen_code', site: MIRASIM_SITE },
  mirasim_grok: { short: MIRASIM_SHORT, brand: 'xAI', site: MIRASIM_SITE },
  mirasim_dsh: { short: MIRASIM_SHORT, site: MIRASIM_SITE },
  mirasim_pi: { short: MIRASIM_SHORT, brand: 'Pi', site: MIRASIM_SITE },
  mirasim_zcode: { short: MIRASIM_SHORT, brand: 'Z.ai', site: MIRASIM_SITE },
}

function entryOf(row: ExtAgentRow): CatalogueEntry | undefined {
  if (row.builtin) return CATALOGUE.Raven
  const byPreset = row.preset && Object.hasOwn(CATALOGUE, row.preset) ? CATALOGUE[row.preset] : undefined
  if (byPreset) return byPreset
  return Object.hasOwn(CATALOGUE, row.name) ? CATALOGUE[row.name] : undefined
}

/* One of Raven's own, whichever way this install registered it. The wire says
   so for a built-in row, a folder discovered under `agents/`, and a configured
   acp row whose handshake named Raven (`own`) -- the last is the server's to
   know, whatever the row is called here. The catalogue is the fallback for a
   server that predates `own`: an install that registered a shipped product as
   a config row (its `install.py` does that) carries no flag there, and the row
   is still Raven's. */
/* How many rows a roster has before the server has said: the catalogue is
   the roster on every machine -- Raven's own agents and every preset, whether
   or not it is installed -- and only a hand-written agent adds to it. */
export const catalogueSize = (): number => Object.keys(CATALOGUE).length

/* How many CARDS that catalogue makes, which is what the grid's skeleton draws
   so the first frame lands where the real one will instead of jumping when a
   family folds. Counted here rather than written down, because a number beside
   the table is the one thing that goes stale in silence. */
export const catalogueCards = (): number => {
  const families = new Map<string, number>()
  let cards = 0
  for (const key of Object.keys(CATALOGUE)) {
    const family = familyOfKey(key)
    if (family) families.set(family, (families.get(family) ?? 0) + 1)
    else cards += 1
  }
  for (const count of families.values()) cards += count < MIN_FAMILY ? count : 1
  return cards
}

export function isOwnRow(row: ExtAgentRow): boolean {
  return !!row.own || !!row.builtin || !!row.vendored || entryOf(row)?.by === OWN_BY
}

/* The one line under the name, or '' when the catalogue has nothing to say. */
export function shortOf(row: ExtAgentRow): string {
  const entry = entryOf(row)
  return entry ? t(entry.short) : ''
}

/* Who makes it, or '' for a row nobody catalogued. */
export function byOf(row: ExtAgentRow): string {
  const entry = entryOf(row)
  if (!entry) return isOwnRow(row) ? t(OWN_BY) : ''
  return entry.by ? t(entry.by) : entry.brand || ''
}

/* Where an absent agent comes from: a command to paste and a site to visit,
   either or both possibly missing. `node` when what is absent is `npx`: a
   preset launched through it needs Node.js, which brings npx, and the agent's
   own installer is then the wrong answer -- an `npm i -g` cannot run without
   Node.js, and an agent installed some other way still launches through npx. */
export function installOf(row: ExtAgentRow): { site?: string; cmd?: string; node?: boolean } {
  if (row.probe_missing === 'npx') return { site: NODE_SITE, node: true }
  const entry = entryOf(row)
  return entry ? { site: entry.site, cmd: entry.cmd } : {}
}

const NODE_SITE = 'nodejs.org'
