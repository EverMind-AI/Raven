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
 * The install data is the vendor's own documentation (sources are listed in
 * the PR that added them), the same commands raven/agent/subagent's presets
 * hint at where they hint at all. `site` is a bare host, spelled the way the
 * prototype spells it and linked as https.
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
   either or both possibly missing. */
export function installOf(row: ExtAgentRow): { site?: string; cmd?: string } {
  const entry = entryOf(row)
  return entry ? { site: entry.site, cmd: entry.cmd } : {}
}
